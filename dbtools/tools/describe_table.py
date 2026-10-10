"""Column, key, index, and partition metadata for a table or view."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..registry import ParamSpec, ToolDefinition
from ..sql_safety import validate_identifier

from ..common.catalog_access import owner_name, require_object
from ..catalog import _format_data_type
from ..common.execution import execute
from ..common.params import require_nonempty_string
from ..common.results import attr_or_key

def _live_describe(context: ToolContext, connection: str, obj: Any, owner: str, table: str) -> dict[str, Any]:
    binds = {"owner": owner, "table_name": table}
    col_sql = (
        "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, "
        "NULLABLE, DATA_DEFAULT, COLUMN_ID, CHAR_LENGTH FROM ALL_TAB_COLUMNS "
        "WHERE OWNER = :owner AND TABLE_NAME = :table_name ORDER BY COLUMN_ID"
    )
    tab_sql = (
        "SELECT COMMENTS FROM ALL_TAB_COMMENTS "
        "WHERE OWNER = :owner AND TABLE_NAME = :table_name"
    )
    cmt_sql = (
        "SELECT COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS "
        "WHERE OWNER = :owner AND TABLE_NAME = :table_name"
    )
    pk_sql = (
        "SELECT acc.COLUMN_NAME FROM ALL_CONSTRAINTS ac "
        "JOIN ALL_CONS_COLUMNS acc ON ac.OWNER = acc.OWNER "
        "AND ac.CONSTRAINT_NAME = acc.CONSTRAINT_NAME "
        "WHERE ac.OWNER = :owner AND ac.TABLE_NAME = :table_name "
        "AND ac.CONSTRAINT_TYPE = :ctype ORDER BY acc.POSITION"
    )
    idx_sql = (
        "SELECT ai.INDEX_NAME, ai.UNIQUENESS, api.LOCALITY, aic.COLUMN_NAME, "
        "aic.COLUMN_POSITION FROM ALL_INDEXES ai "
        "JOIN ALL_IND_COLUMNS aic ON ai.OWNER = aic.INDEX_OWNER "
        "AND ai.INDEX_NAME = aic.INDEX_NAME "
        "LEFT JOIN ALL_PART_INDEXES api ON ai.OWNER = api.OWNER "
        "AND ai.INDEX_NAME = api.INDEX_NAME "
        "WHERE ai.TABLE_OWNER = :owner AND ai.TABLE_NAME = :table_name "
        "ORDER BY ai.INDEX_NAME, aic.COLUMN_POSITION"
    )
    part_sql = (
        "SELECT PARTITIONING_TYPE FROM ALL_PART_TABLES "
        "WHERE OWNER = :owner AND TABLE_NAME = :table_name"
    )
    pkey_sql = (
        "SELECT COLUMN_NAME FROM ALL_PART_KEY_COLUMNS "
        "WHERE OWNER = :owner AND NAME = :table_name AND OBJECT_TYPE = :object_type "
        "ORDER BY COLUMN_POSITION"
    )
    cols_result = execute(context, connection, col_sql, binds, max_rows=4096)
    comments_result = execute(context, connection, cmt_sql, binds, max_rows=4096)
    tab_result = execute(context, connection, tab_sql, binds, max_rows=1)
    pk_result = execute(
        context, connection, pk_sql, {**binds, "ctype": "P"}, max_rows=256
    )
    idx_result = execute(context, connection, idx_sql, binds, max_rows=4096)
    part_result = execute(context, connection, part_sql, binds, max_rows=1)
    pkey_result = execute(
        context, connection, pkey_sql, {**binds, "object_type": "TABLE"}, max_rows=256
    )

    comment_map = {str(row[0]).upper(): row[1] for row in comments_result.rows if row and row[0]}
    pk_cols = [validate_identifier(str(row[0]), max_parts=1) for row in pk_result.rows if row and row[0]]
    pk_set = set(pk_cols)
    # 위치 인덱스 대신 컬럼명으로 매핑한다. 타입 표기는 catalog 의 단일 구현을 쓴다.
    col_names = [str(item).upper() for item in cols_result.columns]
    columns = []
    for row in cols_result.rows:
        row_map = dict(zip(col_names, row))
        name = validate_identifier(str(row_map.get("COLUMN_NAME")), max_parts=1)
        columns.append(
            {
                "name": name,
                "type": _format_data_type(row_map),
                "nullable": str(row_map.get("NULLABLE")).upper() != "N",
                "default": row_map.get("DATA_DEFAULT"),
                "comment": comment_map.get(name),
                "pk": name in pk_set,
            }
        )
    indexes_acc: dict[str, dict[str, Any]] = {}
    for row in idx_result.rows:
        idx_name = str(row[0])
        entry = indexes_acc.setdefault(
            idx_name,
            {
                "name": idx_name,
                "columns": [],
                "unique": str(row[1]).upper() == "UNIQUE",
                "local": str(row[2] or "").strip().upper() == "LOCAL",
            },
        )
        if row[3]:
            entry["columns"].append(validate_identifier(str(row[3]), max_parts=1))
    partitioned = bool(part_result.rows)
    part_type = part_result.rows[0][0] if part_result.rows else None
    key_columns = [
        validate_identifier(str(row[0]), max_parts=1) for row in pkey_result.rows if row and row[0]
    ]
    table_comment = tab_result.rows[0][0] if tab_result.rows else None
    return {
        "owner": owner,
        "table": table,
        "type": attr_or_key(obj, "type", "TABLE"),
        "comment": table_comment if table_comment is not None else attr_or_key(obj, "comment"),
        "num_rows": attr_or_key(obj, "num_rows"),
        "source": "stats",
        "last_analyzed": attr_or_key(obj, "last_analyzed"),
        "columns": columns,
        "primary_key": pk_cols,
        "indexes": list(indexes_acc.values()),
        "partition": {
            "partitioned": partitioned,
            "type": part_type,
            "key_columns": key_columns,
            "hint": "상세는 partition_info",
        },
    }


def describe_table(
    context: ToolContext,
    *,
    connection: str,
    table: str,
    live: bool = False,
) -> dict[str, Any]:
    context.get_connection(connection)
    require_nonempty_string(table, "table")
    if not live:
        return context.catalog.describe(connection, table)
    obj = require_object(context.catalog, connection, table)
    owner, name = owner_name(obj, table)
    return _live_describe(context, connection, obj, owner, name)


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="describe_table",
        description=(
            "테이블·뷰의 컬럼 이름·타입·NULL 허용·기본값·코멘트와 PK·인덱스·파티션 키를 반환한다. "
            "SQL 작성 전 반드시 호출하고, 파티션 경계·행수 분포는 `partition_info`, 컬럼 값 분포는 `table_profile`로 확인하라. "
            "기본은 캐시 응답이며 `live: true`는 딕셔너리를 실조회한다."
        ),
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("table", "string", required=True),
            ParamSpec("live", "bool", required=False, default=False),
        ),
        example={
            "name": "describe_table",
            "arguments": {"connection": "dw-prod", "table": "DW.F_SALES"},
        },
        handler=describe_table,
    )


__all__ = ["definition", "describe_table"]
