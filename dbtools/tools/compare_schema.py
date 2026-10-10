"""Compare schemas or a single table across two connections."""

from __future__ import annotations

import re
from typing import Any, Sequence

from ..catalog import LIST_MAX_LIMIT
from ..context import ToolContext
from ..errors import BadParamError
from ..registry import ParamSpec, ToolDefinition
from ..sql_safety import validate_identifier

from ..common.catalog_access import (
    LOB_TYPES,
    list_owner_objects,
    object_columns,
    require_object,
    require_owner,
)
from ..common.params import bounded_int, require_nonempty_string

_TYPE_LEN = re.compile(
    r"^([A-Z][A-Z0-9_ ]*?)(?:\((\d+)(?:,\s*(-?\d+))?\))?(?:\s+(.*))?$"
)


_NUMERIC_TYPES = frozenset(
    {"NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "INTEGER", "INT"}
)
_STRING_TYPES = frozenset(
    {"CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2", "VARCHAR", "CLOB", "NCLOB"}
)
_DATE_TYPES = frozenset(
    {"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE"}
)
_COMPARE_FIELDS = frozenset({"columns", "types", "nullable", "comments"})


def _parse_type(formatted: str) -> tuple[str, int | None, int | None]:
    text = (formatted or "").strip().upper()
    if not text:
        return "", None, None
    match = _TYPE_LEN.match(text)
    if not match:
        return text, None, None
    base = match.group(1).strip()
    extra = match.group(4)
    if extra:
        base = f"{base} {extra.strip()}".strip()
    length = int(match.group(2)) if match.group(2) else None
    scale = int(match.group(3)) if match.group(3) else None
    return base, length, scale


def _type_family(base: str) -> str:
    if base in _NUMERIC_TYPES or base.startswith("NUMBER") or base.startswith("FLOAT"):
        return "number"
    if base in LOB_TYPES:
        return "lob"
    if base in _STRING_TYPES or base.startswith("VARCHAR") or base.startswith("CHAR"):
        return "string"
    if base in _DATE_TYPES or base.startswith("TIMESTAMP") or base.startswith("DATE"):
        return "date"
    return base


def _type_hint(left: str, right: str) -> str | None:
    lbase, llen, lscale = _parse_type(left)
    rbase, rlen, rscale = _parse_type(right)
    lfam = _type_family(lbase)
    rfam = _type_family(rbase)
    if lfam and rfam and lfam != rfam:
        return "형변환 필요"
    if lbase == rbase == "NUMBER":
        if llen is None:
            return "우측 길이 부족 — 절단 위험" if rlen is not None else None
        if rlen is None:
            return None
        left_scale = lscale or 0
        right_scale = rscale or 0
        if right_scale < left_scale or rlen - right_scale < llen - left_scale:
            return "우측 길이 부족 — 절단 위험"
    if llen is not None and rlen is not None and rlen < llen:
        return "우측 길이 부족 — 절단 위험"
    if lscale is not None and rscale is not None and rscale < lscale:
        return "우측 길이 부족 — 절단 위험"
    return None


def _side_spec(raw: Any, label: str) -> tuple[str, str]:
    if not isinstance(raw, dict):
        raise BadParamError(
            f"{label}는 객체여야 합니다",
            hint='{"connection": "...", "owner": "..."} 형식입니다',
        )
    extra = set(raw) - {"connection", "owner"}
    if extra:
        raise BadParamError(
            f"{label}에 알 수 없는 키: {sorted(extra)[0]}",
            hint="connection/owner 만 허용됩니다",
        )
    connection = require_nonempty_string(raw.get("connection"), f"{label}.connection")
    owner = validate_identifier(
        require_nonempty_string(raw.get("owner"), f"{label}.owner"),
        max_parts=1,
    )
    return connection, owner


def _compare_table(
    left_cols: Sequence[dict[str, Any]],
    right_cols: Sequence[dict[str, Any]],
    *,
    compare: frozenset[str],
    ignore: frozenset[str],
) -> dict[str, Any]:
    left_map = {col["name"]: col for col in left_cols if col["name"] not in ignore}
    right_map = {col["name"]: col for col in right_cols if col["name"] not in ignore}
    left_names = list(left_map)
    right_names = list(right_map)
    only_left = []
    only_right = []
    if "columns" in compare:
        for name in left_names:
            if name not in right_map:
                col = left_map[name]
                only_left.append({"column": name, "type": col["type"]})
        for name in right_names:
            if name not in left_map:
                col = right_map[name]
                only_right.append({"column": name, "type": col["type"]})
    type_diff = []
    nullable_diff = []
    comment_diff = []
    common = [name for name in left_names if name in right_map]
    for name in common:
        lcol = left_map[name]
        rcol = right_map[name]
        if "types" in compare and lcol["type"] != rcol["type"]:
            item = {"column": name, "left": lcol["type"], "right": rcol["type"]}
            hint = _type_hint(lcol["type"], rcol["type"])
            if hint:
                item["hint"] = hint
            type_diff.append(item)
        if "nullable" in compare and bool(lcol["nullable"]) != bool(rcol["nullable"]):
            nullable_diff.append(
                {"column": name, "left": bool(lcol["nullable"]), "right": bool(rcol["nullable"])}
            )
        if "comments" in compare and (lcol.get("comment") or None) != (rcol.get("comment") or None):
            comment_diff.append(
                {
                    "column": name,
                    "left": lcol.get("comment"),
                    "right": rcol.get("comment"),
                }
            )
    right_order = [name for name in right_names if name in left_map]
    order_diff = "columns" in compare and common != right_order
    identical = (
        not only_left
        and not only_right
        and not type_diff
        and not nullable_diff
        and not comment_diff
        and not order_diff
    )
    payload: dict[str, Any] = {
        "identical": identical,
        "order_diff": order_diff,
    }
    if "columns" in compare:
        payload["only_left"] = only_left
        payload["only_right"] = only_right
    if "types" in compare:
        payload["type_diff"] = type_diff
    if "nullable" in compare:
        payload["nullable_diff"] = nullable_diff
    if "comments" in compare:
        payload["comment_diff"] = comment_diff
    return payload


def compare_schema(
    context: ToolContext,
    *,
    left: dict[str, Any],
    right: dict[str, Any],
    table: str | None = None,
    compare: list[Any] | None = None,
    ignore_columns: list[Any] | None = None,
    only_diff: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    left_conn, left_owner = _side_spec(left, "left")
    right_conn, right_owner = _side_spec(right, "right")
    require_owner(context.catalog, left_conn, left_owner)
    require_owner(context.catalog, right_conn, right_owner)
    if compare is None:
        fields = ["columns", "types", "nullable"]
    else:
        if not isinstance(compare, list) or not compare:
            raise BadParamError("compare는 비어 있지 않은 배열이어야 합니다")
        fields = []
        for item in compare:
            if not isinstance(item, str) or item not in _COMPARE_FIELDS:
                raise BadParamError(
                    f"compare 항목이 올바르지 않습니다: {item!r}",
                    hint="columns/types/nullable/comments 만 허용됩니다",
                )
            if item not in fields:
                fields.append(item)
    compare_set = frozenset(fields)
    ignore: set[str] = set()
    if ignore_columns is not None:
        if not isinstance(ignore_columns, list):
            raise BadParamError("ignore_columns는 배열이어야 합니다")
        for item in ignore_columns:
            if not isinstance(item, str):
                raise BadParamError("ignore_columns 항목은 문자열이어야 합니다")
            ignore.add(validate_identifier(item, max_parts=1))
    ignore_set = frozenset(ignore)
    cap = bounded_int(limit, cap=LIST_MAX_LIMIT, name="limit")
    timestamps = context.catalog.cache_timestamps()
    left_ts = timestamps.get(left_conn)
    right_ts = timestamps.get(right_conn)

    if table is not None:
        table_name = validate_identifier(table, max_parts=1)
        left_obj = require_object(context.catalog, left_conn, f"{left_owner}.{table_name}")
        right_obj = require_object(context.catalog, right_conn, f"{right_owner}.{table_name}")
        left_cols = object_columns(left_obj)
        right_cols = object_columns(right_obj)
        diff = _compare_table(left_cols, right_cols, compare=compare_set, ignore=ignore_set)
        payload = {
            "table": table_name,
            "left": {
                "connection": left_conn,
                "owner": left_owner,
                "columns": len(left_cols),
                "cache_refreshed_at": left_ts,
            },
            "right": {
                "connection": right_conn,
                "owner": right_owner,
                "columns": len(right_cols),
                "cache_refreshed_at": right_ts,
            },
        }
        payload.update(diff)
        return payload

    left_list, left_trunc = list_owner_objects(
        context.catalog, left_conn, left_owner, limit=LIST_MAX_LIMIT
    )
    right_list, right_trunc = list_owner_objects(
        context.catalog, right_conn, right_owner, limit=LIST_MAX_LIMIT
    )
    left_names = {item["name"] for item in left_list}
    right_names = {item["name"] for item in right_list}
    only_left_tables = [] if right_trunc else sorted(left_names - right_names)
    only_right_tables = [] if left_trunc else sorted(right_names - left_names)
    common = sorted(left_names & right_names)
    different_rows: list[dict[str, Any]] = []
    identical_count = 0
    truncated = left_trunc or right_trunc
    for name in common:
        left_obj = require_object(context.catalog, left_conn, f"{left_owner}.{name}")
        right_obj = require_object(context.catalog, right_conn, f"{right_owner}.{name}")
        diff = _compare_table(
            object_columns(left_obj),
            object_columns(right_obj),
            compare=compare_set,
            ignore=ignore_set,
        )
        if diff["identical"]:
            identical_count += 1
            if only_diff:
                continue
        if len(different_rows) >= cap:
            truncated = True
            continue
        row = {
            "table": name,
            "only_left": len(diff.get("only_left") or []),
            "only_right": len(diff.get("only_right") or []),
            "type_diff": len(diff.get("type_diff") or []),
            "nullable_diff": len(diff.get("nullable_diff") or []),
            "order_diff": bool(diff["order_diff"]),
        }
        if "comments" in compare_set:
            row["comment_diff"] = len(diff.get("comment_diff") or [])
        different_rows.append(row)
    if len(only_left_tables) > cap:
        only_left_tables = only_left_tables[:cap]
        truncated = True
    if len(only_right_tables) > cap:
        only_right_tables = only_right_tables[:cap]
        truncated = True
    payload = {
        "left": {
            "connection": left_conn,
            "owner": left_owner,
            "tables": len(left_names),
            "cache_refreshed_at": left_ts,
        },
        "right": {
            "connection": right_conn,
            "owner": right_owner,
            "tables": len(right_names),
            "cache_refreshed_at": right_ts,
        },
        "common": len(common),
        "identical": identical_count,
        "different": len(common) - identical_count,
        "tables": different_rows,
        "only_left_tables": only_left_tables,
        "only_right_tables": only_right_tables,
        "truncated": truncated,
        "hint": (
            "결과 상한 내에서만 표시했습니다. 상세는 table을 지정해 다시 호출하세요"
            if truncated
            else "상세는 table을 지정해 다시 호출하세요"
        ),
    }
    return payload


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="compare_schema",
        description=(
            "left/right 스키마의 테이블·뷰 구조를 비교한다. `table`을 주면 컬럼·타입·NULL·코멘트 차이를, "
            "생략하면 동일 이름 객체의 차이와 한쪽에만 있는 객체를 반환한다. 캐시 기반이므로 최신 구조가 "
            "필요하면 먼저 `catalog_refresh`를 호출하라."
        ),
        params=(
            ParamSpec("left", "dict", required=True),
            ParamSpec("right", "dict", required=True),
            ParamSpec("table", "string", required=False),
            ParamSpec("compare", "list", required=False, default=["columns", "types", "nullable"]),
            ParamSpec("ignore_columns", "list", required=False, default=[]),
            ParamSpec("only_diff", "bool", required=False, default=True),
            ParamSpec("limit", "int", required=False, default=200),
        ),
        example={
            "name": "compare_schema",
            "arguments": {
                "left": {"connection": "erp-src", "owner": "CRM"},
                "right": {"connection": "dw-prod", "owner": "STG"},
                "table": "TB_CUSTOMER",
                "ignore_columns": ["ETL_LOAD_DT"],
            },
        },
        handler=compare_schema,
    )


__all__ = ["definition", "compare_schema"]
