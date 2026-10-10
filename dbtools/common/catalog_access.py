"""Catalog lookups: object resolution, owner/name derivation, column and listing shaping.

``CatalogCache`` (see :mod:`dbtools.catalog`) is the only catalog implementation,
so its methods are called directly by their real signatures. Depends on
``results`` for attr/key access; must not import ``execution`` or ``params``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import UnknownObjectError
from ..sql_safety import split_qualified_name, validate_identifier
from .results import attr_or_key


LOB_TYPES = frozenset(
    {"CLOB", "NCLOB", "BLOB", "LONG", "LONG RAW", "BFILE", "JSON", "XMLTYPE"}
)


def require_object(catalog: Any, connection: str, table: str) -> Any:
    """Resolve ``OWNER.TABLE`` to catalog metadata, failing closed when absent."""
    return catalog.require_object(connection, table)


def require_column(catalog: Any, connection: str, table: str, column: str) -> str:
    """Authorize one column against the catalog and return it canonicalized."""
    ident = validate_identifier(column, max_parts=1)
    _obj, col = catalog.require_column(connection, table, ident)
    return col.name


def require_owner(catalog: Any, connection: str, owner: str) -> str:
    """Authorize one schema owner against the catalog and return it canonicalized."""
    ident = validate_identifier(owner, max_parts=1)
    return catalog.require_owner(connection, ident)


def owner_name(obj: Any, table: str) -> tuple[str, str]:
    """Split catalog metadata into (owner, name), falling back to the input name.

    ``name``/``table`` are both accepted because ``describe`` payloads key the
    object as ``table`` while ``ObjectInfo`` exposes ``name``.
    """
    owner = attr_or_key(obj, "owner")
    name = attr_or_key(obj, "name") or attr_or_key(obj, "table")
    if isinstance(owner, str) and isinstance(name, str) and owner and name:
        return validate_identifier(owner, max_parts=1), validate_identifier(name, max_parts=1)
    parts = split_qualified_name(table, max_parts=2)
    if len(parts) != 2:
        raise UnknownObjectError(
            f"카탈로그에 없는 식별자: {table}",
            hint="OWNER.TABLE 형식으로 지정하세요",
        )
    return parts[0], parts[1]


def object_column_names(obj: Any) -> set[str]:
    """Column names of one catalog object, for authorizing a restricted predicate."""
    names: set[str] = set()
    for col in attr_or_key(obj, "columns", ()) or ():
        name = attr_or_key(col, "name")
        if isinstance(name, str) and name:
            names.add(validate_identifier(name, max_parts=1))
    if not names:
        raise UnknownObjectError("카탈로그에 컬럼 메타데이터가 없습니다")
    return names


def object_columns(obj: Any) -> list[dict[str, Any]]:
    """Normalize catalog columns to name/type/nullable/comment dicts in column order."""
    raw = attr_or_key(obj, "columns")
    if raw is None:
        return []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, Mapping):
            name = item.get("name")
            data_type = item.get("type") or item.get("data_type") or ""
            nullable = item.get("nullable", True)
            comment = item.get("comment")
            column_id = item.get("column_id", index + 1)
        else:
            name = getattr(item, "name", None)
            data_type = getattr(item, "data_type", None) or getattr(item, "type", "")
            nullable = getattr(item, "nullable", True)
            comment = getattr(item, "comment", None)
            column_id = getattr(item, "column_id", index + 1)
        if not isinstance(name, str) or not name.strip():
            continue
        ident = validate_identifier(name, max_parts=1)
        out.append(
            {
                "name": ident,
                "type": str(data_type or ""),
                "nullable": bool(nullable),
                "comment": comment if isinstance(comment, str) else None,
                "column_id": int(column_id) if isinstance(column_id, int) and not isinstance(column_id, bool) else index + 1,
            }
        )
    out.sort(key=lambda row: row["column_id"])
    return out


def list_owner_objects(
    catalog: Any,
    connection: str,
    owner: str,
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """List one owner's tables and views as (owner, name) dicts plus a truncation flag."""
    payload = catalog.list_objects(
        connection,
        owner=owner,
        pattern="%",
        include_views=True,
        limit=limit,
    )
    return payload["tables"], payload["truncated"]


__all__ = [
    "LOB_TYPES",
    "list_owner_objects",
    "object_column_names",
    "object_columns",
    "owner_name",
    "require_column",
    "require_object",
    "require_owner",
]
