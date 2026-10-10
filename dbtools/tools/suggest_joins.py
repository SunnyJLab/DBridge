"""Suggest join candidates from manual relationships, FKs, and names."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..context import ToolContext
from ..errors import (
    BadParamError,
    ToolError,
    UnknownObjectError,
)
from ..registry import ParamSpec, ToolDefinition

from ..common.execution import query_maps
from ..common.catalog_access import (
    list_owner_objects,
    object_columns,
    owner_name,
    require_column,
    require_object,
)

_FK_ROWS_CAP = 5000
_NAME_MATCH_TABLE_CAP = 500
_NAME_MATCH_JOIN_CAP = 50


_SUFFIXES = ("_ID", "_NO", "_CD")
_EXACT_KEYS = frozenset({"ID", "NO", "CD"})


_DIRECTIONS_IN = frozenset({"parents", "children", "both"})
_SOURCE_RANK = {"manual": 0, "fk": 1, "name_match": 2}
_FK_PARENT_SQL = """
SELECT
  c.CONSTRAINT_NAME AS CONSTRAINT_NAME,
  p.OWNER AS OTHER_OWNER,
  p.TABLE_NAME AS OTHER_TABLE,
  cc.COLUMN_NAME AS SRC_COL,
  rc.COLUMN_NAME AS DST_COL,
  cc.POSITION AS POSITION
FROM ALL_CONSTRAINTS c
JOIN ALL_CONS_COLUMNS cc
  ON cc.OWNER = c.OWNER AND cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME
JOIN ALL_CONSTRAINTS p
  ON p.OWNER = c.R_OWNER AND p.CONSTRAINT_NAME = c.R_CONSTRAINT_NAME
JOIN ALL_CONS_COLUMNS rc
  ON rc.OWNER = p.OWNER
 AND rc.CONSTRAINT_NAME = p.CONSTRAINT_NAME
 AND rc.POSITION = cc.POSITION
WHERE c.CONSTRAINT_TYPE = 'R'
  AND c.OWNER = :owner
  AND c.TABLE_NAME = :table_name
ORDER BY c.CONSTRAINT_NAME, cc.POSITION
"""
_FK_CHILD_SQL = """
SELECT
  c.CONSTRAINT_NAME AS CONSTRAINT_NAME,
  c.OWNER AS OTHER_OWNER,
  c.TABLE_NAME AS OTHER_TABLE,
  rc.COLUMN_NAME AS SRC_COL,
  cc.COLUMN_NAME AS DST_COL,
  cc.POSITION AS POSITION
FROM ALL_CONSTRAINTS c
JOIN ALL_CONS_COLUMNS cc
  ON cc.OWNER = c.OWNER AND cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME
JOIN ALL_CONSTRAINTS p
  ON p.OWNER = c.R_OWNER AND p.CONSTRAINT_NAME = c.R_CONSTRAINT_NAME
JOIN ALL_CONS_COLUMNS rc
  ON rc.OWNER = p.OWNER
 AND rc.CONSTRAINT_NAME = p.CONSTRAINT_NAME
 AND rc.POSITION = cc.POSITION
WHERE c.CONSTRAINT_TYPE = 'R'
  AND p.OWNER = :owner
  AND p.TABLE_NAME = :table_name
