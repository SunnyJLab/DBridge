"""partition_info tool extracted from analysis_tools."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from ..context import ToolContext
from ..errors import (
    BadParamError,
)
from ..registry import ParamSpec, ToolDefinition

from ..common.results import (
    as_date_str,
    as_int,
    as_text,
)
from ..common.params import (
    bounded_limit,
    require_nonempty_string,
)
from ..common.execution import query_maps
from ..common.catalog_access import (
    owner_name,
    require_object,
    require_owner,
)

_RECENT_CAP = 500
_SCHEMA_TABLE_CAP = 500

_TO_DATE_ISO = re.compile(
    r"TO_DATE\s*\(\s*'(\d{4}-\d{2}-\d{2})(?:[\sT]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?'",
    re.IGNORECASE,
)
_TIMESTAMP_ISO = re.compile(
    r"TIMESTAMP\s+'(\d{4}-\d{2}-\d{2})(?:[\sT]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?'",
    re.IGNORECASE,
)
_BARE_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[\sT].*)?$")

_PARTITION_INFO_DESCRIPTION = (
    "`table`을 주면 파티션 유형·키 컬럼·최근 파티션 경계(`HIGH_VALUE`)·행수·통계일을, "
    "`table`을 생략하고 `owner`만 주면 스키마 내 파티션 테이블 전체의 요약과 "
    "**누락 구간(`gaps`)·빈 파티션(`empty_partitions`)·미분석 파티션**을 반환한다. "
    "대형 테이블에 WHERE를 쓰기 전 `key_columns`를 확인해 조건에 직접 넣어라. "
    "통계 근사치이며 실시간 적재 상태가 아니다."
)


def _parse_high_value(raw: Any) -> tuple[str, date | None]:
    if raw is None:
        return "", None
    if isinstance(raw, datetime):
        return raw.date().isoformat(), raw.date()
    if isinstance(raw, date):
        return raw.isoformat(), raw
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return "", None
    text = str(raw).strip()
    if not text:
        return "", None
    match = _TO_DATE_ISO.search(text) or _TIMESTAMP_ISO.search(text) or _BARE_ISO_DATE.match(text)
    if match is None:
        return text, None
    iso = match.group(1)
    try:
        parsed = date.fromisoformat(iso)
    except ValueError:
        return text, None
    return iso, parsed


def _mode_delta(deltas: list[int]) -> int | None:
    ranked = Counter(item for item in deltas if item > 0).most_common(2)
    if not ranked:
        return None
    if len(ranked) == 2 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _gap_labels(prev: date, curr: date, delta_days: int) -> list[str]:
    if delta_days <= 0 or curr <= prev:
        return []
    monthly = 28 <= delta_days <= 31
    yearly = 365 <= delta_days <= 366
    labels: list[str] = []
    cursor = prev
    guard = 0
    while True:
        guard += 1
        if guard > 2400:
            return []
        if monthly:
            month = cursor.month + 1
            year = cursor.year
            if month > 12:
                month = 1
                year += 1
            try:
                cursor = date(year, month, min(cursor.day, 28))
            except ValueError:
                return []
        elif yearly:
            try:
                cursor = date(cursor.year + 1, cursor.month, cursor.day)
            except ValueError:
                return []
        else:
            cursor = cursor + timedelta(days=delta_days)
        if cursor >= curr:
            break
        if monthly:
            labels.append(f"{cursor.year:04d}-{cursor.month:02d}")
        elif yearly:
            labels.append(f"{cursor.year:04d}")
        else:
            labels.append(cursor.isoformat())
    return labels


def _partition_issues(
    parts: list[dict[str, Any]],
    *,
    truncated: bool,
) -> tuple[list[dict[str, Any]], list[str], list[str], int, str | None]:
    parts.sort(key=lambda item: as_int(item.get("position")) or 0)
    ordered = parts
    issues: list[dict[str, Any]] = []
    empty: list[str] = []
    unanalyzed = 0
    latest_high: str | None = None
    if not ordered:
        return issues, [], empty, unanalyzed, latest_high

    latest = ordered[-1]
    latest_high = as_text(latest.get("high_value"))
    latest_name = as_text(latest.get("name"))
    latest_rows = latest.get("num_rows")
    if latest_rows == 0 and latest_name:
        issues.append(
            {
                "code": "EMPTY_LATEST",
                "partition": latest_name,
                "hint": "최신 파티션이 비어 있음 — 적재 전이거나 누락",
            }
        )
    for item in ordered:
        name = as_text(item.get("name"))
        if item.get("num_rows") == 0 and name:
            empty.append(name)
        if item.get("last_analyzed") is None:
            unanalyzed += 1
    if unanalyzed:
        issues.append(
            {
                "code": "UNANALYZED",
                "hint": f"LAST_ANALYZED가 없는 파티션이 {unanalyzed}개 있습니다",
            }
        )

    gaps: list[str] = []
    if not truncated:
        dated: list[tuple[date, dict[str, Any]]] = []
        for item in ordered:
            parsed = item.get("_parsed_high")
            if isinstance(parsed, date):
                dated.append((parsed, item))
        deltas = [ (dated[i][0] - dated[i - 1][0]).days for i in range(1, len(dated)) ]
        mode = _mode_delta(deltas)
        if mode is not None and len(dated) >= 2:
            for index in range(1, len(dated)):
                prev_date, _prev = dated[index - 1]
                curr_date, _curr = dated[index]
                span = (curr_date - prev_date).days
                if span >= mode * 2:
                    gaps.extend(_gap_labels(prev_date, curr_date, mode))
            if gaps:
                issues.append(
                    {
                        "code": "GAP",
                        "hint": "DATE 경계 파티션에서 최빈 간격의 2배 이상인 구간이 있습니다",
                    }
                )
            latest_date = dated[-1][0]
            today = date.today()
            if (today - latest_date).days >= mode:
                issues.append(
                    {
                        "code": "STALE_LATEST",
                        "hint": f"최신 경계가 {latest_date.isoformat()} — 최근 파티션이 없습니다",
                    }
                )
    for item in ordered:
        item.pop("_parsed_high", None)
    return issues, gaps, empty, unanalyzed, latest_high


def _load_part_tables(
    context: ToolContext,
    connection: str,
    owner: str,
    table: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    sql = (
        "SELECT OWNER, TABLE_NAME, PARTITIONING_TYPE, SUBPARTITIONING_TYPE, "
        "PARTITION_COUNT, INTERVAL FROM ALL_PART_TABLES "
        "WHERE OWNER = :owner"
    )
    binds: dict[str, Any] = {"owner": owner}
    if table is not None:
        sql += " AND TABLE_NAME = :table_name"
        binds["table_name"] = table
    return query_maps(context, connection, sql, binds)


def _load_part_keys(
    context: ToolContext,
    connection: str,
    owner: str,
    table: str | None,
) -> dict[str, list[str]]:
    sql = (
        "SELECT OWNER, NAME, COLUMN_NAME, COLUMN_POSITION FROM ALL_PART_KEY_COLUMNS "
        "WHERE OWNER = :owner AND OBJECT_TYPE = :object_type"
    )
    binds: dict[str, Any] = {"owner": owner, "object_type": "TABLE"}
    if table is not None:
        sql += " AND NAME = :table_name"
        binds["table_name"] = table
    rows, _truncated = query_maps(context, connection, sql, binds)
    grouped: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        name = as_text(row.get("NAME"))
        col = as_text(row.get("COLUMN_NAME"))
        if not name or not col:
            continue
        pos = as_int(row.get("COLUMN_POSITION")) or 0
        grouped.setdefault(name, []).append((pos, col))
    out: dict[str, list[str]] = {}
    for name, pairs in grouped.items():
        pairs.sort(key=lambda item: item[0])
        out[name] = [col for _pos, col in pairs]
    return out


def _load_tab_partitions(
    context: ToolContext,
    connection: str,
    owner: str,
    table: str | None,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    sql = (
        "SELECT TABLE_NAME, PARTITION_NAME, HIGH_VALUE, PARTITION_POSITION, "
        "NUM_ROWS, LAST_ANALYZED FROM ALL_TAB_PARTITIONS WHERE TABLE_OWNER = :owner"
    )
    binds: dict[str, Any] = {"owner": owner}
    if table is not None:
        sql += " AND TABLE_NAME = :table_name"
        binds["table_name"] = table
    rows, truncated = query_maps(context, connection, sql, binds)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        name = as_text(row.get("TABLE_NAME"))
        part_name = as_text(row.get("PARTITION_NAME"))
        if not name or not part_name:
            continue
        display, parsed = _parse_high_value(row.get("HIGH_VALUE"))
        item = {
            "name": part_name,
            "high_value": display if display else as_text(row.get("HIGH_VALUE")),
            "position": as_int(row.get("PARTITION_POSITION")),
            "num_rows": as_int(row.get("NUM_ROWS")),
            "last_analyzed": as_date_str(row.get("LAST_ANALYZED")),
            "_parsed_high": parsed,
        }
        grouped.setdefault(name, []).append(item)
    return grouped, truncated


def _subpartition_type(value: Any) -> str | None:
    text = as_text(value)
    if text is None or text.upper() == "NONE":
        return None
    return text


def partition_info(
    context: ToolContext,
    *,
    connection: str,
    table: str | None = None,
    owner: str | None = None,
    recent: int = 12,
    only_issues: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    context.get_connection(connection)
    if table is None and owner is None:
        raise BadParamError(
            "table 또는 owner가 필요합니다",
            hint="단일 테이블은 OWNER.TABLE, 스키마 점검은 owner를 전달하세요",
        )
    recent_n = bounded_limit(recent, cap=_RECENT_CAP, name="recent")
    limit_n = bounded_limit(limit, cap=_SCHEMA_TABLE_CAP, name="limit")

    if table is not None:
        require_nonempty_string(table, "table")
        obj = require_object(context.catalog, connection, table)
        owner_id, table_name = owner_name(obj, table)
        meta_rows, _meta_trunc = _load_part_tables(context, connection, owner_id, table_name)
        if not meta_rows:
            return {
                "owner": owner_id,
                "table": table_name,
                "partitioned": False,
                "type": None,
                "interval": None,
                "key_columns": [],
                "subpartition_type": None,
                "partition_count": 0,
                "recent": [],
                "issues": [],
                "source": "stats",
                "hint": "파티션 테이블이 아닙니다",
            }
        keys = _load_part_keys(context, connection, owner_id, table_name)
        parts_by_table, parts_trunc = _load_tab_partitions(context, connection, owner_id, table_name)
        parts = parts_by_table.get(table_name, [])
        meta = meta_rows[0]
        part_type = as_text(meta.get("PARTITIONING_TYPE"))
        interval = as_text(meta.get("INTERVAL"))
        key_columns = keys.get(table_name, [])
        issues, _gaps, _empty, _unanalyzed, _latest = _partition_issues(parts, truncated=parts_trunc)
        recent_parts = []
        for item in reversed(parts[-recent_n:]):
            recent_parts.append(
                {
                    "name": item.get("name"),
                    "high_value": item.get("high_value"),
                    "position": item.get("position"),
                    "num_rows": item.get("num_rows"),
                    "last_analyzed": item.get("last_analyzed"),
                }
            )
        key_hint = key_columns[0] if key_columns else "파티션 키"
        return {
            "owner": owner_id,
            "table": table_name,
            "partitioned": True,
            "type": part_type,
            "interval": interval,
            "key_columns": key_columns,
            "subpartition_type": _subpartition_type(meta.get("SUBPARTITIONING_TYPE")),
            "partition_count": as_int(meta.get("PARTITION_COUNT")) or len(parts),
            "recent": recent_parts,
            "truncated": parts_trunc or _meta_trunc,
            "issues": issues,
            "source": "stats",
            "hint": (
                f"WHERE {key_hint} >= :from AND {key_hint} < :to 형태로 "
                "키 컬럼을 직접 조건에 넣으세요"
            ),
        }

    owner_id = require_owner(context.catalog, connection, require_nonempty_string(owner, "owner"))
    meta_rows, _meta_trunc = _load_part_tables(context, connection, owner_id, None)
    keys = _load_part_keys(context, connection, owner_id, None)
    parts_by_table, parts_trunc = _load_tab_partitions(context, connection, owner_id, None)
    tables_out: list[dict[str, Any]] = []
    truncated = False
    for meta in sorted(meta_rows, key=lambda row: as_text(row.get("TABLE_NAME")) or ""):
        table_name = as_text(meta.get("TABLE_NAME"))
        if not table_name:
            continue
        parts = parts_by_table.get(table_name, [])
        issues, gaps, empty, unanalyzed, latest_high = _partition_issues(
            parts, truncated=parts_trunc
        )
        if only_issues and not issues and not gaps and not empty and unanalyzed == 0:
            continue
        if len(tables_out) >= limit_n:
            truncated = True
            break
        entry: dict[str, Any] = {
            "table": table_name,
            "type": as_text(meta.get("PARTITIONING_TYPE")),
            "key_columns": keys.get(table_name, []),
            "partition_count": as_int(meta.get("PARTITION_COUNT")) or len(parts),
            "latest_high_value": latest_high,
            "gaps": gaps,
            "empty_partitions": empty,
            "unanalyzed": unanalyzed,
        }
        extra_issues = [item for item in issues if item.get("code") in {"STALE_LATEST", "EMPTY_LATEST"}]
        if extra_issues:
            entry["issues"] = extra_issues
        tables_out.append(entry)

    return {
        "owner": owner_id,
        "partitioned_tables": len(meta_rows),
        "tables": tables_out,
        "truncated": truncated or _meta_trunc or parts_trunc,
        "source": "stats",
    }


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="partition_info",
        description=_PARTITION_INFO_DESCRIPTION,
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("table", "string", required=False),
            ParamSpec("owner", "string", required=False),
            ParamSpec("recent", "int", required=False, default=12),
            ParamSpec("only_issues", "bool", required=False, default=False),
            ParamSpec("limit", "int", required=False, default=100),
        ),
        example={
            "name": "partition_info",
            "arguments": {"connection": "dw-prod", "table": "DW.F_SALES", "recent": 12},
        },
        handler=partition_info,
    )


__all__ = ["definition", "partition_info"]
