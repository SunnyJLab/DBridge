"""explain tool extracted from analysis_tools."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..context import ToolContext
from ..errors import (
    BadParamError,
    InvalidSqlError,
    NoAccessError,
    ToolError,
)
from ..registry import ParamSpec, ToolDefinition
from ..sql_safety import (
    quote_identifier,
    validate_identifier,
    validate_select_sql,
)

from ..common.results import (
    as_date_str,
    as_int,
    as_text,
    attr_or_key,
    ora_fields,
)
from ..common.params import (
    normalize_binds_lenient,
    require_nonempty_string,
    tool_setting,
)
from ..common.catalog_access import require_object

_PLAN_FETCH_CAP = 10_000

_DEFAULT_HIGH_ROWS = 1_000_000  # config.tools.high_rows_threshold 로 조정 (대형 DW 는 올린다)

_STATEMENT_ID_LEN = 30
_ACCESS_ORA_CODES = frozenset({942, 980, 995, 1031, 1035, 1045, 1745, 1917, 1940})

_EXPLAIN_DESCRIPTION = (
    "실행 계획을 반환한다. 기본 `format: \"summary\"`는 비용·예상 행수·테이블별 접근 방식·"
    "**파티션 프루닝 여부**·경고(`FULL_PARTITION_SCAN`, `CARTESIAN`, `NO_STATS`)를 구조화해 준다. "
    "대형 DW 테이블을 조회하기 전 반드시 호출해 경고가 있으면 WHERE에 파티션 키 조건을 추가하라. "
    "`format: \"full\"`은 `DBMS_XPLAN` 원문이다."
)


def _transaction(context: ToolContext, connection: str, callback: Callable[[Any], Any]) -> Any:
    """Run ``callback(connection)`` in a committing transaction.

    EXPLAIN PLAN 은 조회가 아니라 PLAN_TABLE 에 INSERT 하고 이 도구가 끝에서
    DELETE 한다. SELECT-only 프록시에서 쓰기 경로를 쓰는 유일한 지점이라
    ``database.transaction`` (commit=True) 을 명시적으로 통과시킨다.
    """
    return context.database.transaction(connection, callback, timeout_s=context.query_timeout)


def _explain_plan_table(context: ToolContext) -> str:
    raw = context.config.get("explain") if isinstance(context.config, Mapping) else None
    if not isinstance(raw, Mapping):
        raise NoAccessError(
            "실행 계획 기능이 구성되어 있지 않습니다",
            hint="전용 PLAN_TABLE INSERT/SELECT/DELETE 권한을 부여한 뒤 explain.enabled와 explain.plan_table을 구성하세요",
        )
    if raw.get("enabled") is not True:
        raise NoAccessError(
            "실행 계획 기능이 비활성화되어 있습니다",
            hint="전용 PLAN_TABLE INSERT/SELECT/DELETE 권한을 부여한 뒤 explain.enabled와 explain.plan_table을 구성하세요",
        )
    plan_table = raw.get("plan_table")
    if not isinstance(plan_table, str) or not plan_table.strip():
        raise NoAccessError(
            "explain.plan_table이 구성되어 있지 않습니다",
            hint="임의 PLAN_TABLE을 사용하지 않습니다. 승인된 전용 테이블 이름을 명시하세요",
        )
    return validate_identifier(plan_table.strip(), max_parts=2)


def _new_statement_id() -> str:
    return uuid.uuid4().hex[:_STATEMENT_ID_LEN]


def _cursor_execute(cursor: Any, sql: str, binds: Mapping[str, Any] | None = None) -> None:
    try:
        if binds:
            cursor.execute(sql, dict(binds))
        else:
            cursor.execute(sql)
    except ToolError:
        raise
    except Exception as exc:
        ora, message, position = ora_fields(exc)
        code = None
        if ora and ora.upper().startswith("ORA-"):
            digits = ora[4:]
            if digits.isdigit():
                code = int(digits)
        if code in _ACCESS_ORA_CODES:
            raise NoAccessError(
                message,
                ora=ora,
                hint="전용 PLAN_TABLE INSERT/SELECT/DELETE 권한과 DBMS_XPLAN 실행 권한을 확인하세요",
            ) from exc
        raise InvalidSqlError(message, ora=ora, position=position) from exc


def _cursor_rows(cursor: Any) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
    description = getattr(cursor, "description", None) or ()
    columns = tuple(str(item[0]).upper() for item in description)
    fetched = cursor.fetchmany(_PLAN_FETCH_CAP + 1)
    if len(fetched) > _PLAN_FETCH_CAP:
        fetched = fetched[:_PLAN_FETCH_CAP]
    rows = [tuple(row) for row in fetched]
    return columns, rows


def _plan_maps(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for index, col in enumerate(columns):
            item[col] = row[index] if index < len(row) else None
        out.append(item)
    return out


def _operation_text(row: Mapping[str, Any]) -> str:
    op = as_text(row.get("OPERATION")) or ""
    options = as_text(row.get("OPTIONS"))
    if options:
        return f"{op} {options}".strip()
    return op


def _object_qualified(row: Mapping[str, Any]) -> str | None:
    owner = as_text(row.get("OBJECT_OWNER"))
    name = as_text(row.get("OBJECT_NAME"))
    if owner and name:
        try:
            return f"{validate_identifier(owner, max_parts=1)}.{validate_identifier(name, max_parts=1)}"
        except ToolError:
            return f"{owner}.{name}"
    if name:
        return name
    return None


def _partition_bounds(row: Mapping[str, Any]) -> tuple[int | None, int | None, str | None]:
    start_text = as_text(row.get("PARTITION_START"))
    stop_text = as_text(row.get("PARTITION_STOP"))
    start = as_int(start_text) if start_text is not None else None
    stop = as_int(stop_text) if stop_text is not None else None
    label = None
    if start_text and stop_text:
        label = f"{start_text} - {stop_text}"
    elif start_text:
        label = start_text
    return start, stop, label


def _partition_summary(catalog: Any, connection: str, qualified: str) -> dict[str, Any]:
    try:
        return catalog.partition_summary(connection, qualified)
    except ToolError:
        return {}


def _last_analyzed(catalog: Any, connection: str, qualified: str) -> str | None | object:
    missing = object()
    try:
        obj = require_object(catalog, connection, qualified)
    except ToolError:
        return missing
    value = attr_or_key(obj, "last_analyzed")
    return value if isinstance(value, str) or value is None else as_date_str(value)


def _summarize_plan(
    context: ToolContext,
    connection: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    root: Mapping[str, Any] | None = None
    for row in rows:
        ident = as_int(row.get("ID"))
        if ident == 0:
            root = row
            break
    if root is None and rows:
        root = min(rows, key=lambda item: as_int(item.get("ID")) if as_int(item.get("ID")) is not None else 10**9)
    cost = as_int(root.get("COST")) if root is not None else None
    estimated_rows = as_int(root.get("CARDINALITY")) if root is not None else None

    access: list[dict[str, Any]] = []
    joins: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_no_stats: set[str] = set()
    seen_full: set[str] = set()
    metadata: dict[str, tuple[dict[str, Any], Any]] = {}

    for row in rows:
        operation = _operation_text(row)
        upper_op = operation.upper()
        qualified = _object_qualified(row)
        object_type = (as_text(row.get("OBJECT_TYPE")) or "").upper()
        if "JOIN" in upper_op:
            join_item: dict[str, Any] = {"type": operation}
            join_rows = as_int(row.get("CARDINALITY"))
            if join_rows is not None:
                join_item["estimated_rows"] = join_rows
            joins.append(join_item)
            if "CARTESIAN" in upper_op:
                warnings.append(
                    {
                        "code": "CARTESIAN",
                        "hint": "MERGE JOIN CARTESIAN가 있습니다. 조인 조건을 확인하세요",
                    }
                )
        if qualified and object_type in {"", "TABLE", "MAT_VIEW", "MATERIALIZED VIEW", "VIEW"}:
            start, stop, label = _partition_bounds(row)
            item: dict[str, Any] = {"table": qualified, "operation": operation}
            if label:
                item["partitions"] = label
            if qualified not in metadata:
                metadata[qualified] = (
                    _partition_summary(context.catalog, connection, qualified),
                    _last_analyzed(context.catalog, connection, qualified),
                )
            partition, analyzed = metadata[qualified]
            part_count = as_int(partition.get("partition_count")) if partition.get("partitioned") else None
            if start is None or stop is None or part_count is None:
                item["pruned"] = None
            elif start == 1 and stop == part_count:
                item["pruned"] = False
                if qualified not in seen_full:
                    seen_full.add(qualified)
                    keys = partition.get("key_columns", [])
                    key_hint = keys[0] if keys else "파티션 키"
                    warnings.append(
                        {
                            "code": "FULL_PARTITION_SCAN",
                            "table": qualified,
                            "hint": (
                                f"파티션 키 {key_hint} 조건을 WHERE에 직접 넣으면 "
                                f"{part_count}개 중 필요한 파티션만 읽습니다"
                            ),
                        }
                    )
            else:
                item["pruned"] = True
            access.append(item)
            if analyzed is None and qualified not in seen_no_stats:
                seen_no_stats.add(qualified)
                warnings.append(
                    {
                        "code": "NO_STATS",
                        "table": qualified,
                        "hint": "table_profile mode=live 또는 count_rows를 사용하세요",
                    }
                )
        elif qualified and "INDEX" in upper_op:
            item = {"table": qualified, "operation": operation, "index": qualified.split(".")[-1], "pruned": None}
            access.append(item)

    high_rows = tool_setting(context, "high_rows_threshold", _DEFAULT_HIGH_ROWS)
    if estimated_rows is not None and estimated_rows > high_rows:
        warnings.append(
            {
                "code": "HIGH_ROWS",
                "hint": (
                    f"예상 행수가 {high_rows:,}을 초과합니다. "
                    "query_summary로 집계하거나 WHERE로 범위를 줄이세요"
                ),
            }
        )

    payload: dict[str, Any] = {
        "format": "summary",
        "cost": cost,
        "estimated_rows": estimated_rows,
        "access": access,
        "joins": joins,
        "warnings": warnings,
    }
    return payload


def explain(
    context: ToolContext,
    *,
    connection: str,
    sql: str,
    binds: dict[str, Any] | None = None,
    format: str = "summary",
) -> dict[str, Any]:
    context.get_connection(connection)
    stripped = validate_select_sql(sql)
    fmt = require_nonempty_string(format, "format").strip().lower()
    if fmt not in {"summary", "full"}:
        raise BadParamError(
            "format은 summary 또는 full 이어야 합니다",
            hint="format에 summary 또는 full만 전달하세요",
        )
    plan_table = _explain_plan_table(context)
    quoted_plan = quote_identifier(plan_table, max_parts=2)
    statement_id = _new_statement_id()
    bind_values = normalize_binds_lenient(binds)
    explain_sql = f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' INTO {quoted_plan} FOR {stripped}"
    fetch_sql = (
        f"SELECT ID, PARENT_ID, OPERATION, OPTIONS, OBJECT_OWNER, OBJECT_NAME, OBJECT_TYPE, "
        f"PARTITION_START, PARTITION_STOP, CARDINALITY, COST "
        f"FROM {quoted_plan} WHERE STATEMENT_ID = :sid ORDER BY ID"
    )
    delete_sql = f"DELETE FROM {quoted_plan} WHERE STATEMENT_ID = :sid"
    xplan_sql = (
        "SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY(:pt, :sid, 'TYPICAL'))"
    )

    def _run(conn: Any) -> dict[str, Any]:
        cursor = conn.cursor()
        try:
            _cursor_execute(cursor, explain_sql, bind_values or None)
            primary: BaseException | None = None
            try:
                plan_text = None
                if fmt == "full":
                    try:
                        _cursor_execute(
                            cursor,
                            xplan_sql,
                            {"pt": plan_table, "sid": statement_id},
                        )
                    except InvalidSqlError as exc:
                        raise NoAccessError(
                            exc.message,
                            ora=exc.ora,
                            hint="EXECUTE ON SYS.DBMS_XPLAN 권한과 전용 PLAN_TABLE을 확인하세요",
                        ) from exc
                    _xcols, xrows = _cursor_rows(cursor)
                    lines: list[str] = []
                    for row in xrows:
                        if not row:
                            continue
                        cell = row[0]
                        lines.append("" if cell is None else str(cell))
                    plan_text = "\n".join(lines)
                else:
                    _cursor_execute(cursor, fetch_sql, {"sid": statement_id})
                    columns, rows = _cursor_rows(cursor)
                    plan_rows = _plan_maps(columns, rows)
            except BaseException as exc:
                primary = exc
                raise
            finally:
                try:
                    _cursor_execute(cursor, delete_sql, {"sid": statement_id})
                except BaseException:
                    if primary is None:
                        raise
        finally:
            cursor.close()
        if fmt == "full":
            return {"format": "full", "plan_text": plan_text or ""}
        return _summarize_plan(context, connection, plan_rows)

    return _transaction(context, connection, _run)


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="explain",
        description=_EXPLAIN_DESCRIPTION,
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("sql", "string", required=True),
            ParamSpec("binds", "dict", required=False),
            ParamSpec("format", "string", required=False, default="summary"),
        ),
        example={
            "name": "explain",
            "arguments": {
                "connection": "dw-prod",
                "sql": "SELECT * FROM DW.F_SALES WHERE BASE_DT >= :from_dt",
                "format": "summary",
            },
        },
        handler=explain,
    )


__all__ = ["definition", "explain"]
