"""Stable tool error codes and a small explicit exception hierarchy.

Serialized shape::

    {"error": {"code": str, "ora"?: str, "message": str, "hint": str}}

``ora`` is omitted when unknown. Codes are the public contract; class names
are an implementation detail of this package.
"""

from __future__ import annotations

import builtins
import re
from typing import Any, Mapping

NO_ACCESS = "NO_ACCESS"
TIMEOUT = "TIMEOUT"
INVALID_SQL = "INVALID_SQL"
UNKNOWN_OBJECT = "UNKNOWN_OBJECT"
BAD_PARAM = "BAD_PARAM"
NO_STATS = "NO_STATS"

ERROR_CODES = frozenset(
    {
        NO_ACCESS,
        TIMEOUT,
        INVALID_SQL,
        UNKNOWN_OBJECT,
        BAD_PARAM,
        NO_STATS,
    }
)

_CODE_RE = re.compile(r"(?:ORA|DPI|DPY)-\d+", re.IGNORECASE)
# Privilege, credential, and TNS failures. ORA-01745 is invalid bind name
# (InvalidSqlError). ORA-01018 is LONG fetch mismatch; ORA-00995/01940 are
# not access denials for this SELECT-only adapter.
_ACCESS_ORA_CODES = frozenset(
    {
        942,
        980,
        1004,
        1005,
        1017,
        1031,
        1035,
        1045,
        1917,
        28000,
        12154,
        12505,
        12514,
        12541,
        12545,
    }
)
_TIMEOUT_FULL_CODES = frozenset(
    {
        "DPI-1067",
        "DPY-4011",
        "DPY-4012",
        "DPY-4024",
        "DPY-6001",
    }
)
_CONNECT_HINTS = (
    "tns",
    "listener",
    "cannot connect",
    "connect failed",
    "could not resolve",
    "refused",
)
_DEFAULT_HINTS: Mapping[str, str] = {
    NO_ACCESS: "my_scope로 접근 가능한 오브젝트를 확인하세요",
    TIMEOUT: "count_rows/partition_info로 규모·파티션 키를 확인한 뒤 조건을 축소하세요",
    INVALID_SQL: "오류 위치를 확인하고 SELECT/WITH 단일 문장만 전달하세요",
    UNKNOWN_OBJECT: "search_catalog 또는 catalog_refresh로 식별자를 확인하세요",
    BAD_PARAM: "해당 파라미터 형식을 확인하세요",
    NO_STATS: "table_profile mode=live 또는 count_rows를 사용하세요",
}


class ToolError(Exception):
    """Base error for every DB tool. Subclasses pin a stable ``code``."""

    code: str = BAD_PARAM
    default_hint: str = _DEFAULT_HINTS[BAD_PARAM]

    def __init__(
        self,
        message: str,
        *,
        ora: str | None = None,
        hint: str | None = None,
        position: int | None = None,
    ) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("ToolError message must be a non-empty string")
        if self.code not in ERROR_CODES:
            raise ValueError(f"unstable error code: {self.code!r}")
        self.message = message.strip()
        self.ora = ora if ora else None
        self.hint = (hint if hint else self.default_hint).strip()
        self.position = position
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        """Return the ``{error: {...}}`` envelope for this instance."""
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
        }
        if self.ora:
            error["ora"] = self.ora
        return {"error": error}


class NoAccessError(ToolError):
    """권한·구성 부재로 대상에 접근할 수 없음. 재시도해도 같다."""

    code = NO_ACCESS
    default_hint = _DEFAULT_HINTS[NO_ACCESS]


class TimeoutError(ToolError):
    """Query timeout. Distinct from :class:`builtins.TimeoutError` when imported from this module."""

    code = TIMEOUT
    default_hint = _DEFAULT_HINTS[TIMEOUT]


class InvalidSqlError(ToolError):
    """SELECT-only 문법 위반 또는 허용되지 않은 구문. ``position`` 을 함께 준다."""

    code = INVALID_SQL
    default_hint = _DEFAULT_HINTS[INVALID_SQL]

    def __init__(
        self,
        message: str,
        *,
        ora: str | None = None,
        hint: str | None = None,
        position: int | None = None,
    ) -> None:
        if position is not None and "position" not in message.lower():
            message = f"{message} (position {position})"
        super().__init__(message, ora=ora, hint=hint, position=position)


