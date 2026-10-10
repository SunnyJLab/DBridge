"""Read-only loader for relationships.d JSON.

JSON files are validated strictly and never executed. One bad file becomes a
structured load error; valid siblings still load. Path escapes and symlinks
are rejected. Reloads replace an immutable snapshot under a lock.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import AbstractSet, Any

from .errors import (
    BAD_PARAM,
    BadParamError,
    InvalidSqlError,
    UnknownObjectError,
)
from .sql_safety import (
    strip_leading_comments,
    validate_identifier,
)

_LOG = logging.getLogger(__name__)

_RELATIONSHIPS_DIR = "relationships.d"
_JSON_SUFFIX = ".json"
_JOIN_DIRECTIONS = frozenset({"parent", "child"})
_MAX_JSON_BYTES = 1_048_576
_WS = frozenset(" \t\r\n\f\v")
_IDENT_CHAR = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$#")
_SYSTEM_PREFIXES = frozenset(
    {
        Path("/tmp"),
        Path("/var"),
        Path("/etc"),
        Path("/private"),
        Path("/home"),
        Path("/Users"),
        Path("/opt"),
        Path("/usr"),
        Path("/dev"),
        Path("/root"),
    }
)
_Q_PAIR = {"[": "]", "(": ")", "{": "}", "<": ">"}

_REL_KEYS = frozenset({"connection", "table", "joins"})
_JOIN_KEYS = frozenset({"table", "on", "direction"})
_CODE_KEYS = frozenset(
    {
        "id",
        "type",
        "connection",
        "values_table",
        "group_table",
        "group_column",
        "code_column",
        "name_column",
        "extra_columns",
        "scope",
        "group_rule",
        "bindings",
    }
)
_SCOPE_KEYS = frozenset({"owner", "table_pattern"})
_BINDING_KEYS = frozenset({"column", "group"})


class CatalogView:
    """Duck-typed catalog used only when the caller supplies one.

    Expected methods (all optional individually, but existence checks run only
    when the matching method is present):

    * ``has_object(connection, qualified_name) -> bool``
    * ``has_column(connection, qualified_table, column) -> bool``
    """


@dataclass(frozen=True)
class LoadError:
    """One rejected file or directory. Does not abort the rest of a reload."""

    path: str
    kind: str
    message: str
    hint: str
    code: str = BAD_PARAM

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "error": {
                "code": self.code,
                "message": self.message,
                "hint": self.hint,
            },
        }


@dataclass(frozen=True)
class JoinOn:
    left: str
    right: str


@dataclass(frozen=True)
class JoinSpec:
    table: str
    on: tuple[JoinOn, ...]
    direction: str


@dataclass(frozen=True)
class ManualRelationship:
    connection: str
    table: str
    joins: tuple[JoinSpec, ...]
    source_path: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "table": self.table,
            "source": "manual",
            "source_path": self.source_path,
            "joins": [
                {
                    "table": item.table,
                    "on": [[pair.left, pair.right] for pair in item.on],
                    "direction": item.direction,
                    "source": "manual",
                    "confidence": 1.0,
                }
                for item in self.joins
            ],
        }


@dataclass(frozen=True)
class ExtensionSnapshot:
    relationships: tuple[ManualRelationship, ...] = ()
    errors: tuple[LoadError, ...] = ()
    base_dir: str = ""

    def relationships_for(self, connection: str, table: str | None = None) -> tuple[ManualRelationship, ...]:
        wanted = connection
        matches: list[ManualRelationship] = []
        for item in self.relationships:
            if item.connection != wanted:
                continue
            if table is not None and item.table != table:
                continue
            matches.append(item)
        return tuple(matches)

    def to_payload(self) -> dict[str, Any]:
        return {
            "base_dir": self.base_dir,
            "relationships": [item.to_payload() for item in self.relationships],
            "errors": [item.to_payload() for item in self.errors],
        }


@dataclass
class _BuildState:
    relationships: list[ManualRelationship] = field(default_factory=list)
    errors: list[LoadError] = field(default_factory=list)
    relationship_keys: set[tuple[str, str]] = field(default_factory=set)


class ExtensionLoader:
    """Load extension JSON from ``base_dir/relationships.d``."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        catalog: Any | None = None,
    ) -> None:
        self._lexical_base = Path(base_dir)
        self._catalog = catalog
        self._lock = threading.RLock()
        try:
            self._base_dir = _validated_root(self._lexical_base, require_existing=False)
        except _LoadFail as exc:
            raise BadParamError(exc.error.message, hint=exc.error.hint) from None
        self._snapshot = ExtensionSnapshot(base_dir=str(self._base_dir))

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def snapshot(self) -> ExtensionSnapshot:
        with self._lock:
            return self._snapshot

    def errors(self) -> tuple[LoadError, ...]:
        return self.snapshot().errors

    def relationships(
        self, connection: str | None = None, table: str | None = None
    ) -> tuple[ManualRelationship, ...]:
        snap = self.snapshot()
        if connection is None:
            if table is not None:
                raise BadParamError(
                    "table 필터는 connection과 함께 사용해야 합니다",
                    hint="relationships(connection, table) 형식으로 호출하세요",
                )
            return snap.relationships
        return snap.relationships_for(connection, table)

    def reload(self) -> ExtensionSnapshot:
        """Re-read JSON files and atomically replace the snapshot."""
        snapshot = self._load_all()
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _load_all(self) -> ExtensionSnapshot:
        state = _BuildState()
        try:
            root = self._resolve_base()
        except _LoadFail as exc:
            state.errors.append(exc.error)
            return self._finish(state, str(self._base_dir))
        self._load_kind(
            root, _RELATIONSHIPS_DIR, "relationships", state, self._load_relationship_file
        )
        return self._finish(state, str(root))

    def _finish(self, state: _BuildState, base: str) -> ExtensionSnapshot:
        for item in state.errors:
            _LOG.error(
                "extension load error kind=%s path=%s code=%s message=%s",
                item.kind,
                item.path,
                item.code,
                item.message,
            )
        return ExtensionSnapshot(
            relationships=tuple(state.relationships),
            errors=tuple(state.errors),
            base_dir=base,
        )

    def _resolve_base(self) -> Path:
        root = _validated_root(self._lexical_base, require_existing=True)
        self._base_dir = root
        return root

    def _load_kind(
        self,
        root: Path,
        dirname: str,
        kind: str,
        state: _BuildState,
        loader: Callable[[Path, dict[str, Any], _BuildState], None],
    ) -> None:
        directory = root / dirname
        if directory.is_symlink():
            state.errors.append(
                LoadError(
                    path=str(directory),
                    kind=kind,
                    code=BAD_PARAM,
                    message=f"{dirname} 는 심볼릭 링크일 수 없습니다",
                    hint="실제 디렉터리만 허용됩니다",
                )
            )
            return
        if not directory.exists():
            return
        if not directory.is_dir():
            state.errors.append(
                LoadError(
                    path=str(directory),
                    kind=kind,
                    code=BAD_PARAM,
                    message=f"{dirname} 는 디렉터리여야 합니다",
                    hint="JSON 파일은 해당 .d 디렉터리 바로 아래에 두세요",
                )
            )
            return
        try:
            resolved_dir = directory.resolve()
            _assert_inside(resolved_dir, root.resolve())
        except _LoadFail as exc:
            state.errors.append(exc.error)
            return
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if entry.name.startswith("."):
                continue
            if not entry.name.endswith(_JSON_SUFFIX):
                continue
            self._load_entry(entry, resolved_dir, kind, state, loader)

    def _load_entry(
        self,
        entry: Path,
        directory: Path,
        kind: str,
        state: _BuildState,
        loader: Callable[[Path, dict[str, Any], _BuildState], None],
    ) -> None:
        path_s = str(entry)
        if entry.is_symlink():
            state.errors.append(
                LoadError(
                    path=path_s,
                    kind=kind,
                    code=BAD_PARAM,
                    message="심볼릭 링크 JSON은 허용되지 않습니다",
                    hint="실제 파일만 배치하세요",
                )
            )
            return
        if not entry.is_file():
            state.errors.append(
                LoadError(
                    path=path_s,
                    kind=kind,
                    code=BAD_PARAM,
                    message="JSON 파일만 허용됩니다",
                    hint=".d 디렉터리에는 일반 파일만 두세요",
                )
            )
            return
        try:
            resolved = entry.resolve()
            _assert_inside(resolved, directory)
        except _LoadFail as exc:
            state.errors.append(exc.error)
            return
        try:
            payload = _read_json_object(resolved)
        except _LoadFail as exc:
            state.errors.append(exc.error)
            return
        try:
            loader(resolved, payload, state)
        except _LoadFail as exc:
            state.errors.append(exc.error)
        except (BadParamError, InvalidSqlError, UnknownObjectError) as exc:
            state.errors.append(
                LoadError(
                    path=str(resolved),
                    kind=kind,
                    code=exc.code,
                    message=exc.message,
                    hint=exc.hint,
                )
            )

    def _load_relationship_file(
        self, path: Path, payload: dict[str, Any], state: _BuildState
    ) -> None:
        _reject_unknown_keys(payload, _REL_KEYS, str(path), "relationships")
        connection = _require_connection(
            payload.get("connection"), str(path), "relationships"
        )
        table = _ident(payload.get("table"), "table", str(path), "relationships", max_parts=2)
        joins_raw = payload.get("joins")
        if not isinstance(joins_raw, list) or not joins_raw:
            raise _LoadFail(
                LoadError(
                    path=str(path),
                    kind="relationships",
                    code=BAD_PARAM,
                    message="joins는 비어 있지 않은 배열이어야 합니다",
                    hint='joins: [{"table","on","direction"}]',
                )
            )
        key = (connection, table)
        if key in state.relationship_keys:
            raise _LoadFail(
                LoadError(
                    path=str(path),
                    kind="relationships",
                    code=BAD_PARAM,
                    message=f"중복된 관계 정의입니다: {connection} {table}",
                    hint="같은 connection+table 관계는 파일 하나에서만 정의하세요",
                )
            )
        joins: list[JoinSpec] = []
        seen_targets: set[str] = set()
        for index, item in enumerate(joins_raw):
            joins.append(_parse_join(item, index, str(path), seen_targets))
        state.relationship_keys.add(key)
        state.relationships.append(
            ManualRelationship(
                connection=connection,
                table=table,
                joins=tuple(joins),
                source_path=str(path),
            )
        )

