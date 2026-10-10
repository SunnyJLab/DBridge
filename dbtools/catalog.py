"""Thread-safe per-connection Oracle catalog cache (ALL_* only).

Refresh builds a new snapshot off-lock and swaps it once. A failed refresh
leaves the previous snapshot in place. Identifiers accepted by public APIs
are validated as unquoted Oracle names. This module never queries DBA_* views.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from .common.results import as_int  # 숫자 강제 변환은 한 구현만 쓴다 (조용한 절단 방지)
from .database import AdapterError, Database, QueryResult
from .errors import BadParamError, NoAccessError, UnknownObjectError
from .sql_safety import split_qualified_name, validate_identifier

log = logging.getLogger("tool")

_CATALOG_FETCH_CAP = 1_000_000
# 도구 계층이 같은 숫자를 다시 정의하지 않도록 공개 상한. my_scope/list_tables/
# search_catalog 는 이 값을 import 해서 쓴다.
SCOPE_OBJECT_LIMIT = 200
_LIST_DEFAULT_LIMIT = 100
LIST_MAX_LIMIT = 500
_SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_LIMIT = 200
_SEARCH_SCOPES = frozenset({"all", "table", "column"})

_SQL_TABLES = (
    "SELECT OWNER, TABLE_NAME, NUM_ROWS, LAST_ANALYZED, PARTITIONED "
    "FROM ALL_TABLES"
)
_SQL_VIEWS = "SELECT OWNER, VIEW_NAME FROM ALL_VIEWS"
_SQL_COLUMNS = (
    "SELECT OWNER, TABLE_NAME, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, "
    "DATA_PRECISION, DATA_SCALE, NULLABLE, DATA_DEFAULT, COLUMN_ID, CHAR_LENGTH "
    "FROM ALL_TAB_COLUMNS"
)
_SQL_TAB_COMMENTS = "SELECT OWNER, TABLE_NAME, COMMENTS FROM ALL_TAB_COMMENTS"
_SQL_COL_COMMENTS = (
    "SELECT OWNER, TABLE_NAME, COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS"
)
_SQL_SYNONYMS = (
    "SELECT OWNER, SYNONYM_NAME, TABLE_OWNER, TABLE_NAME, DB_LINK "
    "FROM ALL_SYNONYMS"
)
_SQL_PART_TABLES = (
    "SELECT OWNER, TABLE_NAME, PARTITIONING_TYPE, SUBPARTITIONING_TYPE, "
    "PARTITION_COUNT, INTERVAL FROM ALL_PART_TABLES"
)
_SQL_PART_KEYS = (
    "SELECT OWNER, NAME, OBJECT_TYPE, COLUMN_NAME, COLUMN_POSITION "
    "FROM ALL_PART_KEY_COLUMNS WHERE OBJECT_TYPE = :object_type"
)
_SQL_CONSTRAINTS = (
    "SELECT OWNER, CONSTRAINT_NAME, CONSTRAINT_TYPE, TABLE_NAME "
    "FROM ALL_CONSTRAINTS WHERE CONSTRAINT_TYPE = :ctype"
)
_SQL_CONS_COLUMNS = (
    "SELECT OWNER, CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, POSITION "
    "FROM ALL_CONS_COLUMNS"
)
_SQL_INDEXES = (
    "SELECT OWNER, INDEX_NAME, TABLE_OWNER, TABLE_NAME, UNIQUENESS, PARTITIONED "
    "FROM ALL_INDEXES"
)
_SQL_IND_COLUMNS = (
    "SELECT INDEX_OWNER, INDEX_NAME, TABLE_OWNER, TABLE_NAME, COLUMN_NAME, "
    "COLUMN_POSITION FROM ALL_IND_COLUMNS"
)
_SQL_PART_INDEXES = (
    "SELECT OWNER, INDEX_NAME, LOCALITY FROM ALL_PART_INDEXES"
)


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    data_default: str | None
    comment: str | None
    column_id: int


@dataclass(frozen=True)
class IndexInfo:
    name: str
    columns: tuple[str, ...]
    unique: bool
    local: bool

@dataclass(frozen=True)
class PartitionSummary:
    partitioned: bool
    type: str | None
    interval: str | None
    subpartition_type: str | None
    key_columns: tuple[str, ...]
    partition_count: int | None

    def as_describe_dict(self) -> dict[str, Any]:
        if not self.partitioned:
            return {
                "partitioned": False,
                "type": None,
                "key_columns": [],
                "hint": None,
            }
        return {
            "partitioned": True,
            "type": self.type,
            "key_columns": list(self.key_columns),
            "hint": "상세는 partition_info",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "partitioned": self.partitioned,
            "type": self.type,
            "interval": self.interval,
            "subpartition_type": self.subpartition_type,
            "key_columns": list(self.key_columns),
            "partition_count": self.partition_count,
        }


@dataclass(frozen=True)
class ObjectInfo:
    owner: str
    name: str
    type: str
    comment: str | None
    num_rows: int | None
    last_analyzed: str | None
    last_analyzed_full: str | None
    partition: PartitionSummary
    columns: tuple[ColumnInfo, ...]
    primary_key: tuple[str, ...]
    indexes: tuple[IndexInfo, ...]
    metadata_available: bool

    @property
    def qualified(self) -> str:
        return f"{self.owner}.{self.name}"


@dataclass(frozen=True)
class SynonymInfo:
    owner: str
    name: str
    table_owner: str
    table_name: str
    db_link: str | None

    @property
    def qualified(self) -> str:
        return f"{self.owner}.{self.name}"

    @property
    def target(self) -> str:
        return f"{self.table_owner}.{self.table_name}"


@dataclass(frozen=True)
class CatalogSnapshot:
    connection: str
    user: str
    refreshed_at: str
    refreshed_monotonic: float
    objects: tuple[ObjectInfo, ...]
    synonyms: tuple[SynonymInfo, ...]
    by_object: Mapping[str, ObjectInfo]
    by_synonym: Mapping[str, SynonymInfo]
    schema_count: int
    table_count: int
    column_count: int


def _empty_snapshot(connection: str, user: str) -> CatalogSnapshot:
    return CatalogSnapshot(
        connection=connection,
        user=user,
        refreshed_at="",
        refreshed_monotonic=0.0,
        objects=(),
        synonyms=(),
        by_object=MappingProxyType({}),
        by_synonym=MappingProxyType({}),
        schema_count=0,
        table_count=0,
        column_count=0,
    )


class CatalogCache:
    """Shared in-memory catalog. All MCP sessions read the same snapshots."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._lock = threading.RLock()
        self._snapshots: dict[str, CatalogSnapshot] = {}
        self._refresh_locks: dict[str, threading.Lock] = {}

    def refresh(self, connection: str | None = None) -> dict[str, Any]:
        """Re-collect one connection's catalog, or all of them when ``None``.

        기존 스냅샷으로 계속 응답하다가 완성된 스냅샷을 원자적으로 교체한다.
        커넥션당 수 초~수십 초가 걸리므로 도구가 자주 호출해서는 안 된다.
        """
        names = self._connection_names(connection)
        refreshed: dict[str, dict[str, int]] = {}
        first_error: Exception | None = None
        for name in names:
            try:
                refreshed[name] = self._refresh_one(name)
            except Exception as exc:
                log.exception("catalog refresh failed: %s", name)
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        return {"refreshed": refreshed}

    def connection_stats(self) -> list[dict[str, Any]]:
        """Per-connection schema/table counts and cache timestamp for the UI."""
        statuses = {item.name: item for item in self._db.snapshot()}
        out: list[dict[str, Any]] = []
        for name in self._db.connection_names():
            status = statuses.get(name)
            payload = status.as_dict() if status is not None else {
                "name": name,
                "dsn": self._db.connection_config(name).dsn,
                "state": "error",
                "latency_ms": None,
                "pool_used": 0,
                "pool_max": self._db.connection_config(name).pool_max,
            }
            snap = self._peek(name)
            payload["schema_count"] = snap.schema_count
            payload["table_count"] = snap.table_count
            payload["column_count"] = snap.column_count
            payload["cache_refreshed_at"] = snap.refreshed_at or None
            out.append(payload)
        return out

    def cache_timestamps(self) -> dict[str, str | None]:
        """Last successful refresh time per connection (``None`` = 아직 수집 안 됨)."""
        return {
            name: (self._peek(name).refreshed_at or None)
            for name in self._db.connection_names()
        }

    def scopes(
        self,
        connection: str,
        owner: str | None = None,
        *,
        object_limit: int = SCOPE_OBJECT_LIMIT,
    ) -> dict[str, Any]:
        """Accessible schemas with object counts, optionally narrowed to one owner.

        현황(개수)과 드릴다운(objects 목록)을 분리한다: ``owner`` 를 생략하면
        스키마별 개수 요약만 반환하고(전체 objects 덤프는 토큰 폭탄이라 생략),
        ``owner`` 를 지정했을 때만 그 스키마의 objects 목록을 붙인다.
        ``object_limit`` 은 그 목록 길이 상한이다 (개수 집계는 상한과 무관하게 전수).
        """
        name = self._require_connection(connection)
        snap = self._peek(name)
        owner_key = None if owner is None else validate_identifier(owner, max_parts=1)
        objects = [
            obj
            for obj in snap.objects
            if owner_key is None or obj.owner == owner_key
        ]
        # [tables, views, partitioned] — partitioned 는 테이블 중 파티션된 개수.
        schema_counts: dict[str, list[int]] = {}
        for obj in objects:
            counts = schema_counts.setdefault(obj.owner, [0, 0, 0])
            if obj.type == "VIEW":
                counts[1] += 1
            else:
                counts[0] += 1
                if obj.partition.partitioned:
                    counts[2] += 1
        schemas = [
            {
                "owner": owner_name,
                "tables": counts[0],
                "views": counts[1],
                "partitioned": counts[2],
            }
            for owner_name, counts in sorted(schema_counts.items())
        ]
        payload: dict[str, Any] = {
            "connection": name,
            "user": snap.user,
            "schemas": schemas,
        }
        if owner_key is None:
            # 현황 단계: 개수만. 대상 스키마를 고른 뒤 owner 로 좁혀 다시 부른다.
            payload["hint"] = (
                "objects 목록은 owner를 지정하면 반환됩니다. "
                "먼저 schemas 개수로 대상 스키마를 고르세요"
            )
            return payload
        limit = _require_limit(object_limit, default=SCOPE_OBJECT_LIMIT, cap=SCOPE_OBJECT_LIMIT)
        truncated = len(objects) > limit
        listed = objects[:limit]
        payload["objects"] = [
            {"owner": obj.owner, "name": obj.name, "type": obj.type}
            for obj in listed
        ]
        payload["truncated"] = truncated
        if truncated:
            payload["hint"] = (
                f"objects는 {limit}개까지만 표시. list_tables의 pattern으로 더 좁히세요"
            )
        return payload

    def list_objects(
        self,
        connection: str,
        owner: str | None = None,
        pattern: str = "%",
        *,
        include_views: bool = True,
        limit: int = _LIST_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Tables and views of one owner filtered by a LIKE pattern."""
        name = self._require_connection(connection)
        snap = self._peek(name)
        owner_key = None if owner is None else validate_identifier(owner, max_parts=1)
        like = _require_like_pattern(pattern)
        cap = _require_limit(limit, default=_LIST_DEFAULT_LIMIT, cap=LIST_MAX_LIMIT)
        matched: list[ObjectInfo] = []
        truncated = False
        for obj in snap.objects:
            if owner_key is not None and obj.owner != owner_key:
                continue
            if not include_views and obj.type == "VIEW":
                continue
            if not _oracle_like(obj.name, like):
                continue
            if len(matched) >= cap:
                truncated = True
                break
            matched.append(obj)
        # 식별용 필드 + 규모 감을 잡는 통계 근사 행수(num_rows). 파티션·통계일자는
        # 목록 판별에 기여하지 않아 제외(각각 partition_info·describe_table 담당).
        tables = [
            {
                "owner": obj.owner,
                "name": obj.name,
                "type": obj.type,
                "comment": obj.comment,
                "num_rows": obj.num_rows,
            }
            for obj in matched
        ]
        return {
            "tables": tables,
            "row_count": len(tables),
            "truncated": truncated,
            "source": "stats",
        }

    def describe(self, connection: str, table: str) -> dict[str, Any]:
        """Full column/PK/index/partition metadata from the cached snapshot.

        ``metadata_available`` 이 False 면 제약·인덱스 수집 권한이 없다는 뜻이고
        ``primary_key``/``indexes`` 는 빈 배열로 나간다 (조용히 생략하지 않는다).
        """
        obj = self.require_object(connection, table)
        pk_set = set(obj.primary_key)
        payload: dict[str, Any] = {
            "owner": obj.owner,
            "table": obj.name,
            "type": obj.type,
            "comment": obj.comment,
            "num_rows": obj.num_rows,
            "source": "stats",
            "last_analyzed": obj.last_analyzed,
            "columns": [
                {
                    "name": col.name,
                    "type": col.data_type,
                    "nullable": col.nullable,
                    "default": col.data_default,
                    "comment": col.comment,
                    "pk": col.name in pk_set,
                }
                for col in obj.columns
            ],
            "partition": obj.partition.as_describe_dict(),
        }
        if not obj.metadata_available:
            payload["metadata_available"] = False
            payload["primary_key"] = []
            payload["indexes"] = []
            return payload
        payload["primary_key"] = list(obj.primary_key)
        payload["indexes"] = [
            {
                "name": idx.name,
                "columns": list(idx.columns),
                "unique": idx.unique,
                "local": idx.local,
            }
            for idx in obj.indexes
        ]
        return payload

    def resolve(self, connection: str, name: str) -> dict[str, Any]:
        """Resolve a bare or qualified name through synonyms to ``OWNER.TABLE``."""
        conn = self._require_connection(connection)
        snap = self._peek(conn)
        raw = name if isinstance(name, str) else ""
        parts = split_qualified_name(raw, max_parts=2)
        if len(parts) == 2:
            return self._resolve_qualified(raw, parts[0], parts[1], snap)
        return self._resolve_unqualified(raw, parts[0], snap)

    def search(
        self,
        keyword: str,
        connection: str | None = None,
        owner: str | None = None,
        scope: str = "all",
        limit: int = _SEARCH_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Keyword search over table/column names and comments."""
        if not isinstance(keyword, str) or not keyword.strip():
            raise BadParamError("keyword가 비어 있습니다")
        needle = keyword.strip().upper()
        if not isinstance(scope, str) or scope.strip().lower() not in _SEARCH_SCOPES:
            raise BadParamError(
                "scope는 all, table, column 중 하나여야 합니다",
            )
        scope_key = scope.strip().lower()
        cap = _require_limit(limit, default=_SEARCH_DEFAULT_LIMIT, cap=SEARCH_MAX_LIMIT)
        owner_key = None if owner is None else validate_identifier(owner, max_parts=1)
        names = self._connection_names(connection)
        matches: list[dict[str, Any]] = []
        truncated = False
        for conn_name in names:
            snap = self._peek(conn_name)
            for obj in snap.objects:
                if owner_key is not None and obj.owner != owner_key:
                    continue
                if scope_key in ("all", "table"):
                    if needle in obj.name.upper():
                        if _append_match(
                            matches,
                            cap,
                            connection=conn_name,
                            obj=obj,
                            match="table_name",
                            column=None,
                            col_type=None,
                            detail=None,
                        ):
                            truncated = True
                            return _search_payload(matches, truncated)
                    comment = obj.comment or ""
                    if comment and needle in comment.upper():
                        if _append_match(
                            matches,
                            cap,
                            connection=conn_name,
                            obj=obj,
                            match="table_comment",
                            column=None,
                            col_type=None,
                            detail=obj.comment,
                        ):
                            truncated = True
                            return _search_payload(matches, truncated)
                if scope_key in ("all", "column"):
                    for col in obj.columns:
                        if needle in col.name.upper():
                            if _append_match(
                                matches,
                                cap,
                                connection=conn_name,
                                obj=obj,
                                match="column",
                                column=col.name,
                                col_type=col.data_type,
                                detail=None,
                            ):
                                truncated = True
                                return _search_payload(matches, truncated)
                        col_comment = col.comment or ""
                        if col_comment and needle in col_comment.upper():
                            if _append_match(
                                matches,
                                cap,
                                connection=conn_name,
                                obj=obj,
                                match="column_comment",
                                column=col.name,
                                col_type=col.data_type,
                                detail=col.comment,
                            ):
                                truncated = True
                                return _search_payload(matches, truncated)
        return _search_payload(matches, truncated)

    def require_owner(self, connection: str, owner: str) -> str:
        """Return the canonical owner name, or fail closed when inaccessible."""
        conn = self._require_connection(connection)
        owner_key = validate_identifier(owner, max_parts=1)
        snap = self._peek(conn)
        for obj in snap.objects:
            if obj.owner == owner_key:
                return owner_key
        for syn in snap.synonyms:
            if syn.owner == owner_key:
                return owner_key
        raise UnknownObjectError(f"카탈로그에 없는 식별자: {owner_key}")

    def require_object(self, connection: str, table: str) -> ObjectInfo:
        """Return metadata for ``OWNER.TABLE``, or fail closed when inaccessible.

        도구가 SQL 을 조립하기 전 식별자를 인가하는 단일 관문이다.
        """
        resolved = self.resolve(connection, table)
        qualified = resolved.get("resolved")
        if not qualified or not resolved.get("accessible"):
            candidates = resolved.get("candidates") or []
            if candidates:
                raise UnknownObjectError(
                    f"식별자가 모호합니다: {table}",
                    hint="후보 중 하나를 OWNER.TABLE로 지정하세요",
                )
            raise UnknownObjectError(
                f"카탈로그에 없는 식별자: {validate_identifier(table, max_parts=2)}"
            )
        conn = self._require_connection(connection)
        snap = self._peek(conn)
        obj = snap.by_object.get(str(qualified))
        if obj is None:
            raise UnknownObjectError(f"카탈로그에 없는 식별자: {qualified}")
        return obj

    def require_column(
        self,
        connection: str,
        table: str,
        column: str | None = None,
    ) -> tuple[ObjectInfo, ColumnInfo]:
        """Return the canonical column name, or fail closed when it does not exist."""
        if column is None:
            parts = split_qualified_name(table, max_parts=3)
            if len(parts) != 3:
                raise BadParamError(
                    "컬럼은 OWNER.TABLE.COLUMN 또는 table+column으로 지정하세요",
                )
            obj = self.require_object(connection, f"{parts[0]}.{parts[1]}")
            col_name = parts[2]
        else:
            obj = self.require_object(connection, table)
            col_name = validate_identifier(column, max_parts=1)
        for col in obj.columns:
            if col.name == col_name:
                return obj, col
        raise UnknownObjectError(
            f"카탈로그에 없는 식별자: {obj.qualified}.{col_name}",
        )

    def partition_summary(self, connection: str, table: str) -> dict[str, Any]:
        """Partition type and key columns for one table (경계·행수는 partition_info)."""
        obj = self.require_object(connection, table)
        payload = obj.partition.as_dict()
        payload["owner"] = obj.owner
        payload["table"] = obj.name
        return payload

    def _refresh_one(self, name: str) -> dict[str, int]:
        lock = self._refresh_lock(name)
        with lock:
            started = time.perf_counter()
            snapshot = self._load_snapshot(name)
            with self._lock:
                self._snapshots[name] = snapshot
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            return {
                "tables": snapshot.table_count,
                "columns": snapshot.column_count,
                "elapsed_ms": elapsed_ms,
            }

    def _load_snapshot(self, name: str) -> CatalogSnapshot:
        cfg = self._db.connection_config(name)
        user = cfg.user.strip().upper()
        tables = _row_maps(self._query(name, _SQL_TABLES))
        views = _row_maps(self._query(name, _SQL_VIEWS))
        columns = _row_maps(self._query(name, _SQL_COLUMNS))
        tab_comments = _row_maps(self._query(name, _SQL_TAB_COMMENTS))
        col_comments = _row_maps(self._query(name, _SQL_COL_COMMENTS))
        synonyms = _row_maps(self._query(name, _SQL_SYNONYMS))
        part_tables = _row_maps(self._query(name, _SQL_PART_TABLES))
        part_keys = _row_maps(
            self._query(name, _SQL_PART_KEYS, binds={"object_type": "TABLE"})
        )
        constraints, cons_columns, indexes, ind_columns, part_indexes, metadata_available = (
            self._load_constraint_index_rows(name)
        )
        return _build_snapshot(
            connection=name,
            user=user,
            tables=tables,
            views=views,
            columns=columns,
            tab_comments=tab_comments,
            col_comments=col_comments,
            synonyms=synonyms,
            part_tables=part_tables,
            part_keys=part_keys,
            constraints=constraints,
            cons_columns=cons_columns,
            indexes=indexes,
            ind_columns=ind_columns,
            part_indexes=part_indexes,
            metadata_available=metadata_available,
        )

    def _load_constraint_index_rows(
        self, name: str
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        bool,
    ]:
        try:
            constraints = _row_maps(
                self._query(name, _SQL_CONSTRAINTS, binds={"ctype": "P"})
            )
            cons_columns = _row_maps(self._query(name, _SQL_CONS_COLUMNS))
            indexes = _row_maps(self._query(name, _SQL_INDEXES))
            ind_columns = _row_maps(self._query(name, _SQL_IND_COLUMNS))
            part_indexes = _row_maps(self._query(name, _SQL_PART_INDEXES))
        except NoAccessError:
            log.exception("catalog PK/index metadata unavailable: %s", name)
            return [], [], [], [], [], False
        return constraints, cons_columns, indexes, ind_columns, part_indexes, True

    def _query(
        self,
        connection: str,
        sql: str,
        *,
        binds: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        result = self._db.execute(
            connection,
            sql,
            binds=binds,
            max_rows=_CATALOG_FETCH_CAP,
        )
        if result.truncated:
            raise AdapterError("catalog dictionary query exceeded fetch cap")
        return result

    def _connection_names(self, connection: str | None) -> tuple[str, ...]:
        if connection is None:
            return self._db.connection_names()
        return (self._require_connection(connection),)

    def _require_connection(self, connection: str) -> str:
        cfg = self._db.connection_config(connection)
        return cfg.name

    def _peek(self, name: str) -> CatalogSnapshot:
        with self._lock:
            snap = self._snapshots.get(name)
        if snap is not None:
            return snap
        user = self._db.connection_config(name).user.strip().upper()
        return _empty_snapshot(name, user)

    def _refresh_lock(self, name: str) -> threading.Lock:
        with self._lock:
            lock = self._refresh_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._refresh_locks[name] = lock
            return lock

    def _resolve_qualified(
        self,
        original: str,
        owner: str,
        obj_name: str,
        snap: CatalogSnapshot,
    ) -> dict[str, Any]:
        key = f"{owner}.{obj_name}"
        obj = snap.by_object.get(key)
        if obj is not None:
            return {
                "input": original,
                "resolved": obj.qualified,
                "via": "DIRECT",
                "accessible": True,
                "type": obj.type,
            }
        syn = snap.by_synonym.get(key)
        if syn is not None:
            return _synonym_resolution(original, syn, snap)
        return {
            "input": original,
            "resolved": None,
            "candidates": [],
            "hint": "search_catalog 또는 catalog_refresh로 식별자를 확인하세요",
        }

    def _resolve_unqualified(
        self,
        original: str,
        obj_name: str,
        snap: CatalogSnapshot,
    ) -> dict[str, Any]:
        user_key = f"{snap.user}.{obj_name}"
        obj = snap.by_object.get(user_key)
        if obj is not None:
            return {
                "input": original,
                "resolved": obj.qualified,
                "via": "DIRECT",
                "accessible": True,
                "type": obj.type,
            }
        syn = snap.by_synonym.get(user_key)
        if syn is not None:
            return _synonym_resolution(original, syn, snap)
        public = snap.by_synonym.get(f"PUBLIC.{obj_name}")
        if public is not None:
            return _synonym_resolution(original, public, snap)
        objects = [item for item in snap.objects if item.name == obj_name]
        synonyms = [item for item in snap.synonyms if item.name == obj_name]
        if len(objects) == 1 and not synonyms:
            only = objects[0]
            return {
                "input": original,
                "resolved": only.qualified,
                "via": "DIRECT",
                "accessible": True,
                "type": only.type,
            }
        candidates: list[str] = []
        seen: set[str] = set()
        for item in objects:
            if item.qualified not in seen:
                seen.add(item.qualified)
                candidates.append(item.qualified)
        for item in synonyms:
            target = item.target
            if target not in seen:
                seen.add(target)
                candidates.append(target)
        if len(candidates) == 1:
            only_key = candidates[0]
            obj = snap.by_object.get(only_key)
            if obj is not None:
                via = "DIRECT"
                for item in synonyms:
                    if item.target == only_key:
                        via = _via_text(item)
                        break
                return {
                    "input": original,
                    "resolved": obj.qualified,
                    "via": via,
                    "accessible": True,
                    "type": obj.type,
                }
        hint = "후보 중 하나를 OWNER.TABLE로 지정하세요" if candidates else (
            "search_catalog 또는 catalog_refresh로 식별자를 확인하세요"
        )
        return {
            "input": original,
            "resolved": None,
            "candidates": candidates,
            "hint": hint,
        }


def _synonym_resolution(
    original: str,
    syn: SynonymInfo,
    snap: CatalogSnapshot,
) -> dict[str, Any]:
    target = snap.by_object.get(syn.target)
    accessible = target is not None and not syn.db_link
    return {
        "input": original,
        "resolved": syn.target if not syn.db_link else None,
        "via": _via_text(syn),
        "accessible": accessible,
        "type": None if target is None else target.type,
    }


def _via_text(syn: SynonymInfo) -> str:
    if syn.owner == "PUBLIC":
        return f"PUBLIC SYNONYM {syn.name}"
    return f"SYNONYM {syn.qualified}"


def _search_payload(matches: list[dict[str, Any]], truncated: bool) -> dict[str, Any]:
    return {
        "matches": matches,
        "row_count": len(matches),
        "truncated": truncated,
    }


def _append_match(
    matches: list[dict[str, Any]],
    cap: int,
    *,
    connection: str,
    obj: ObjectInfo,
    match: str,
    column: str | None,
    col_type: str | None,
    detail: str | None,
) -> bool:
    if len(matches) >= cap:
        return True
    matches.append(
        {
            "connection": connection,
            "owner": obj.owner,
            "object": obj.name,
            "object_type": obj.type,
            "match": match,
            "column": column,
            "type": col_type,
            "detail": detail,
        }
    )
    return False


def _build_snapshot(
    *,
    connection: str,
    user: str,
    tables: list[dict[str, Any]],
    views: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    tab_comments: list[dict[str, Any]],
    col_comments: list[dict[str, Any]],
    synonyms: list[dict[str, Any]],
    part_tables: list[dict[str, Any]],
    part_keys: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
    cons_columns: list[dict[str, Any]],
    indexes: list[dict[str, Any]],
    ind_columns: list[dict[str, Any]],
    part_indexes: list[dict[str, Any]],
    metadata_available: bool,
) -> CatalogSnapshot:
    comments = {
        f"{_ident(row.get('OWNER'))}.{_ident(row.get('TABLE_NAME'))}": _text(
            row.get("COMMENTS")
        )
        for row in tab_comments
        if _ident(row.get("OWNER")) and _ident(row.get("TABLE_NAME"))
    }
    col_comment_map = {
        (
            f"{_ident(row.get('OWNER'))}.{_ident(row.get('TABLE_NAME'))}."
            f"{_ident(row.get('COLUMN_NAME'))}"
        ): _text(row.get("COMMENTS"))
        for row in col_comments
        if _ident(row.get("OWNER"))
        and _ident(row.get("TABLE_NAME"))
        and _ident(row.get("COLUMN_NAME"))
    }
    part_map: dict[str, dict[str, Any]] = {}
    for row in part_tables:
        owner = _ident(row.get("OWNER"))
        table = _ident(row.get("TABLE_NAME"))
        if not owner or not table:
            continue
        part_map[f"{owner}.{table}"] = {
            "type": _text(row.get("PARTITIONING_TYPE")),
            "subpartition_type": _text(row.get("SUBPARTITIONING_TYPE")),
            "partition_count": as_int(row.get("PARTITION_COUNT")),
            "interval": _text(row.get("INTERVAL")),
            "keys": [],
        }
    key_rows = sorted(
        part_keys,
        key=lambda row: (
            _ident(row.get("OWNER")) or "",
            _ident(row.get("NAME")) or "",
            as_int(row.get("COLUMN_POSITION")) or 0,
        ),
    )
    for row in key_rows:
        owner = _ident(row.get("OWNER"))
        table = _ident(row.get("NAME"))
        column = _ident(row.get("COLUMN_NAME"))
        if not owner or not table or not column:
            continue
        entry = part_map.setdefault(
            f"{owner}.{table}",
            {
                "type": None,
                "subpartition_type": None,
                "partition_count": None,
                "interval": None,
                "keys": [],
            },
        )
        keys: list[str] = entry["keys"]
        if column not in keys:
            keys.append(column)

    objects: dict[str, dict[str, Any]] = {}
    for row in tables:
        owner = _ident(row.get("OWNER"))
        table = _ident(row.get("TABLE_NAME"))
        if not owner or not table:
            continue
        key = f"{owner}.{table}"
        analyzed_full = _text(row.get("LAST_ANALYZED"))
        objects[key] = {
            "owner": owner,
            "name": table,
            "type": "TABLE",
            "comment": comments.get(key),
            "num_rows": as_int(row.get("NUM_ROWS")),
            "last_analyzed": _as_date_str(analyzed_full),
            "last_analyzed_full": analyzed_full,
            "partitioned": _as_yes(row.get("PARTITIONED")),
        }
    for row in views:
        owner = _ident(row.get("OWNER"))
        table = _ident(row.get("VIEW_NAME"))
        if not owner or not table:
            continue
        key = f"{owner}.{table}"
        if key in objects:
            continue
        objects[key] = {
            "owner": owner,
            "name": table,
            "type": "VIEW",
            "comment": comments.get(key),
            "num_rows": None,
            "last_analyzed": None,
            "last_analyzed_full": None,
            "partitioned": False,
        }

    columns_by_obj: dict[str, list[ColumnInfo]] = {key: [] for key in objects}
    pending_cols: list[tuple[str, ColumnInfo]] = []
    for row in columns:
        owner = _ident(row.get("OWNER"))
        table = _ident(row.get("TABLE_NAME"))
        col_name = _ident(row.get("COLUMN_NAME"))
        if not owner or not table or not col_name:
            continue
        key = f"{owner}.{table}"
        col = ColumnInfo(
            name=col_name,
            data_type=_format_data_type(row),
            nullable=_as_nullable(row.get("NULLABLE")),
            data_default=_text(row.get("DATA_DEFAULT")),
            comment=col_comment_map.get(f"{key}.{col_name}"),
            column_id=as_int(row.get("COLUMN_ID")) or 0,
        )
        pending_cols.append((key, col))
    for key, col in pending_cols:
        bucket = columns_by_obj.get(key)
        if bucket is None:
            continue
        bucket.append(col)
    for bucket in columns_by_obj.values():
        bucket.sort(key=lambda item: item.column_id)
    pk_by_obj, indexes_by_obj = _constraint_index_maps(
        constraints, cons_columns, indexes, ind_columns, part_indexes
    )

    object_list: list[ObjectInfo] = []
    by_object: dict[str, ObjectInfo] = {}
    for key, raw in objects.items():
        part_raw = part_map.get(key)
        partitioned = bool(raw["partitioned"] or part_raw)
        if part_raw is None:
            partition = PartitionSummary(
                partitioned=partitioned,
                type=None,
                interval=None,
                subpartition_type=None,
                key_columns=(),
                partition_count=None,
            )
        else:
            partition = PartitionSummary(
                partitioned=True,
                type=part_raw["type"],
                interval=part_raw["interval"],
                subpartition_type=part_raw["subpartition_type"],
                key_columns=tuple(part_raw["keys"]),
                partition_count=part_raw["partition_count"],
            )
        cols = tuple(columns_by_obj.get(key, []))
        pk_cols = pk_by_obj.get(key, ())
        idx_list = indexes_by_obj.get(key, ())
        info = ObjectInfo(
            owner=raw["owner"],
            name=raw["name"],
            type=raw["type"],
            comment=raw["comment"],
            num_rows=raw["num_rows"],
            last_analyzed=raw["last_analyzed"],
            last_analyzed_full=raw["last_analyzed_full"],
            partition=partition,
            columns=cols,
            primary_key=pk_cols,
            indexes=idx_list,
            metadata_available=metadata_available,
        )
        object_list.append(info)
        by_object[key] = info
    object_list.sort(key=lambda item: (item.owner, item.name))

    synonym_list: list[SynonymInfo] = []
    by_synonym: dict[str, SynonymInfo] = {}
    for row in synonyms:
        owner = _ident(row.get("OWNER"))
        syn_name = _ident(row.get("SYNONYM_NAME"))
        table_owner = _ident(row.get("TABLE_OWNER"))
        table_name = _ident(row.get("TABLE_NAME"))
        if not owner or not syn_name or not table_owner or not table_name:
            continue
        info = SynonymInfo(
            owner=owner,
            name=syn_name,
            table_owner=table_owner,
            table_name=table_name,
            db_link=_text(row.get("DB_LINK")),
        )
        synonym_list.append(info)
        by_synonym[info.qualified] = info
    synonym_list.sort(key=lambda item: (item.owner, item.name))

    owners = {item.owner for item in object_list}
    column_count = sum(len(item.columns) for item in object_list)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return CatalogSnapshot(
        connection=connection,
        user=user,
        refreshed_at=now,
        refreshed_monotonic=time.monotonic(),
        objects=tuple(object_list),
        synonyms=tuple(synonym_list),
        by_object=MappingProxyType(by_object),
        by_synonym=MappingProxyType(by_synonym),
        schema_count=len(owners),
        table_count=len(object_list),
        column_count=column_count,
    )


# 스냅샷 빌드용: 항상 전 행을 받아 list 만 돌려준다. 잘림 여부까지 돌려주는
# common.results._row_maps 와는 반환 계약이 다르다 (이름만 같음).
def _row_maps(result: QueryResult) -> list[dict[str, Any]]:
    cols = tuple(str(name).upper() for name in result.columns)
    mapped: list[dict[str, Any]] = []
    for row in result.rows:
        mapped.append({cols[i]: row[i] if i < len(row) else None for i in range(len(cols))})
    return mapped


# 카탈로그 행의 식별자를 관대하게 정규화한다(실패 시 None). JSON 로드 오류를
# 던지는 extensions._ident 와는 다른 함수다 (이름만 같음).
def _ident(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return validate_identifier(text, max_parts=1)
    except (BadParamError, UnknownObjectError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _as_yes(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().upper() in {"Y", "YES"}


def _as_nullable(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().upper() != "N"


# 이미 문자열로 변환된 LAST_ANALYZED 만 다듬는다. datetime 을 다루는
# common.results._as_date_str 와는 계약이 다르다 (이름만 같음).
def _as_date_str(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    return value


def _format_data_type(row: Mapping[str, Any]) -> str:
    raw = row.get("DATA_TYPE")
    if raw is None:
        return ""
    data_type = str(raw).strip().upper()
    length = as_int(row.get("CHAR_LENGTH")) or as_int(row.get("DATA_LENGTH"))
    precision = as_int(row.get("DATA_PRECISION"))
    scale = as_int(row.get("DATA_SCALE"))
    if data_type in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"}:
        return f"{data_type}({length})" if length is not None else data_type
    if data_type == "NUMBER":
        if precision is None:
            return "NUMBER"
        if scale in (None, 0):
            return f"NUMBER({precision})"
        return f"NUMBER({precision},{scale})"
    if data_type.startswith("TIMESTAMP") and scale is not None:
        return f"{data_type}({scale})"
    if data_type in {"FLOAT"} and precision is not None:
        return f"FLOAT({precision})"
    return data_type

def _constraint_index_maps(
    constraints: list[dict[str, Any]],
    cons_columns: list[dict[str, Any]],
    indexes: list[dict[str, Any]],
    ind_columns: list[dict[str, Any]],
    part_indexes: list[dict[str, Any]],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[IndexInfo, ...]]]:
    pk_names: dict[str, str] = {}
    for row in constraints:
        owner = _ident(row.get("OWNER"))
        table = _ident(row.get("TABLE_NAME"))
        name = _ident(row.get("CONSTRAINT_NAME"))
        if not owner or not table or not name:
            continue
        if str(row.get("CONSTRAINT_TYPE") or "").strip().upper() != "P":
            continue
        pk_names[f"{owner}.{name}"] = f"{owner}.{table}"

    pk_cols: dict[str, list[tuple[int, str]]] = {}
    for row in cons_columns:
        owner = _ident(row.get("OWNER"))
        table = _ident(row.get("TABLE_NAME"))
        cons = _ident(row.get("CONSTRAINT_NAME"))
        column = _ident(row.get("COLUMN_NAME"))
        if not owner or not table or not cons or not column:
            continue
        table_key = pk_names.get(f"{owner}.{cons}")
        if table_key is None:
            continue
        pos = as_int(row.get("POSITION")) or 0
        bucket = pk_cols.setdefault(table_key, [])
        if column not in {item[1] for item in bucket}:
            bucket.append((pos, column))
    pk_by_obj = {
        key: tuple(name for _pos, name in sorted(cols, key=lambda item: item[0]))
        for key, cols in pk_cols.items()
    }

    locality: dict[str, str] = {}
    for row in part_indexes:
        owner = _ident(row.get("OWNER"))
        name = _ident(row.get("INDEX_NAME"))
        if not owner or not name:
            continue
        loc = _text(row.get("LOCALITY"))
        if loc:
            locality[f"{owner}.{name}"] = loc.upper()

    index_meta: dict[str, dict[str, Any]] = {}
    for row in indexes:
        owner = _ident(row.get("OWNER"))
        name = _ident(row.get("INDEX_NAME"))
        table_owner = _ident(row.get("TABLE_OWNER"))
        table = _ident(row.get("TABLE_NAME"))
        if not owner or not name or not table_owner or not table:
            continue
        loc = locality.get(f"{owner}.{name}")
        index_meta[f"{owner}.{name}"] = {
            "table": f"{table_owner}.{table}",
            "name": name,
            "unique": str(row.get("UNIQUENESS") or "").strip().upper() == "UNIQUE",
            "local": loc == "LOCAL",
            "columns": [],
        }

    for row in ind_columns:
        owner = _ident(row.get("INDEX_OWNER"))
        name = _ident(row.get("INDEX_NAME"))
        column = _ident(row.get("COLUMN_NAME"))
        if not owner or not name or not column:
            continue
        entry = index_meta.get(f"{owner}.{name}")
        if entry is None:
            table_owner = _ident(row.get("TABLE_OWNER"))
            table = _ident(row.get("TABLE_NAME"))
            if not table_owner or not table:
                continue
            loc = locality.get(f"{owner}.{name}")
            entry = {
                "table": f"{table_owner}.{table}",
                "name": name,
                "unique": False,
                "local": loc == "LOCAL",
                "columns": [],
            }
            index_meta[f"{owner}.{name}"] = entry
        cols: list[str] = entry["columns"]
        if column not in cols:
            cols.append(column)

    indexes_by_obj: dict[str, list[IndexInfo]] = {}
    for entry in index_meta.values():
        info = IndexInfo(
            name=entry["name"],
            columns=tuple(entry["columns"]),
            unique=bool(entry["unique"]),
            local=bool(entry["local"]),
        )
        indexes_by_obj.setdefault(entry["table"], []).append(info)
    return pk_by_obj, {
        key: tuple(items) for key, items in indexes_by_obj.items()
    }


def _require_like_pattern(pattern: str | None) -> str:
    if pattern is None or pattern == "":
        return "%"
    if not isinstance(pattern, str):
        raise BadParamError("pattern은 문자열이어야 합니다")
    if '"' in pattern or "'" in pattern or ";" in pattern:
        raise BadParamError("pattern에 따옴표 또는 세미콜론을 사용할 수 없습니다")
    return pattern


def _oracle_like(value: str, pattern: str) -> bool:
    if pattern == "%":
        return True
    pieces = ["^"]
    for ch in pattern:
        if ch == "%":
            pieces.append(".*")
        elif ch == "_":
            pieces.append(".")
        else:
            pieces.append(re.escape(ch))
    pieces.append("$")
    return re.match("".join(pieces), value, re.IGNORECASE) is not None


def _require_limit(limit: Any, *, default: int, cap: int) -> int:
    if limit is None:
        return default
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise BadParamError("limit는 1 이상의 정수여야 합니다")
    if limit > cap:
        return cap
    return limit