class UnknownObjectError(ToolError):
    """카탈로그에 없는 식별자. 최근 생성이면 catalog_refresh 후 재시도."""

    code = UNKNOWN_OBJECT
    default_hint = _DEFAULT_HINTS[UNKNOWN_OBJECT]


class BadParamError(ToolError):
    """도구 인자가 계약에 맞지 않음. DB 에 도달하기 전에 실패한다."""

    code = BAD_PARAM
    default_hint = _DEFAULT_HINTS[BAD_PARAM]


class NoStatsError(ToolError):
    """옵티마이저 통계가 없거나 낡아 근사값을 낼 수 없음."""

    code = NO_STATS
    default_hint = _DEFAULT_HINTS[NO_STATS]


_CODE_TO_CLASS: Mapping[str, type[ToolError]] = {
    NO_ACCESS: NoAccessError,
    TIMEOUT: TimeoutError,
    INVALID_SQL: InvalidSqlError,
    UNKNOWN_OBJECT: UnknownObjectError,
    BAD_PARAM: BadParamError,
    NO_STATS: NoStatsError,
}


def error_class(code: str) -> type[ToolError]:
    """Return the exception class for a stable code. Unknown codes fail closed."""
    try:
        return _CODE_TO_CLASS[code]
    except KeyError:
        raise BadParamError(
            f"알 수 없는 오류 코드: {code!r}",
            hint="정의된 오류 코드만 사용하세요",
        ) from None


def serialize_error(error: BaseException) -> dict[str, Any]:
    """Serialize a :class:`ToolError` as ``{error:{code,ora?,message,hint}}``.

    Non-:class:`ToolError` values fail closed. ``ora`` is included only when set.
    """
    if not isinstance(error, ToolError):
        raise TypeError(
            f"serialize_error requires ToolError, got {type(error).__name__}"
        )
    return error.to_payload()


def _ora_code(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for value in (
            getattr(current, "ora", None),
            getattr(current, "full_code", None),
            str(current),
        ):
            if isinstance(value, str):
                match = _CODE_RE.search(value)
                if match:
                    return match.group(0).upper()
        current = current.__cause__ or current.__context__
    return None


def _is_adapter(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "AdapterError":
            return True
        if isinstance(current, ImportError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _looks_connect(exc: BaseException, code: str | None) -> bool:
    if code:
        upper = code.upper()
        if upper in _TIMEOUT_FULL_CODES:
            return True
        if upper.startswith("ORA-"):
            digits = upper[4:]
            if digits.isdigit() and int(digits) in _ACCESS_ORA_CODES:
                return True
    text = str(exc).lower()
    return any(hint in text for hint in _CONNECT_HINTS)


def _with_code(message: str, code: str | None) -> str:
    return f"{message} ({code})" if code else message


def error_code(source: BaseException | str | None) -> str | None:
    """ORA/DPI/DPY code for the status table. None when unknown."""
    if source is None:
        return None
    if isinstance(source, str):
        match = _CODE_RE.search(source)
        return match.group(0).upper() if match else None
    return _ora_code(source)


def user_message(exc: BaseException) -> str:
    """Short Korean UI guidance. Never includes exception or driver prose."""
    code = _ora_code(exc)
    if isinstance(exc, (TimeoutError, builtins.TimeoutError)):
        return _with_code("응답 시간을 초과했습니다", code)
    if isinstance(exc, InvalidSqlError):
        return _with_code("SQL을 실행할 수 없습니다", code)
    if isinstance(exc, NoAccessError) or _looks_connect(exc, code):
        return _with_code(
            "접속할 수 없습니다. 호스트·서비스명·계정을 확인하세요",
            code,
        )
    if _is_adapter(exc):
        return _with_code("DB 드라이버를 사용할 수 없습니다", code)
    return _with_code("연결에 실패했습니다", code)


__all__ = (
    "NO_ACCESS",
    "TIMEOUT",
    "INVALID_SQL",
    "UNKNOWN_OBJECT",
    "BAD_PARAM",
    "NO_STATS",
    "ERROR_CODES",
    "ToolError",
    "NoAccessError",
    "TimeoutError",
    "InvalidSqlError",
    "UnknownObjectError",
    "BadParamError",
    "NoStatsError",
    "error_class",
    "serialize_error",
    "user_message",
    "error_code",
)