class _LoadFail(Exception):
    def __init__(self, error: LoadError) -> None:
        self.error = error
        super().__init__(error.message)


def extract_bind_names(sql: str) -> tuple[str, ...]:
    """Named Oracle binds (``:ident``) outside comments and literals, first-seen order."""
    stripped, _ = strip_leading_comments(sql)
    scanner = _BindScanner(stripped)
    return scanner.names()


def _parse_join(item: Any, index: int, path: str, seen_targets: set[str]) -> JoinSpec:
    if not isinstance(item, dict):
        raise _LoadFail(
            LoadError(
                path=path,
                kind="relationships",
                code=BAD_PARAM,
                message=f"joins[{index}]는 객체여야 합니다",
                hint='{"table","on","direction"} 형식입니다',
            )
        )
    extra = set(item) - _JOIN_KEYS
    if extra:
        raise _LoadFail(
            LoadError(
                path=path,
                kind="relationships",
                code=BAD_PARAM,
                message=f"joins[{index}]에 알 수 없는 키: {sorted(extra)[0]}",
                hint="table/on/direction 만 허용됩니다",
            )
        )
    table = _ident(item.get("table"), f"joins[{index}].table", path, "relationships", max_parts=2)
    if table in seen_targets:
        raise _LoadFail(
            LoadError(
                path=path,
                kind="relationships",
                code=BAD_PARAM,
                message=f"중복된 조인 대상입니다: {table}",
                hint="같은 대상 테이블은 한 번만 적으세요",
            )
        )
    direction = item.get("direction")
    if not isinstance(direction, str) or direction not in _JOIN_DIRECTIONS:
        raise _LoadFail(
            LoadError(
                path=path,
                kind="relationships",
                code=BAD_PARAM,
                message=f"joins[{index}].direction이 올바르지 않습니다",
                hint="parent 또는 child만 허용됩니다",
            )
        )
    on_raw = item.get("on")
    if not isinstance(on_raw, list) or not on_raw:
        raise _LoadFail(
            LoadError(
                path=path,
                kind="relationships",
                code=BAD_PARAM,
                message=f"joins[{index}].on은 비어 있지 않은 배열이어야 합니다",
                hint='on: [["LEFT_COL","RIGHT_COL"]]',
            )
        )
    pairs: list[JoinOn] = []
    for pair_index, pair in enumerate(on_raw):
        if not isinstance(pair, list) or len(pair) != 2:
            raise _LoadFail(
                LoadError(
                    path=path,
                    kind="relationships",
                    code=BAD_PARAM,
                    message=f"joins[{index}].on[{pair_index}]는 길이 2 배열이어야 합니다",
                    hint='["LEFT_COL","RIGHT_COL"] 형식입니다',
                )
            )
        left = _ident(
            pair[0],
            f"joins[{index}].on[{pair_index}][0]",
            path,
            "relationships",
            max_parts=1,
        )
        right = _ident(
            pair[1],
            f"joins[{index}].on[{pair_index}][1]",
            path,
            "relationships",
            max_parts=1,
        )
        pairs.append(JoinOn(left=left, right=right))
    seen_targets.add(table)
    return JoinSpec(table=table, on=tuple(pairs), direction=direction)


