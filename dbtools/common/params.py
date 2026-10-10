"""Parameter validation and bind-name normalization shared across dbtools families.

Standalone: no imports of other ``dbtools.common`` peer modules.
"""

from __future__ import annotations

import re
from typing import Any, TypeVar, Mapping

from ..context import ToolContext
from ..database import _normalize_binds as normalize_binds_lenient
from ..errors import BadParamError

from ..extensions import extract_bind_names
from ..sql_safety import ROW_LIMIT_BIND, validate_select_sql

# ``normalize_binds_lenient`` 는 database 계층의 구현을 그대로 재수출한다.
# 어댑터가 받아들이는 바인드 이름 규칙과 도구가 검사하는 규칙이 갈라지면 안 된다.


_T = TypeVar("_T", int, float)
_BIND_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


# 운영자가 조정하는 도구 정책값. transport.config._validate_tools 가 범위를 검증하므로
# 여기서는 타입만 확인하고, 미설정이면 호출부가 넘긴 기본값을 쓴다.
def tool_setting(context: ToolContext, key: str, default: _T) -> _T:
    """Read ``config.tools[key]``, falling back to the caller's default."""
    raw = context.config.get("tools") if isinstance(context.config, Mapping) else None
    if not isinstance(raw, Mapping):
        return default
    value = raw.get(key)
    if value is None or isinstance(value, bool) or not isinstance(value, type(default)):
        return default
    return value


def bounded_limit(value: int, *, cap: int, name: str) -> int:
    """Validate a 1..cap row/result limit, failing closed outside the range."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BadParamError(
            f"{name}는 1 이상의 정수여야 합니다",
            hint=f"{name}에 양의 정수를 전달하세요",
        )
    if value > cap:
        return cap
    return value


def bounded_int(value: int, *, cap: int, name: str, minimum: int = 1) -> int:
    """Like :func:`bounded_limit` but with an explicit lower bound (0 허용용)."""
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise BadParamError(
            f"{name}는 {minimum} 이상의 정수여야 합니다",
            hint=f"{name}에 양의 정수를 전달하세요",
        )
    if value > cap:
        return cap
    return value


def require_nonempty_string(value: Any, name: str) -> str:
    """Return the value unchanged, or raise BAD_PARAM when it is not a filled string."""
    if not isinstance(value, str) or not value.strip():
        raise BadParamError(
            f"{name}이(가) 비어 있습니다",
            hint=f"{name}에 문자열을 전달하세요",
        )
    return value


def normalize_binds_reserved_guard(binds: Mapping[str, Any] | None) -> dict[str, Any]:
    """Strip leading ``:`` and reject the row-cap bind name the proxy reserves."""
    out = normalize_binds_strict_name(binds)
    if any(key.upper() == ROW_LIMIT_BIND.upper() for key in out):
        raise BadParamError(
            f"binds에 예약 이름 {ROW_LIMIT_BIND}는 사용할 수 없습니다",
            hint="다른 바인드 이름을 사용하세요",
        )
    return out


def normalize_binds_strict_name(binds: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate bind names without reserving the proxy row-limit name."""
    if binds is None:
        return {}
    if not isinstance(binds, Mapping):
        raise BadParamError("binds는 객체여야 합니다")
    out: dict[str, Any] = {}
    seen: set[str] = set()
    for raw_key, value in binds.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise BadParamError("바인드 이름이 비어 있습니다")
        key = raw_key.strip()
        if key.startswith(":"):
            key = key[1:]
        if not key or not _BIND_NAME.match(key):
            raise BadParamError(
                f"바인드 이름이 올바르지 않습니다: {raw_key!r}",
                hint=":name 형식의 바인드만 허용됩니다",
            )
        canonical = key.lower()
        if canonical in seen:
            raise BadParamError(f"바인드 이름이 중복됩니다: {key}")
        seen.add(canonical)
        out[key] = value
    return out


def bound_sql(sql: str, binds: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Validate SELECT-only SQL and match its ``:name`` placeholders to ``binds``.

    선언과 실제가 어긋나면(누락·잉여) 실행하지 않고 BAD_PARAM 으로 실패한다.
    """
    stripped = validate_select_sql(sql)
    provided = normalize_binds_strict_name(binds)
    names = extract_bind_names(stripped)
    lower = {key.lower(): key for key in provided}
    mapped: dict[str, Any] = {}
    for name in names:
        key = lower.get(name.lower())
        if key is None:
            raise BadParamError(
                f"바인드가 없습니다: {name}",
                hint="SQL의 :name 값을 binds 객체로 전달하세요",
            )
        mapped[name] = provided[key]
    used = {name.lower() for name in names}
    unused = [key for key in provided if key.lower() not in used]
    if unused:
        raise BadParamError(
            f"SQL에 없는 바인드입니다: {unused[0]}",
            hint="binds 키는 SQL의 :name과 일치해야 합니다",
        )
    return stripped, mapped


__all__ = [
    "tool_setting",
    "bound_sql",
    "bounded_int",
    "bounded_limit",
    "normalize_binds_lenient",
    "normalize_binds_reserved_guard",
    "normalize_binds_strict_name",
    "require_nonempty_string",
]
