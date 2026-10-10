"""Driver-output shaping: rows, columns, scalar coercion, and ORA error fields.

드라이버가 돌려준 것을 해석하는 계층이다 — 성공 결과(행·컬럼·스칼라)와
실패 결과(ORA 코드·메시지·위치)를 같은 곳에서 다룬다.

Leaf module: ``dbtools.errors`` 외에는 아무것도 import 하지 않는다. 그래서
``catalog``(하위 계층)도 이 모듈을 가져올 수 있다.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Mapping

from ..errors import ToolError


_INT_TEXT = re.compile(r"^-?\d+$")


def attr_or_key(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping or an attribute of the same name."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def query_payload(result: Any, *, max_rows: int | None = None) -> dict[str, Any]:
    """Shape a driver result into the ``{columns, rows, row_count, truncated}`` contract."""
    columns = list(getattr(result, "columns"))
    raw_rows = result.rows
    truncated = bool(getattr(result, "truncated"))
    elapsed_ms = int(getattr(result, "elapsed_ms"))
    if max_rows is not None and len(raw_rows) > max_rows:
        raw_rows = raw_rows[:max_rows]
        truncated = True
    rows = [list(row) for row in raw_rows]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
    }


def row_maps(result: Any) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(rows as upper-cased dicts, truncated)`` for dictionary-view queries."""
    columns = tuple(str(name).upper() for name in getattr(result, "columns", ()))
    rows_out: list[dict[str, Any]] = []
    for raw in getattr(result, "rows", ()):
        mapped: dict[str, Any] = {}
        for index, col in enumerate(columns):
            mapped[col] = raw[index] if index < len(raw) else None
        rows_out.append(mapped)
    truncated = bool(getattr(result, "truncated", False))
    return rows_out, truncated


def as_int(value: Any) -> int | None:
    """Coerce to int only when exact — 1.5 는 조용히 1 로 절단하지 않고 None 이다."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if _INT_TEXT.match(text):
        return int(text)
    return None


def as_text(value: Any) -> str | None:
    """Coerce to a stripped string, or ``None`` for empty/missing values."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    text = str(value).strip()
    return text if text else None


def as_date_str(value: Any) -> str | None:
    """Format a date/datetime/date-text as ISO text, or ``None`` when absent."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.microsecond:
            return value.isoformat(sep=" ", timespec="microseconds")
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    text = as_text(value)
    if text is None:
        return None
    if " " in text:
        return text.split(".")[0]
    return text[:10] if len(text) >= 10 and text[4] == "-" else text


def ora_fields(exc: BaseException) -> tuple[str | None, str, int | None]:
    """Extract ``(ORA-xxxxx, message, position)`` by walking the exception chain.

    ``__cause__``/``__context__`` 를 따라가므로 AdapterError 로 감싸인 드라이버
    오류에서도 원래 ORA 코드를 찾아낸다.
    """
    current: BaseException | None = exc
    ora: str | None = None
    message = str(exc).strip() or type(exc).__name__
    position: int | None = None
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ToolError):
            ora = current.ora or ora
            message = current.message or message
            if isinstance(current.position, int) and not isinstance(current.position, bool):
                position = current.position
        args0 = current.args[0] if current.args else None
        for obj in (current, args0):
            if obj is None:
                continue
            full = getattr(obj, "full_code", None) or getattr(obj, "ora", None)
            if isinstance(full, str) and full.strip():
                ora = full.strip()
            code = getattr(obj, "code", None)
            if ora is None and isinstance(code, int) and not isinstance(code, bool):
                ora = f"ORA-{code:05d}"
            offset = getattr(obj, "offset", None)
            if offset is None:
                offset = getattr(obj, "position", None)
            if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
                position = offset
            raw_message = getattr(obj, "message", None)
            if isinstance(raw_message, str) and raw_message.strip():
                message = raw_message.strip()
        current = current.__cause__ or current.__context__
    if ora is None:
        upper = message.upper()
        start = upper.find("ORA-")
        if start >= 0:
            token = [message[start : start + 4]]
            i = start + 4
            while i < len(message) and message[i].isdigit():
                token.append(message[i])
                i += 1
            if len(token) > 1:
                ora = "".join(token)
    return ora, message, position


__all__ = [
    "as_date_str",
    "as_int",
    "as_text",
    "attr_or_key",
    "ora_fields",
    "query_payload",
    "row_maps",
]