def _ident(value: Any, field: str, path: str, kind: str, *, max_parts: int) -> str:
    if not isinstance(value, str):
        raise _LoadFail(
            LoadError(
                path=path,
                kind=kind,
                code=BAD_PARAM,
                message=f"{field}는 문자열이어야 합니다",
                hint="미인용 Oracle 식별자만 허용됩니다",
            )
        )
    try:
        return validate_identifier(value, max_parts=max_parts)
    except (BadParamError, UnknownObjectError) as exc:
        raise _LoadFail(
            LoadError(
                path=path,
                kind=kind,
                code=exc.code,
                message=f"{field}: {exc.message}",
                hint=exc.hint,
            )
        ) from exc


def _require_name(value: Any, field: str, path: str, kind: str) -> str:
    text = _require_nonempty_string(value, field, path, kind)
    if text != text.strip() or any(ch in text for ch in "/\\:\x00"):
        raise _LoadFail(
            LoadError(
                path=path,
                kind=kind,
                code=BAD_PARAM,
                message=f"{field}에 허용되지 않은 문자가 있습니다",
                hint="경로 구분자 없는 이름을 사용하세요",
            )
        )
    return text


# JSON 로드 실패를 LoadError 로 보고한다. ToolError 를 던지는
# common.params._require_nonempty_string 와는 다른 함수다 (이름만 같음).
def _require_nonempty_string(value: Any, field: str, path: str, kind: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _LoadFail(
            LoadError(
                path=path,
                kind=kind,
                code=BAD_PARAM,
                message=f"{field}는 비어 있지 않은 문자열이어야 합니다",
                hint="해당 필드를 확인하세요",
            )
        )
    if value != value.strip():
        raise _LoadFail(
            LoadError(
                path=path,
                kind=kind,
                code=BAD_PARAM,
                message=f"{field} 앞뒤에 공백을 둘 수 없습니다",
                hint="값을 공백 없이 입력하세요",
            )
        )
    return value


def _require_connection(value: Any, path: str, kind: str) -> str:
    text = _require_nonempty_string(value, "connection", path, kind)
    if text == "*" or any(ch in text for ch in "/\\:\x00") or text != text.strip():
        raise _LoadFail(
            LoadError(
                path=path,
                kind=kind,
                code=BAD_PARAM,
                message="connection 이름이 올바르지 않습니다",
                hint="커넥션 설정에 있는 이름을 그대로 쓰세요",
            )
        )
    return text


def _reject_unknown_keys(
    payload: Mapping[str, Any], allowed: AbstractSet[str], path: str, kind: str
) -> None:
    extra = [key for key in payload if key not in allowed]
    if extra:
        raise _LoadFail(
            LoadError(
                path=path,
                kind=kind,
                code=BAD_PARAM,
                message=f"알 수 없는 키입니다: {extra[0]}",
                hint="스키마에 정의된 필드만 허용됩니다",
            )
        )


def _read_json_object(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size > _MAX_JSON_BYTES:
        raise _LoadFail(
            LoadError(
                path=str(path),
                kind="path",
                code=BAD_PARAM,
                message="JSON 파일이 너무 큽니다",
                hint=f"{_MAX_JSON_BYTES}바이트 이하만 허용됩니다",
            )
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _LoadFail(
            LoadError(
                path=str(path),
                kind="path",
                code=BAD_PARAM,
                message="JSON은 UTF-8이어야 합니다",
                hint="UTF-8로 저장하세요",
            )
        ) from exc
    except OSError as exc:
        raise _LoadFail(
            LoadError(
                path=str(path),
                kind="path",
                code=BAD_PARAM,
                message=f"파일을 읽을 수 없습니다: {exc}",
                hint="프록시 OS 계정의 읽기 권한을 확인하세요",
            )
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _LoadFail(
            LoadError(
                path=str(path),
                kind="path",
                code=BAD_PARAM,
                message=f"JSON 구문 오류: {exc.msg}",
                hint="해당 파일을 유효한 JSON 객체로 고치세요",
            )
        ) from exc
    if not isinstance(payload, dict):
        raise _LoadFail(
            LoadError(
                path=str(path),
                kind="path",
                code=BAD_PARAM,
                message="JSON 루트는 객체여야 합니다",
                hint="배열이 아닌 객체 하나를 사용하세요",
            )
        )
    return payload


def _lexically_present(path: Path) -> bool:
    try:
        return path.is_symlink() or path.exists()
    except OSError:
        return False


def _existing_components(raw: Path) -> tuple[Path, ...]:
    path = Path(raw)
    items: list[Path] = []
    if path.is_absolute():
        acc = Path(path.anchor)
        rest = path.parts[1:]
        if _lexically_present(acc):
            items.append(acc)
        for part in rest:
            acc = acc / part
            if _lexically_present(acc):
                items.append(acc)
        return tuple(items)
    acc: Path | None = None
    for part in path.parts:
        acc = Path(part) if acc is None else acc / part
        if _lexically_present(acc):
            items.append(acc)
    return tuple(items)


def _is_ignored_system_prefix(path: Path) -> bool:
    if not path.is_absolute():
        return False
    return Path(path) in _SYSTEM_PREFIXES


def _reject_symlink_components(raw: Path) -> None:
    path = Path(raw)
    for component in _existing_components(path):
        if not component.is_symlink():
            continue
        if component != path and _is_ignored_system_prefix(component):
            continue
        raise _LoadFail(
            LoadError(
                path=str(component),
                kind="path",
                code=BAD_PARAM,
                message="확장 루트 디렉터리는 심볼릭 링크일 수 없습니다",
                hint="실제 디렉터리 경로를 base_dir로 전달하세요",
            )
        )


def _validated_root(base_dir: str | Path, *, require_existing: bool) -> Path:
    raw = Path(base_dir)
    _reject_symlink_components(raw)
    if raw.is_symlink():
        raise _LoadFail(
            LoadError(
                path=str(raw),
                kind="path",
                code=BAD_PARAM,
                message="확장 루트 디렉터리는 심볼릭 링크일 수 없습니다",
                hint="실제 디렉터리 경로를 base_dir로 전달하세요",
            )
        )
    if not raw.exists():
        if not require_existing:
            return raw.resolve()
        raise _LoadFail(
            LoadError(
                path=str(raw),
                kind="path",
                code=BAD_PARAM,
                message="확장 루트 디렉터리가 없습니다",
                hint="exe 옆 디렉터리에 relationships.d 를 두세요",
            )
        )
    if not raw.is_dir():
        raise _LoadFail(
            LoadError(
                path=str(raw),
                kind="path",
                code=BAD_PARAM,
                message="확장 루트가 디렉터리가 아닙니다",
                hint="base_dir은 디렉터리여야 합니다",
            )
        )
    return raw.resolve()


def _assert_inside(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise _LoadFail(
            LoadError(
                path=str(path),
                kind="path",
                code=BAD_PARAM,
                message="확장 디렉터리 밖으로 탈출하는 경로입니다",
                hint="base_dir 아래의 실제 파일만 허용됩니다",
            )
        ) from exc


class _BindScanner:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.n = len(sql)
        self.i = 0

    def names(self) -> tuple[str, ...]:
        found: list[str] = []
        seen: set[str] = set()
        while self.i < self.n:
            self._skip_trivia()
            if self.i >= self.n:
                break
            ch = self.sql[self.i]
            if ch == ":" and self._is_bind_start():
                name = self._read_bind()
                key = name.upper()
                if key in seen:
                    previous = next(item for item in found if item.upper() == key)
                    if previous != name:
                        raise InvalidSqlError(
                            f"SQL 바인드 이름이 모호합니다: :{name}",
                            hint=":id 와 :ID 처럼 대소문자만 다른 바인드는 허용되지 않습니다",
                        )
                    continue
                seen.add(key)
                found.append(name)
                continue
            self.i += 1
        return tuple(found)

    def _is_bind_start(self) -> bool:
        nxt = self.sql[self.i + 1] if self.i + 1 < self.n else ""
        if nxt == "=" or nxt == ":":
            return False
        return bool(nxt) and (nxt.isalpha() or nxt == "_" or nxt.isdigit())

    def _read_bind(self) -> str:
        self.i += 1
        start = self.i
        if self.i < self.n and self.sql[self.i] == '"':
            raise InvalidSqlError(
                "인용된 바인드 이름은 허용되지 않습니다",
                hint=":order_no 형식의 미인용 바인드만 사용하세요",
            )
        while self.i < self.n and self.sql[self.i] in _IDENT_CHAR:
            self.i += 1
        name = self.sql[start : self.i]
        if not name:
            raise InvalidSqlError(
                "빈 바인드 이름입니다",
                hint=":name 형식만 허용됩니다",
            )
        return name

    def _skip_trivia(self) -> None:
        while self.i < self.n:
            ch = self.sql[self.i]
            if ch in _WS:
                self.i += 1
                continue
            if ch == "-" and self._peek(1) == "-":
                self.i += 2
                while self.i < self.n and self.sql[self.i] not in "\r\n":
                    self.i += 1
                continue
            if ch == "/" and self._peek(1) == "*":
                self.i += 2
                while self.i < self.n and not (
                    self.sql[self.i] == "*" and self._peek(1) == "/"
                ):
                    self.i += 1
                self.i = min(self.n, self.i + 2)
                continue
            if ch == '"':
                self.i += 1
                while self.i < self.n:
                    if self.sql[self.i] == '"':
                        if self._peek(1) == '"':
                            self.i += 2
                            continue
                        self.i += 1
                        break
                    self.i += 1
                continue
            if self._try_skip_string():
                continue
            return

    def _try_skip_string(self) -> bool:
        i = self.i
        s = self.sql
        n = self.n
        p = i
        if p < n and s[p] in "nN":
            p += 1
        qquote = False
        if p < n and s[p] in "qQ":
            qquote = True
            p += 1
        if p >= n or s[p] != "'":
            return False
        if qquote:
            self.i = p + 1
            if self.i >= n:
                return True
            opener = s[self.i]
            closer = _Q_PAIR.get(opener, opener)
            self.i += 1
            while self.i < n:
                if s[self.i] == closer and self._peek(1) == "'":
                    self.i += 2
                    return True
                self.i += 1
            return True
        self.i = p + 1
        while self.i < n:
            if s[self.i] == "'":
                if self._peek(1) == "'":
                    self.i += 2
                    continue
                self.i += 1
                return True
            self.i += 1
        return True

    def _peek(self, k: int) -> str:
        j = self.i + k
        return self.sql[j] if j < self.n else ""


__all__ = [
    "CatalogView",
    "ExtensionLoader",
    "ExtensionSnapshot",
    "JoinOn",
    "JoinSpec",
    "LoadError",
    "ManualRelationship",
    "extract_bind_names",
]
