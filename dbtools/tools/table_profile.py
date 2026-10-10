"""Column-level profile from optimizer stats, or exact live aggregates.

제한 술어 문법(`where`)은 count_rows 와 동일하게 ``sql_safety.render_restricted_where``
하나만 쓴다. 이 도구에 전용 파서를 두지 않는다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..context import ToolContext
from ..database import AdapterError
from ..errors import (
    BadParamError,
    NoAccessError,
)
from ..registry import ParamSpec, ToolDefinition
from ..sql_safety import (
    quote_identifier,
    quote_qualified_name,
    render_restricted_where,
    validate_select_sql,
)

from ..catalog import _format_data_type
from ..common.results import (
    as_date_str,
    as_int,
    as_text,
    row_maps,
)
from ..common.params import (
    bounded_limit,
    normalize_binds_reserved_guard,
    require_nonempty_string,
    tool_setting,
)
from ..common.execution import (
    execute,
    query_maps,
)
from ..common.catalog_access import (
    LOB_TYPES,
    object_column_names,
    object_columns,
    owner_name,
    require_column,
    require_object,
)

_LIVE_COLUMN_CAP = 5
_TOP_N_CAP = 50

_DEFAULT_STALE_RATIO = 0.10  # config.tools.stale_change_ratio 로 조정 (사이트 ETL 주기에 맞춤)

_TABLE_PROFILE_DESCRIPTION = (
    "컬럼별 프로파일을 반환한다. 기본 `stats`는 `ALL_TAB_*` 옵티마이저 메타데이터로 테이블을 "
    "스캔하지 않고 전 컬럼의 통계와 역할(`key/code/flag/date/measure/text`)을 추정한다. 정확한 "
    "distinct·top values가 필요하면 "
    f"컬럼을 최대 {_LIVE_COLUMN_CAP}개 지정해 `live`로 호출하라(`where`/`binds`는 live 전용). "
    "`stale: true`면 live 또는 `count_rows exact`를 사용하라. 코드명은 `list_connections`의 "
    "공통코드 테이블을 `query`로 조회하라."
)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _role(data_type: str, num_distinct: int | None, num_rows: int | None) -> str:
    dt = data_type.upper()
    is_date = dt.startswith("DATE") or dt.startswith("TIMESTAMP")
    is_num = dt.startswith("NUMBER") or dt in {"FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "INTEGER"}
    if (
        num_distinct is not None
        and num_rows is not None
        and num_rows > 0
        and num_distinct >= num_rows * 0.95
    ):
        return "key"
    if num_distinct is not None and num_distinct <= 2:
        return "flag"
    if num_distinct is not None and num_distinct <= 50:
        return "code"
    if is_date:
        return "date"
    if is_num:
        return "measure"
    return "text"


def _stat_bound(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        return as_date_str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _null_pct(num_nulls: int | None, num_rows: int | None) -> float | None:
    if num_nulls is None or num_rows is None or num_rows <= 0:
        return None
    return round(100.0 * float(num_nulls) / float(num_rows), 1)


def _column_names_requested(
    catalog: Any,
    connection: str,
    qualified: str,
    columns: list[Any] | None,
    *,
    live: bool,
) -> list[str] | None:
    if columns is None:
        if live:
            raise BadParamError(
                f"live 모드는 columns를 최대 {_LIVE_COLUMN_CAP}개까지 지정해야 합니다",
                hint="프로파일할 컬럼 이름을 배열로 전달하세요",
            )
        return None
    if not isinstance(columns, list) or not columns:
        raise BadParamError(
            "columns가 비어 있습니다",
            hint="컬럼 이름 배열을 전달하세요",
        )
    names: list[str] = []
    seen: set[str] = set()
    for item in columns:
        if not isinstance(item, str):
            raise BadParamError(
                "columns 항목은 문자열이어야 합니다",
                hint="컬럼 이름을 문자열 배열로 전달하세요",
            )
        ident = require_column(catalog, connection, qualified, item)
        if ident in seen:
            continue
        seen.add(ident)
        names.append(ident)
    if live and len(names) > _LIVE_COLUMN_CAP:
        raise BadParamError(
            f"live 모드 컬럼은 최대 {_LIVE_COLUMN_CAP}개입니다",
            hint=f"columns를 {_LIVE_COLUMN_CAP}개 이하로 줄이세요",
        )
    return names


def _stats_stale(
    context: ToolContext,
    connection: str,
    owner: str,
    table: str,
    num_rows: int | None,
    last_analyzed: str | None,
) -> bool:
    if last_analyzed is None:
        return True
    sql = (
        "SELECT SUM(NVL(INSERTS, 0)) AS INSERTS, SUM(NVL(UPDATES, 0)) AS UPDATES, "
        "SUM(NVL(DELETES, 0)) AS DELETES, MAX(TRUNCATED) AS TRUNCATED "
        "FROM ALL_TAB_MODIFICATIONS "
        "WHERE TABLE_OWNER = :owner AND TABLE_NAME = :table_name"
    )
    try:
        rows, _truncated = query_maps(
            context,
            connection,
            sql,
            {"owner": owner, "table_name": table},
            max_rows=1,
        )
    except NoAccessError:
        return True
    if not rows:
        return True
    truncated = as_text(rows[0].get("TRUNCATED"))
    if truncated and truncated.upper() == "YES":
        return True
    inserts = as_int(rows[0].get("INSERTS")) or 0
    updates = as_int(rows[0].get("UPDATES")) or 0
    deletes = as_int(rows[0].get("DELETES")) or 0
    changed = inserts + updates + deletes
    if num_rows is None or num_rows <= 0:
        return changed > 0
    ratio = tool_setting(context, "stale_change_ratio", _DEFAULT_STALE_RATIO)
    return changed > num_rows * ratio


def _profile_stats(
    context: ToolContext,
    connection: str,
    owner: str,
    table: str,
    requested: list[str] | None,
    *,
    detailed: bool,
) -> dict[str, Any]:
    col_sql = (
        "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, CHAR_LENGTH "
        "FROM ALL_TAB_COLUMNS WHERE OWNER = :owner AND TABLE_NAME = :table_name "
        "ORDER BY COLUMN_ID"
    )
    stat_sql = (
        "SELECT COLUMN_NAME, NUM_DISTINCT, LOW_VALUE, HIGH_VALUE, DENSITY, NUM_NULLS, "
        "NUM_BUCKETS, LAST_ANALYZED, AVG_COL_LEN, HISTOGRAM "
        "FROM ALL_TAB_COL_STATISTICS WHERE OWNER = :owner AND TABLE_NAME = :table_name"
    )
    tab_sql = (
        "SELECT NUM_ROWS, LAST_ANALYZED FROM ALL_TABLES "
        "WHERE OWNER = :owner AND TABLE_NAME = :table_name"
    )
    binds = {"owner": owner, "table_name": table}
    col_rows, _c_trunc = query_maps(context, connection, col_sql, binds)
    stat_rows, _s_trunc = query_maps(context, connection, stat_sql, binds)
    tab_rows, _t_trunc = query_maps(context, connection, tab_sql, binds, max_rows=2)
    stats_by_col = {
        (as_text(row.get("COLUMN_NAME")) or ""): row
        for row in stat_rows
        if as_text(row.get("COLUMN_NAME"))
    }
    num_rows = as_int(tab_rows[0].get("NUM_ROWS")) if tab_rows else None
    last_analyzed = as_date_str(tab_rows[0].get("LAST_ANALYZED")) if tab_rows else None
    wanted = set(requested) if requested is not None else None
    columns_out: list[dict[str, Any]] = []
    for row in col_rows:
        name = as_text(row.get("COLUMN_NAME"))
        if not name:
            continue
        if wanted is not None and name not in wanted:
            continue
        data_type = _format_data_type(row)
        stats = stats_by_col.get(name, {})
        num_distinct = as_int(stats.get("NUM_DISTINCT"))
        num_nulls = as_int(stats.get("NUM_NULLS"))
        item: dict[str, Any] = {
            "name": name,
            "type": data_type,
            "num_distinct": num_distinct,
            "null_pct": _null_pct(num_nulls, num_rows),
            "role": _role(data_type, num_distinct, num_rows),
        }
        if not data_type.upper().startswith("RAW"):
            low = _stat_bound(stats.get("LOW_VALUE"))
            high = _stat_bound(stats.get("HIGH_VALUE"))
            if low is not None:
                item["low"] = low
            if high is not None:
                item["high"] = high
        if detailed:
            item["histogram"] = as_text(stats.get("HISTOGRAM"))
            item["num_buckets"] = as_int(stats.get("NUM_BUCKETS"))
            item["avg_col_len"] = as_int(stats.get("AVG_COL_LEN"))
            density = _as_float(stats.get("DENSITY"))
            item["density"] = density
            item["last_analyzed"] = as_date_str(stats.get("LAST_ANALYZED"))
        columns_out.append(item)
    if wanted is not None:
        order = {name: index for index, name in enumerate(requested or [])}
        columns_out.sort(key=lambda item: order.get(item["name"], 10**9))
    return {
        "owner": owner,
        "table": table,
        "num_rows": num_rows,
        "last_analyzed": last_analyzed,
        "source": "stats",
        "stale": _stats_stale(context, connection, owner, table, num_rows, last_analyzed),
        "columns": columns_out,
    }


def _profile_live(
    context: ToolContext,
    connection: str,
    owner: str,
    table: str,
    requested: list[str],
    obj: Any,
    *,
    top_n: int,
    where: str | None,
    binds: dict[str, Any] | None,
) -> dict[str, Any]:
    if where is None and binds:
        raise BadParamError("binds는 where와 함께 전달해야 합니다")
    column_types = {
        item["name"]: item["type"].split("(", 1)[0].strip().upper()
        for item in object_columns(obj)
    }
    unsupported = [name for name in requested if column_types.get(name) in LOB_TYPES]
    if unsupported:
        name = unsupported[0]
        raise BadParamError(
            f"live 모드에서 지원하지 않는 컬럼 타입입니다: {name} ({column_types[name]})",
            hint="LOB/LONG 계열을 제외하고 columns를 지정하세요",
        )
    quoted_table = quote_qualified_name((owner, table))
    where_sql = ""
    bind_values: dict[str, Any] = {}
    if where is not None:
        # count_rows 와 같은 제한 술어 문법·같은 파서를 쓴다 (sql_safety).
        rendered, bind_values = render_restricted_where(
            where,
            allowed_columns=object_column_names(obj),
            table_name=table,
            owner=owner,
            binds=normalize_binds_reserved_guard(binds),
        )
        where_sql = f" WHERE ({rendered})"
    # 모든 컬럼 집계를 한 문장으로 묶어 COUNT(*)와 컬럼별 반복 조회를 줄인다.
    quoted_columns = [quote_identifier(name, max_parts=1) for name in requested]
    select_parts = ["COUNT(*) AS CNT"]
    for index, quoted_col in enumerate(quoted_columns):
        select_parts.extend([
            f"COUNT(DISTINCT {quoted_col}) AS C{index}_ND",
            f"COUNT(*) - COUNT({quoted_col}) AS C{index}_NC",
            f"MIN({quoted_col}) AS C{index}_MN",
            f"MAX({quoted_col}) AS C{index}_MX",
        ])
    if any(key.lower() == "top_n" for key in bind_values):
        raise BadParamError(
            "binds에 예약 이름 top_n는 사용할 수 없습니다",
            hint="다른 바인드 이름을 사용하세요",
        )
    top_binds = {**bind_values, "top_n": top_n}
    agg_sql = validate_select_sql(
        f"SELECT {', '.join(select_parts)} FROM {quoted_table}{where_sql}"
    )
    agg_result = execute(context, connection, agg_sql, bind_values, max_rows=1)
    agg_rows, _ = row_maps(agg_result)
    if not agg_rows:
        raise AdapterError(f"{owner}.{table}: 집계 결과가 비었습니다")
    agg = agg_rows[0]
    scanned = as_int(agg.get("CNT"))
    elapsed = agg_result.elapsed_ms
    columns_out: list[dict[str, Any]] = []
    for index, (name, quoted_col) in enumerate(zip(requested, quoted_columns)):
        top_sql = validate_select_sql(
            f"SELECT {quoted_col} AS VAL, COUNT(*) AS CNT FROM {quoted_table}{where_sql} "
            f"GROUP BY {quoted_col} ORDER BY COUNT(*) DESC FETCH FIRST :top_n ROWS ONLY"
        )
        top_result = execute(context, connection, top_sql, top_binds, max_rows=top_n)
        elapsed += int(getattr(top_result, "elapsed_ms", 0) or 0)
        top_rows, _ = row_maps(top_result)
        top_values = []
        for row in top_rows:
            top_values.append({"value": row.get("VAL"), "count": as_int(row.get("CNT"))})
        columns_out.append(
            {
                "name": name,
                "count": scanned,
                "num_distinct": as_int(agg.get(f"C{index}_ND")),
                "null_count": as_int(agg.get(f"C{index}_NC")),
                "min": agg.get(f"C{index}_MN"),
                "max": agg.get(f"C{index}_MX"),
                "top_values": top_values,
            }
        )
    return {
        "owner": owner,
        "table": table,
        "source": "live",
        "exact": True,
        "scanned_rows": scanned,
        "elapsed_ms": elapsed,
        "columns": columns_out,
    }


def table_profile(
    context: ToolContext,
    *,
    connection: str,
    table: str,
    columns: list[Any] | None = None,
    mode: str = "stats",
    top_n: int = 10,
    where: str | None = None,
    binds: dict[str, Any] | None = None,
    format: str = "concise",
) -> dict[str, Any]:
    require_nonempty_string(table, "table")
    mode_key = require_nonempty_string(mode, "mode").strip().lower()
    if mode_key not in {"stats", "live"}:
        raise BadParamError(
            "mode는 stats 또는 live 이어야 합니다",
            hint="mode에 stats 또는 live만 전달하세요",
        )
    fmt = require_nonempty_string(format, "format").strip().lower()
    if fmt not in {"concise", "detailed"}:
        raise BadParamError(
            "format은 concise 또는 detailed 이어야 합니다",
            hint="format에 concise 또는 detailed만 전달하세요",
        )
    if mode_key == "stats" and (where is not None or binds):
        raise BadParamError("where와 binds는 live 모드에서만 사용할 수 있습니다")
    if mode_key == "live" and fmt != "concise":
        raise BadParamError("format=detailed는 stats 모드에서만 사용할 수 있습니다")
    obj = require_object(context.catalog, connection, table)
    owner_id, table_name = owner_name(obj, table)
    qualified = f"{owner_id}.{table_name}"
    requested = _column_names_requested(
        context.catalog,
        connection,
        qualified,
        columns,
        live=mode_key == "live",
    )
    if mode_key == "live":
        assert requested is not None
        top = bounded_limit(top_n, cap=_TOP_N_CAP, name="top_n")
        return _profile_live(
            context,
            connection,
            owner_id,
            table_name,
            requested,
            obj,
            top_n=top,
            where=where,
            binds=binds,
        )
    return _profile_stats(
        context,
        connection,
        owner_id,
        table_name,
        requested,
        detailed=fmt == "detailed",
    )


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="table_profile",
        description=_TABLE_PROFILE_DESCRIPTION,
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("table", "string", required=True),
            ParamSpec("columns", "list", required=False),
            ParamSpec("mode", "string", required=False, default="stats"),
            ParamSpec("top_n", "int", required=False, default=10),
            ParamSpec("where", "string", required=False),
            ParamSpec("binds", "dict", required=False),
            ParamSpec("format", "string", required=False, default="concise"),
        ),
        example={
            "name": "table_profile",
            "arguments": {
                "connection": "dw-prod",
                "table": "DW.F_SALES",
                "mode": "live",
                "columns": ["STAT_CD"],
            },
        },
        handler=table_profile,
    )


__all__ = ["definition", "table_profile"]