ORDER BY c.CONSTRAINT_NAME, cc.POSITION
"""


def _confidence_value(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadParamError(
            "min_confidence는 0 이상 1 이하의 숫자여야 합니다",
            hint="0.5 같은 소수를 전달하세요",
        )
    number = float(value)
    if not 0 <= number <= 1:
        raise BadParamError(
            "min_confidence는 0 이상 1 이하의 숫자여야 합니다",
            hint="0.5 같은 소수를 전달하세요",
        )
    return number


def _wanted_directions(direction: str) -> frozenset[str]:
    if direction not in _DIRECTIONS_IN:
        raise BadParamError(
            "direction은 parents, children, both 중 하나여야 합니다",
        )
    if direction == "both":
        return frozenset({"parent", "child"})
    if direction == "parents":
        return frozenset({"parent"})
    return frozenset({"child"})


def _flip_direction(direction: str) -> str:
    if direction == "parent":
        return "child"
    if direction == "child":
        return "parent"
    raise BadParamError(f"direction이 올바르지 않습니다: {direction}")


def _join_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    on = tuple((pair[0], pair[1]) for pair in item["on"])
    return (item["table"], item["direction"], on)


def _validate_on_pairs(
    catalog: Any,
    connection: str,
    left_table: str,
    right_table: str,
    pairs: Sequence[Any],
) -> list[list[str]]:
    if not isinstance(pairs, Sequence) or isinstance(pairs, (str, bytes)) or not pairs:
        raise BadParamError("on은 비어 있지 않은 배열이어야 합니다")
    out: list[list[str]] = []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise BadParamError("on 항목은 [왼쪽컬럼, 오른쪽컬럼] 이어야 합니다")
        left = require_column(catalog, connection, left_table, str(pair[0]))
        right = require_column(catalog, connection, right_table, str(pair[1]))
        out.append([left, right])
    return out


def _collect_fk_joins(
    context: ToolContext,
    connection: str,
    owner: str,
    table: str,
    qualified: str,
    wanted: frozenset[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    catalog = context.catalog
    for sql, direction in ((_FK_PARENT_SQL, "parent"), (_FK_CHILD_SQL, "child")):
        if direction not in wanted:
            continue
        try:
            rows, _truncated = query_maps(
                context,
                connection,
                sql,
                {"owner": owner, "table_name": table},
                max_rows=_FK_ROWS_CAP,
            )
        except ToolError:
            continue
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for mapping in rows:
            constraint = str(mapping.get("CONSTRAINT_NAME") or "")
            other_owner = mapping.get("OTHER_OWNER")
            other_table = mapping.get("OTHER_TABLE")
            src = mapping.get("SRC_COL")
            dst = mapping.get("DST_COL")
            if not all(isinstance(v, str) and v.strip() for v in (other_owner, other_table, src, dst)):
                continue
            other = f"{other_owner}.{other_table}"
            try:
                other_obj = require_object(catalog, connection, other)
                other_qual = ".".join(owner_name(other_obj, other))
                on_pair = _validate_on_pairs(
                    catalog, connection, qualified, other_qual, [[src, dst]]
                )[0]
            except (UnknownObjectError, BadParamError):
                continue
            bucket = grouped.setdefault(
                (other_qual, constraint),
                {
                    "table": other_qual,
                    "on": [],
                    "direction": direction,
                    "source": "fk",
                    "constraint": constraint,
                    "confidence": 1.0,
                },
            )
            if on_pair not in bucket["on"]:
                bucket["on"].append(on_pair)
        out.extend(grouped.values())
    return out


def _name_match_joins(
    catalog: Any,
    connection: str,
    owner: str,
    qualified: str,
    columns: Sequence[dict[str, Any]],
    wanted: frozenset[str],
) -> list[dict[str, Any]]:
    keys = {
        col["name"]
        for col in columns
        if col["name"] in _EXACT_KEYS or col["name"].endswith(_SUFFIXES)
    }
    if not keys:
        return []
    listed, _truncated = list_owner_objects(
        catalog, connection, owner, limit=_NAME_MATCH_TABLE_CAP
    )
    out: list[dict[str, Any]] = []
    for item in listed:
        other = f"{item['owner']}.{item['name']}"
        if other == qualified:
            continue
        try:
            other_obj = require_object(catalog, connection, other)
        except UnknownObjectError:
            continue
        other_cols = {col["name"] for col in object_columns(other_obj)}
        shared = sorted(keys & other_cols)
        if not shared:
            continue
        other_name = item["name"]
        direction = "parent"
        if other_name.startswith(("F_", "FACT_")) or other_name.endswith(("_DTL", "_HIST", "_FCT")):
            direction = "child"
        elif other_name.startswith(("D_", "DIM_")) or other_name.endswith(("_MST", "_DIM", "_D")):
            direction = "parent"
        if direction not in wanted:
            continue
        out.append(
            {
                "table": other,
                "on": [[name, name] for name in shared],
                "direction": direction,
                "source": "name_match",
                "confidence": 0.7,
            }
        )
        if len(out) >= _NAME_MATCH_JOIN_CAP:
            break
    return out


def _merge_joins(items: Iterable[dict[str, Any]], *, min_confidence: float, wanted: frozenset[str]) -> list[dict[str, Any]]:
    chosen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        if item["direction"] not in wanted:
            continue
        if float(item["confidence"]) < min_confidence:
            continue
        key = _join_key(item)
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = item
            continue
        if _SOURCE_RANK.get(item["source"], 9) < _SOURCE_RANK.get(previous["source"], 9):
            chosen[key] = item
    merged = list(chosen.values())
    merged.sort(
        key=lambda row: (
            _SOURCE_RANK.get(row["source"], 9),
            row["table"],
            row["direction"],
            tuple((a, b) for a, b in row["on"]),
        )
    )
    return merged


def suggest_joins(
    context: ToolContext,
    *,
    connection: str,
    table: str,
    direction: str = "both",
    min_confidence: float = 0.5,
) -> dict[str, Any]:
    context.get_connection(connection)
    wanted = _wanted_directions(direction)
    floor = _confidence_value(min_confidence)
    obj = require_object(context.catalog, connection, table)
    owner, name = owner_name(obj, table)
    qualified = f"{owner}.{name}"
    collected: list[dict[str, Any]] = []

    relationships = context.extensions.relationships(connection) if context.extensions else ()
    for rel in relationships:
        payload = rel.to_payload()
        source_table, joins = payload["table"], payload["joins"]
        if not source_table:
            continue
        try:
            source_obj = require_object(context.catalog, connection, source_table)
            source_qual = ".".join(owner_name(source_obj, source_table))
        except (UnknownObjectError, BadParamError):
            continue
        for join in joins:
            target = join.get("table") if isinstance(join, Mapping) else None
            if not isinstance(target, str):
                continue
            try:
                target_obj = require_object(context.catalog, connection, target)
                target_qual = ".".join(owner_name(target_obj, target))
            except (UnknownObjectError, BadParamError):
                continue
            raw_on = join.get("on") if isinstance(join, Mapping) else None
            raw_dir = join.get("direction") if isinstance(join, Mapping) else None
            if raw_dir not in {"parent", "child"}:
                continue
            try:
                if source_qual == qualified:
                    on = _validate_on_pairs(
                        context.catalog, connection, qualified, target_qual, raw_on or []
                    )
                    collected.append(
                        {
                            "table": target_qual,
                            "on": on,
                            "direction": raw_dir,
                            "source": "manual",
                            "confidence": 1.0,
                        }
                    )
                elif target_qual == qualified:
                    on = _validate_on_pairs(
                        context.catalog, connection, source_qual, qualified, raw_on or []
                    )
                    flipped = [[right, left] for left, right in on]
                    collected.append(
                        {
                            "table": source_qual,
                            "on": flipped,
                            "direction": _flip_direction(raw_dir),
                            "source": "manual",
                            "confidence": 1.0,
                        }
                    )
            except (UnknownObjectError, BadParamError):
                continue

    collected.extend(_collect_fk_joins(context, connection, owner, name, qualified, wanted))
    if floor <= 0.7:
        collected.extend(
            _name_match_joins(
                context.catalog, connection, owner, qualified, object_columns(obj), wanted
            )
        )
    return {"table": qualified, "joins": _merge_joins(collected, min_confidence=floor, wanted=wanted)}


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="suggest_joins",
        description=(
            "테이블의 조인 후보를 source와 confidence와 함께 반환한다. "
            "우선순위: ① relationships.d/ 수동 등록 ② FK 제약 ③ 컬럼명 규칙 매칭. "
            "FK 없는 DW에서는 name_match 후보를 그대로 믿지 말고 table_profile로 값 범위를 확인한 뒤 조인하라."
        ),
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("table", "string", required=True),
            ParamSpec("direction", "string", required=False, default="both"),
            ParamSpec("min_confidence", "number", required=False, default=0.5),
        ),
        example={
            "name": "suggest_joins",
            "arguments": {"connection": "dw-prod", "table": "DW.F_SALES"},
        },
        handler=suggest_joins,
    )


__all__ = ["definition", "suggest_joins"]
