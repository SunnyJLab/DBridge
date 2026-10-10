"""Deterministic SELECT/WITH guardrails and unquoted Oracle identifier helpers.

User SQL is accepted only when a character scanner (not a regex over the raw
text) can prove it is a single ``SELECT`` or ``WITH`` statement. Comments and
literals are skipped with Oracle ``--``, ``/* */``, ``'...'``, ``n'...'``, and
``q'[...]'`` / ``nq'[...]'`` rules. Ambiguous or unterminated input fails closed.

``authorize_select_sql`` is the fail-closed catalog/bind authorizer used by
``query``. Statement-shape validation alone is not sufficient.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import AbstractSet, Any

from .errors import BadParamError, InvalidSqlError, UnknownObjectError

_MAX_UNQUOTED_IDENT = 128
_UNQUOTED_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$#]")
_WS = frozenset(" \t\r\n\f\v")
_Q_PAIR = {"[": "]", "(": ")", "{": "}", "<": ">"}

ROW_LIMIT_BIND = "dbtools_row_limit"

# Statement verbs and PL/SQL introducers that must not appear in SELECT/WITH.
# CASE/WHEN/END/JOIN/INTO-as-column are handled by whole-token matching;
# INTO is rejected because SELECT INTO / RETURNING INTO are not read-only SQL.
_FORBIDDEN_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "RENAME",
        "COMMENT",
        "GRANT",
        "REVOKE",
        "AUDIT",
        "NOAUDIT",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "BEGIN",
        "DECLARE",
        "CALL",
        "EXECUTE",
        "EXEC",
        "EXPLAIN",
        "LOCK",
        "ANALYZE",
        "FLASHBACK",
        "PURGE",
        "ASSOCIATE",
        "DISASSOCIATE",
        "SET",
        "FUNCTION",
        "PROCEDURE",
        "PACKAGE",
        "TRIGGER",
        "PRAGMA",
        "INTO",
        "UPSERT",
    }
)

_CLAUSE_STOP = frozenset(
    {
        "WHERE",
        "GROUP",
        "HAVING",
        "ORDER",
        "UNION",
        "INTERSECT",
        "MINUS",
        "FETCH",
        "OFFSET",
        "FOR",
        "WITH",
        "CONNECT",
        "START",
        "MODEL",
        "PIVOT",
        "UNPIVOT",
        "MATCH_RECOGNIZE",
        "SAMPLE",
        "VERSIONS",
    }
)

_JOIN_INTRO = frozenset(
    {
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "CROSS",
        "NATURAL",
        "OUTER",
    }
)

_PRED_STOP = frozenset(
    {
        "AND",
        "OR",
        "THEN",
        "ELSE",
        "WHEN",
        "END",
        "GROUP",
        "HAVING",
        "ORDER",
        "UNION",
        "INTERSECT",
        "MINUS",
        "FETCH",
        "OFFSET",
        "FOR",
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "CROSS",
        "NATURAL",
        "ON",
        "USING",
        "WHERE",
        "CONNECT",
        "START",
        "FROM",
        "SELECT",
        "WITH",
        "RETURNING",
        "INTO",
    }
)

_PSEUDOCOLUMNS = frozenset(
    {
        "ROWNUM",
        "ROWID",
        "ORA_ROWSCN",
        "LEVEL",
        "CONNECT_BY_ISLEAF",
        "CONNECT_BY_ISCYCLE",
        "COLUMN_VALUE",
        "OBJECT_ID",
        "OBJECT_VALUE",
    }
)

_KEYWORD_ATOMS = frozenset(
    {
        "NULL",
        "SYSDATE",
        "SYSTIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "LOCALTIMESTAMP",
        "USER",
        "UID",
        "TRUE",
        "FALSE",
        "SESSIONTIMEZONE",
        "DBTIMEZONE",
    }
)

_FORMAT_LITERAL_FUNCS = frozenset(
    {
        "TO_DATE",
        "TO_TIMESTAMP",
        "TO_TIMESTAMP_TZ",
        "TO_CHAR",
        "TO_NUMBER",
        "TRUNC",
        "ROUND",
        "NUMTODSINTERVAL",
        "NUMTOYMINTERVAL",
        "TO_DSINTERVAL",
        "TO_YMINTERVAL",
    }
)

_READONLY_CALLS = frozenset(
    {
        "ABS",
        "ADD_MONTHS",
        "ASCII",
        "ASCIISTR",
        "AVG",
        "BIN_TO_NUM",
        "BITAND",
        "CAST",
        "CEIL",
        "CHR",
        "COALESCE",
        "CONCAT",
        "COUNT",
        "CUME_DIST",
        "DECODE",
        "DENSE_RANK",
        "DUMP",
        "EXP",
        "EXTRACT",
        "FIRST_VALUE",
        "FLOOR",
        "FROM_TZ",
        "GREATEST",
        "GROUPING",
        "GROUPING_ID",
        "GROUP_ID",
        "HEXTORAW",
        "INITCAP",
        "INSTR",
        "INSTRB",
        "JSON_ARRAY",
        "JSON_EXISTS",
        "JSON_OBJECT",
        "JSON_QUERY",
        "JSON_SCALAR",
        "JSON_SERIALIZE",
        "JSON_VALUE",
        "LAG",
        "LAST_DAY",
        "LAST_VALUE",
        "LEAD",
        "LEAST",
        "LENGTH",
        "LENGTHB",
        "LISTAGG",
        "LN",
        "LOG",
        "LOWER",
        "LPAD",
        "LTRIM",
        "MAX",
        "MEDIAN",
        "MIN",
        "MOD",
        "MONTHS_BETWEEN",
        "NANVL",
        "NCHR",
        "NEXT_DAY",
        "NLS_INITCAP",
        "NLS_LOWER",
        "NLS_UPPER",
        "NLSSORT",
        "NTH_VALUE",
        "NTILE",
        "NULLIF",
        "NUMTODSINTERVAL",
        "NUMTOYMINTERVAL",
        "NVL",
        "NVL2",
        "PERCENTILE_CONT",
        "PERCENTILE_DISC",
        "PERCENT_RANK",
        "POWER",
        "RANK",
        "RATIO_TO_REPORT",
        "RAWTOHEX",
        "REGEXP_COUNT",
        "REGEXP_INSTR",
        "REGEXP_LIKE",
        "REGEXP_REPLACE",
        "REGEXP_SUBSTR",
        "REMAINDER",
        "REPLACE",
        "REVERSE",
        "ROUND",
        "ROW_NUMBER",
        "RPAD",
        "RTRIM",
        "SIGN",
        "SOUNDEX",
        "SQRT",
        "STDDEV",
        "STDDEV_POP",
        "STDDEV_SAMP",
        "SUBSTR",
        "SUBSTRB",
        "SUM",
        "SYS_EXTRACT_UTC",
        "TO_CHAR",
        "TO_CLOB",
        "TO_DATE",
        "TO_DSINTERVAL",
        "TO_MULTI_BYTE",
        "TO_NCHAR",
        "TO_NCLOB",
        "TO_NUMBER",
        "TO_SINGLE_BYTE",
        "TO_TIMESTAMP",
        "TO_TIMESTAMP_TZ",
        "TO_YMINTERVAL",
        "TRANSLATE",
        "TRIM",
        "TRUNC",
        "TZ_OFFSET",
        "UNISTR",
        "UPPER",
        "VARIANCE",
        "VAR_POP",
        "VAR_SAMP",
        "VSIZE",
        "WIDTH_BUCKET",
    }
)

_COMPARE_OPS = frozenset({"=", "<", ">", "<=", ">=", "<>", "!="})

_TYPE_WORDS = frozenset(
    {
        "VARCHAR",
        "VARCHAR2",
        "NVARCHAR2",
        "CHAR",
        "NCHAR",
        "NUMBER",
        "FLOAT",
        "BINARY_FLOAT",
        "BINARY_DOUBLE",
        "DATE",
        "TIMESTAMP",
        "RAW",
        "BLOB",
        "CLOB",
        "NCLOB",
        "LONG",
        "INTEGER",
        "INT",
        "SMALLINT",
        "DECIMAL",
        "NUMERIC",
        "REAL",
        "DOUBLE",
        "PRECISION",
        "BOOLEAN",
        "JSON",
        "XMLTYPE",
        "ROWID",
        "UROWID",
        "INTERVAL",
        "YEAR",
        "MONTH",
        "DAY",
        "SECOND",
        "HOUR",
        "MINUTE",
        "TIME",
        "ZONE",
        "LOCAL",
        "WITH",
        "BYTE",
        "CHARACTER",
    }
)


def strip_leading_comments(sql: str) -> tuple[str, int]:
    """Strip leading whitespace and Oracle comments.

    Returns ``(remainder, index_in_original)``. Unterminated leading comments
    raise :class:`InvalidSqlError`.
    """
    if not isinstance(sql, str):
        raise InvalidSqlError("SQL은 문자열이어야 합니다", position=0)
    scanner = _Scanner(sql)
    scanner.skip_whitespace_and_comments()
    return sql[scanner.i :], scanner.i


def validate_select_sql(sql: str) -> str:
    """Validate user SQL as a single SELECT/WITH statement.

    Leading comments are stripped in the returned string. Semicolons, DML/DDL/
    PL/SQL keywords, ``FOR UPDATE``, and non-SELECT/WITH roots are rejected.
    The returned text is the original SQL with only leading trivia removed;
    interior comments and literals are preserved.
    """
    if not isinstance(sql, str):
        raise InvalidSqlError("SQL은 문자열이어야 합니다", position=0)
    if sql == "":
        raise InvalidSqlError("빈 SQL입니다", position=0)

    scanner = _Scanner(sql)
    first = scanner.first_code_keyword()
    if first is None:
        raise InvalidSqlError("빈 SQL입니다", position=scanner.i)
    word, pos = first
    if word not in ("SELECT", "WITH"):
        raise InvalidSqlError(
            f"SELECT/WITH 단일 문장만 허용됩니다 (시작 토큰: {word})",
            position=pos,
        )
    scanner.scan_body()
    stripped, start = strip_leading_comments(sql)
    if not stripped.strip():
        raise InvalidSqlError("빈 SQL입니다", position=start)
    return stripped


def split_qualified_name(name: str, *, max_parts: int = 3) -> tuple[str, ...]:
    """Split ``OWNER[.TABLE[.COLUMN]]`` into uppercased unquoted parts."""
    if not isinstance(name, str) or not name.strip():
        raise BadParamError(
            "식별자가 비어 있습니다",
            hint="OWNER, OWNER.TABLE, OWNER.TABLE.COLUMN 형식의 미인용 식별자만 허용됩니다",
        )
    if name.strip() != name:
        raise BadParamError(
            "식별자 앞뒤에 공백을 둘 수 없습니다",
            hint="OWNER.TABLE 형식의 미인용 식별자만 허용됩니다",
        )
    if '"' in name or "'" in name or "`" in name:
        raise BadParamError(
            "인용된 식별자는 허용되지 않습니다",
            hint="Oracle 미인용 식별자만 허용됩니다",
        )
    if max_parts < 1:
        raise BadParamError("max_parts는 1 이상이어야 합니다")
    raw_parts = name.split(".")
    if len(raw_parts) > max_parts:
        raise BadParamError(
            f"식별자 구성 요소가 {max_parts}개를 초과합니다: {name}",
            hint="OWNER.TABLE 또는 OWNER.TABLE.COLUMN만 허용됩니다",
        )
    parts: list[str] = []
    for raw in raw_parts:
        if raw == "":
            raise BadParamError(
                f"빈 식별자 구성 요소가 있습니다: {name}",
                hint="OWNER.TABLE 형식의 미인용 식별자만 허용됩니다",
            )
        parts.append(_require_unquoted(raw))
    return tuple(parts)


def validate_identifier(
    name: str,
    *,
    allowed: AbstractSet[str] | None = None,
    max_parts: int = 3,
) -> str:
    """Return ``OWNER[.TABLE[.COLUMN]]`` uppercased after unquoted validation.

    When ``allowed`` is provided, the normalized name must be a member of that
    set (compared case-insensitively). Missing membership is
    :class:`UnknownObjectError`; malformed names are :class:`BadParamError`.
    """
    parts = split_qualified_name(name, max_parts=max_parts)
    qualified = ".".join(parts)
    if allowed is not None:
        normalized_allowed = {_normalize_allowed_entry(item) for item in allowed}
        if qualified not in normalized_allowed:
            raise UnknownObjectError(
                f"카탈로그에 없는 식별자: {qualified}",
                hint="search_catalog 또는 catalog_refresh로 식별자를 확인하세요",
            )
    return qualified


def quote_identifier(
    name: str,
    *,
    allowed: AbstractSet[str] | None = None,
    max_parts: int = 3,
) -> str:
    """Validate an unquoted name and return double-quoted Oracle identifier(s).

    Input must still be an unquoted identifier; quoting is only applied to the
    validated uppercase form so it can be embedded in SQL.
    """
    parts = split_qualified_name(name, max_parts=max_parts)
    qualified = ".".join(parts)
    if allowed is not None:
        validate_identifier(qualified, allowed=allowed, max_parts=max_parts)
    return ".".join(f'"{part}"' for part in parts)


def quote_qualified_name(
    parts: Sequence[str],
    *,
    allowed: AbstractSet[str] | None = None,
) -> str:
    """Quote already-split unquoted parts as ``"OWNER"."TABLE"``."""
    if not parts:
        raise BadParamError("식별자가 비어 있습니다")
    return quote_identifier(".".join(parts), allowed=allowed, max_parts=len(parts))


def authorize_select_sql(sql: str, catalog: Any, connection: str) -> str:
    """Fail-closed catalog/bind authorization for a SELECT/WITH statement.

    ``catalog`` is the selected connection's CatalogCache (or compatible).
    Shape validation still runs first; this pass then tokenizes the statement
    and checks objects, columns, callables, and value binds.
    """
    stripped = validate_select_sql(sql)
    tokens = tokenize_sql(stripped)
    _reject_reserved_bind_tokens(tokens)
    parser = _SelectParser(tokens, catalog, connection)
    parser.parse_statement()
    return stripped


def wrap_select_rownum_cap(sql: str, *, bind_name: str = ROW_LIMIT_BIND) -> str:
    """Wrap authorized SQL so a server-side ROWNUM cap always applies."""
    return f"SELECT * FROM ({sql}) WHERE ROWNUM <= :{bind_name}"


def sql_mentions_bind(sql: str, name: str) -> bool:
    """True when ``sql`` contains a real bind placeholder named ``name``."""
    target = name.upper()
    for token in tokenize_sql(sql):
        if token.kind == "BIND" and token.upper == target:
            return True
    return False


def render_restricted_where(
    where: str,
    *,
    allowed_columns: AbstractSet[str],
    table_name: str | None = None,
    owner: str | None = None,
    binds: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Parse a count_rows predicate and render catalog-safe SQL.

    Allowed: parentheses, AND/OR/NOT, comparisons to named binds, IS [NOT] NULL,
    [NOT] IN (:b, ...), [NOT] BETWEEN :a AND :b, [NOT] LIKE :b. Comments,
    literals, functions, subqueries, and clause keywords are rejected.
    Returned binds are exactly those referenced; extras/missing fail closed.
    """
    if not isinstance(where, str) or not where.strip():
        raise BadParamError(
            "where가 비어 있습니다",
            hint="제한된 술어 문법으로 where를 전달하세요",
        )
    tokens = tokenize_sql(where, allow_comments=False)
    _reject_reserved_bind_tokens(tokens)
    parser = _WhereParser(
        tokens,
        allowed_columns={item.upper() for item in allowed_columns},
        table_name=None if table_name is None else table_name.upper(),
        owner=None if owner is None else owner.upper(),
    )
    rendered = parser.parse()
    used = parser.bind_names
    provided = {key: value for key, value in binds.items()}
    provided_upper = {key.upper(): key for key in provided}
    missing = [name for name in sorted(used) if name.upper() not in provided_upper]
    extra = [key for key in provided if key.upper() not in {n.upper() for n in used}]
    if missing or extra:
        raise BadParamError(
            "where 바인드가 선언과 일치하지 않습니다",
            hint="술어에 등장하는 :name만 binds에 전달하세요",
        )
    selected = {provided_upper[name.upper()]: provided[provided_upper[name.upper()]] for name in used}
    return rendered, selected


def _require_unquoted(part: str) -> str:
    if not _UNQUOTED_IDENT.match(part) or len(part) > _MAX_UNQUOTED_IDENT:
        raise BadParamError(
            f"Oracle 미인용 식별자가 아닙니다: {part!r}",
            hint="문자로 시작하고 문자·숫자·_·$·# 만 사용하며 최대 128자입니다",
        )
    return part.upper()


def _normalize_allowed_entry(item: str) -> str:
    if not isinstance(item, str) or not item.strip():
        raise BadParamError(
            "allowed 집합의 항목이 비어 있습니다",
            hint="카탈로그 식별자는 OWNER.TABLE 형식의 미인용 이름이어야 합니다",
        )
    return ".".join(split_qualified_name(item))


class _Scanner:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.n = len(sql)
        self.i = 0

    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.sql[j] if j < self.n else ""

    def skip_whitespace(self) -> None:
        while self.i < self.n and self.sql[self.i] in _WS:
            self.i += 1

    def skip_whitespace_and_comments(self) -> None:
        while self.i < self.n:
            self.skip_whitespace()
            if self.i >= self.n:
                return
            if self.peek() == "-" and self.peek(1) == "-":
                self._skip_line_comment()
                continue
            if self.peek() == "/" and self.peek(1) == "*":
                self._skip_block_comment()
                continue
            return

    def first_code_keyword(self) -> tuple[str, int] | None:
        self.skip_whitespace_and_comments()
        if self.i >= self.n:
            return None
        pos = self.i
        token = self._read_code_token_for_root()
        return (token, pos) if token is not None else None

    def scan_body(self) -> None:
        prev_keyword: str | None = None
        while self.i < self.n:
            self.skip_whitespace()
            if self.i >= self.n:
                return
            ch = self.peek()
            if ch == "-" and self.peek(1) == "-":
                self._skip_line_comment()
                continue
            if ch == "/" and self.peek(1) == "*":
                self._skip_block_comment()
                continue
            if self._try_skip_string():
                prev_keyword = None
                continue
            if ch == '"':
                self._skip_quoted_ident()
                prev_keyword = None
                continue
            if ch == ";":
                raise InvalidSqlError(
                    "세미콜론은 허용되지 않습니다 (단일 문장만)",
                    position=self.i,
                )
            if ch == "'":
                raise InvalidSqlError("문자 리터럴이 올바르지 않습니다", position=self.i)
            if _is_ident_start(ch):
                pos = self.i
                word = self._read_identifier()
                upper = word.upper()
                if upper in _FORBIDDEN_KEYWORDS:
                    raise InvalidSqlError(
                        f"차단된 키워드입니다: {upper}",
                        position=pos,
                    )
                if prev_keyword == "FOR" and upper == "UPDATE":
                    raise InvalidSqlError(
                        "FOR UPDATE는 허용되지 않습니다",
                        position=pos,
                    )
                prev_keyword = upper
                continue
            prev_keyword = None
            self.i += 1

    def _read_code_token_for_root(self) -> str | None:
        ch = self.peek()
        if ch == "":
            return None
        if ch == '"':
            self._skip_quoted_ident()
            return '"'
        if self._try_skip_string():
            return "'"
        if ch == ";":
            raise InvalidSqlError(
                "세미콜론은 허용되지 않습니다 (단일 문장만)",
                position=self.i,
            )
        if _is_ident_start(ch):
            return self._read_identifier().upper()
        return ch

    def _read_identifier(self) -> str:
        start = self.i
        self.i += 1
        while self.i < self.n and _IDENT_CHAR.match(self.sql[self.i]):
            self.i += 1
        return self.sql[start : self.i]

    def _skip_line_comment(self) -> None:
        self.i += 2
        while self.i < self.n and self.sql[self.i] not in "\r\n":
            self.i += 1

    def _skip_block_comment(self) -> None:
        pos = self.i
        self.i += 2
        while self.i < self.n:
            if self.peek() == "*" and self.peek(1) == "/":
                self.i += 2
                return
            self.i += 1
        raise InvalidSqlError("종료되지 않은 블록 주석입니다", position=pos)

    def _skip_quoted_ident(self) -> None:
        pos = self.i
        self.i += 1
        while self.i < self.n:
            if self.peek() == '"':
                if self.peek(1) == '"':
                    self.i += 2
                    continue
                self.i += 1
                return
            self.i += 1
        raise InvalidSqlError("종료되지 않은 인용 식별자입니다", position=pos)

    def _try_skip_string(self) -> bool:
        """Skip an Oracle string or q-quote if one starts at ``self.i``."""
        i = self.i
        s = self.sql
        n = self.n
        p = i
        if p < n and s[p] in "nN":
            p += 1
        qquote = False
        if p < n and s[p] in "qQ":
            qquote = True
            p += 1
        if p >= n or s[p] != "'":
            return False
        if qquote:
            self.i = p + 1
            self._skip_q_body(i)
            return True
        self.i = p + 1
        self._skip_simple_string(i)
        return True

    def _skip_simple_string(self, start: int) -> None:
        while self.i < self.n:
            ch = self.peek()
            if ch == "'":
                if self.peek(1) == "'":
                    self.i += 2
                    continue
                self.i += 1
                return
            self.i += 1
        raise InvalidSqlError("종료되지 않은 문자 리터럴입니다", position=start)

    def _skip_q_body(self, start: int) -> None:
        if self.i >= self.n:
            raise InvalidSqlError("종료되지 않은 q-quote 리터럴입니다", position=start)
        opener = self.sql[self.i]
        if opener in _WS or opener == "'":
            raise InvalidSqlError("올바르지 않은 q-quote 구분자입니다", position=start)
        closer = _Q_PAIR.get(opener, opener)
        self.i += 1
        while self.i < self.n:
            if self.peek() == closer and self.peek(1) == "'":
                self.i += 2
                return
            self.i += 1
        raise InvalidSqlError("종료되지 않은 q-quote 리터럴입니다", position=start)


def _is_ident_start(ch: str) -> bool:
    return len(ch) == 1 and ("A" <= ch <= "Z" or "a" <= ch <= "z")


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    upper: str
    pos: int


def tokenize_sql(sql: str, *, allow_comments: bool = True) -> list[Token]:
    """Tokenize Oracle SQL, skipping comments when allowed."""
    if not isinstance(sql, str):
        raise InvalidSqlError("SQL은 문자열이어야 합니다", position=0)
    scanner = _Scanner(sql)
    tokens: list[Token] = []
    while scanner.i < scanner.n:
        scanner.skip_whitespace()
        if scanner.i >= scanner.n:
            break
        ch = scanner.peek()
        pos = scanner.i
        if ch == "-" and scanner.peek(1) == "-":
            if not allow_comments:
                raise InvalidSqlError("주석은 허용되지 않습니다", position=pos)
            scanner._skip_line_comment()
            continue
        if ch == "/" and scanner.peek(1) == "*":
            if not allow_comments:
                raise InvalidSqlError("주석은 허용되지 않습니다", position=pos)
            scanner._skip_block_comment()
            continue
        if ch == ";":
            raise InvalidSqlError("세미콜론은 허용되지 않습니다 (단일 문장만)", position=pos)
        if ch == "@":
            raise InvalidSqlError("DB 링크는 허용되지 않습니다", position=pos)
        if ch == '"':
            start = scanner.i
            scanner._skip_quoted_ident()
            raw = sql[start:scanner.i]
            inner = raw[1:-1].replace('""', '"')
            if '"' in inner:
                raise InvalidSqlError("인용 식별자가 올바르지 않습니다", position=pos)
            try:
                ident = _require_unquoted(inner)
            except BadParamError as exc:
                raise InvalidSqlError(
                    "인용 식별자는 미인용 Oracle 이름과 같아야 합니다",
                    position=pos,
                ) from exc
            tokens.append(Token("IDENT", ident, ident, pos))
            continue
        if ch == ":":
            scanner.i += 1
            if scanner.peek() == "=":
                raise InvalidSqlError("할당 구문은 허용되지 않습니다", position=pos)
            if not _is_ident_start(scanner.peek()):
                raise InvalidSqlError(
                    "이름 있는 바인드만 허용됩니다",
                    position=pos,
                    hint=":name 형식의 바인드를 사용하세요",
                )
            name = scanner._read_identifier()
            if scanner.peek() == ":":
                raise InvalidSqlError("지시자 바인드는 허용되지 않습니다", position=pos)
            tokens.append(Token("BIND", name, name.upper(), pos))
            continue
        string_token = _read_string_token(scanner, sql)
        if string_token is not None:
            tokens.append(string_token)
            continue
        if _is_ident_start(ch):
            word = scanner._read_identifier()
            tokens.append(Token("IDENT", word, word.upper(), pos))
            continue
        if ch.isdigit() or (ch == "." and scanner.peek(1).isdigit()):
            tokens.append(_read_number(scanner, sql, pos))
            continue
        two = ch + scanner.peek(1)
        if two in {"<=", ">=", "<>", "!=", "||"}:
            scanner.i += 2
            tokens.append(Token("OP", two, two, pos))
            continue
        if ch in "=<>+-*/":
            scanner.i += 1
            tokens.append(Token("OP", ch, ch, pos))
            continue
        if ch in "(),.":
            scanner.i += 1
            tokens.append(Token("PUNCT", ch, ch, pos))
            continue
        raise InvalidSqlError(f"허용되지 않은 문자입니다: {ch!r}", position=pos)
    return tokens


def _read_string_token(scanner: _Scanner, sql: str) -> Token | None:
    pos = scanner.i
    i = scanner.i
    n = scanner.n
    p = i
    if p < n and sql[p] in "nN":
        p += 1
    qquote = False
    if p < n and sql[p] in "qQ":
        qquote = True
        p += 1
    if p >= n or sql[p] != "'":
        return None
    if qquote:
        scanner.i = p + 1
        scanner._skip_q_body(pos)
    else:
        scanner.i = p + 1
        scanner._skip_simple_string(pos)
    return Token("STRING", sql[pos:scanner.i], sql[pos:scanner.i].upper(), pos)


def _read_number(scanner: _Scanner, sql: str, pos: int) -> Token:
    start = scanner.i
    if scanner.peek() == ".":
        scanner.i += 1
    while scanner.i < scanner.n and scanner.sql[scanner.i].isdigit():
        scanner.i += 1
    if scanner.peek() == "." and scanner.peek(1).isdigit():
        scanner.i += 1
        while scanner.i < scanner.n and scanner.sql[scanner.i].isdigit():
            scanner.i += 1
    if scanner.peek() in "eE":
        raise InvalidSqlError("숫자 리터럴은 허용되지 않습니다", position=pos)
    text = sql[start:scanner.i]
    return Token("NUMBER", text, text, pos)


def _reject_reserved_bind_tokens(tokens: Sequence[Token]) -> None:
    for token in tokens:
        if token.kind == "BIND" and token.upper == ROW_LIMIT_BIND.upper():
            raise BadParamError(
                f"예약 바인드 이름은 사용할 수 없습니다: {ROW_LIMIT_BIND}",
                hint="다른 바인드 이름을 사용하세요",
            )


@dataclass
class _Relation:
    alias: str
    columns: frozenset[str]
    qualified: str | None
    kind: str


@dataclass
class _Scope:
    relations: list[_Relation] = field(default_factory=list)
    ctes: dict[str, frozenset[str]] = field(default_factory=dict)

    def add(self, rel: _Relation) -> None:
        self.relations.append(rel)

    def by_alias(self, alias: str) -> _Relation | None:
        key = alias.upper()
        found = [item for item in self.relations if item.alias == key]
        if len(found) > 1:
            raise InvalidSqlError(f"별칭이 모호합니다: {alias}")
        return found[0] if found else None


class _TokenCursor:
    def __init__(self, tokens: Sequence[Token]) -> None:
        self.tokens = tokens
        self.i = 0

    def peek(self, k: int = 0) -> Token | None:
        j = self.i + k
        if 0 <= j < len(self.tokens):
            return self.tokens[j]
        return None

    def at_end(self) -> bool:
        return self.i >= len(self.tokens)

    def pos(self) -> int:
        token = self.peek()
        return 0 if token is None else token.pos

    def kind(self) -> str | None:
        token = self.peek()
        return None if token is None else token.kind

    def upper(self) -> str | None:
        token = self.peek()
        return None if token is None else token.upper

    def is_ident(self, *names: str) -> bool:
        token = self.peek()
        if token is None or token.kind != "IDENT":
            return False
        if not names:
            return True
        return token.upper in names

    def is_punct(self, value: str) -> bool:
        token = self.peek()
        return token is not None and token.kind == "PUNCT" and token.value == value

    def is_op(self, *values: str) -> bool:
        token = self.peek()
        if token is None or token.kind != "OP":
            return False
        if not values:
            return True
        return token.value in values

    def bump(self) -> Token:
        if self.at_end():
            raise InvalidSqlError("SQL이 예기치 않게 끝났습니다", position=self.pos())
        token = self.tokens[self.i]
        self.i += 1
        return token

    def expect_ident(self, *names: str) -> Token:
        if not self.is_ident(*names):
            found = self.upper() or "EOF"
            raise InvalidSqlError(f"예상하지 않은 토큰입니다: {found}", position=self.pos())
        return self.bump()

    def expect_punct(self, value: str) -> Token:
        if not self.is_punct(value):
            found = self.upper() or "EOF"
            raise InvalidSqlError(f"'{value}'가 필요합니다 (토큰: {found})", position=self.pos())
        return self.bump()


class _SelectParser:
    def __init__(self, tokens: Sequence[Token], catalog: Any, connection: str) -> None:
        self.c = _TokenCursor(tokens)
        self.catalog = catalog
        self.connection = connection
        self._core_scope: _Scope | None = None  # 마지막 select core의 FROM 스코프

    def parse_statement(self) -> list[str]:
        """Parse one full statement and return its output column names."""
        outputs = self._parse_query()
        if not self.c.at_end():
            raise InvalidSqlError(
                f"문장 뒤에 추가 토큰이 있습니다: {self.c.upper()}",
                position=self.c.pos(),
            )
        return outputs

    def _parse_query(self) -> list[str]:
        ctes = dict(self._outer_ctes())
        if self.c.is_ident("WITH"):
            self.c.bump()
            if self.c.is_ident("RECURSIVE"):
                raise InvalidSqlError("RECURSIVE CTE는 허용되지 않습니다", position=self.c.pos())
            while True:
                name = self._ident_name()
                if name in ctes:
                    raise InvalidSqlError(f"CTE 이름이 중복됩니다: {name}", position=self.c.pos())
                declared: list[str] = []
                if self.c.is_punct("("):
                    self.c.bump()
                    while True:
                        declared.append(self._ident_name())
                        if self.c.is_punct(","):
                            self.c.bump()
                            continue
                        break
                    self.c.expect_punct(")")
                self.c.expect_ident("AS")
                self.c.expect_punct("(")
                outputs, _consumed = self._parse_paren_query(ctes)
                if declared:
                    ctes[name] = frozenset(declared)
                else:
                    ctes[name] = frozenset(item.upper() for item in outputs if item)
                if self.c.is_punct(","):
                    self.c.bump()
                    continue
                break
        return self._parse_select_block(ctes)

    def _outer_ctes(self) -> dict[str, frozenset[str]]:
        env = getattr(self, "_cte_env", None)
        return {} if not env else dict(env)

    def _parse_paren_query(self, ctes: dict[str, frozenset[str]]) -> tuple[list[str], int]:
        depth = 1
        start = self.c.i
        i = start
        tokens = self.c.tokens
        while i < len(tokens):
            token = tokens[i]
            if token.kind == "PUNCT" and token.value == "(":
                depth += 1
            elif token.kind == "PUNCT" and token.value == ")":
                depth -= 1
                if depth == 0:
                    inner = _SelectParser(tokens[start:i], self.catalog, self.connection)
                    inner._cte_env = ctes
                    outputs = inner.parse_statement()
                    self.c.i = i + 1
                    return outputs, i + 1
            i += 1
        raise InvalidSqlError("종료되지 않은 괄호입니다", position=self.c.pos())

    def _parse_select_block(self, ctes: dict[str, frozenset[str]]) -> list[str]:
        parts: list[list[str]] = []
        while True:
            parts.append(self._parse_select_core(ctes))
            if self.c.is_ident("UNION"):
                self.c.bump()
                if self.c.is_ident("ALL"):
                    self.c.bump()
                continue
            if self.c.is_ident("INTERSECT", "MINUS"):
                self.c.bump()
                continue
            break
        if self.c.is_ident("ORDER"):
            self._parse_order_by(self._order_by_scope(parts, ctes), allow_numbers=True)
        self._parse_fetch_offset()
        if self.c.is_ident("FOR"):
            raise InvalidSqlError("FOR UPDATE는 허용되지 않습니다", position=self.c.pos())
        return parts[0]

    def _order_by_scope(
        self, parts: list[list[str]], ctes: dict[str, frozenset[str]]
    ) -> _Scope:
        """Scope for a top-level ORDER BY.

        단일 SELECT면 FROM 관계의 모든 컬럼과 출력 별칭을 함께 본다 (SQL 규칙).
        집합연산 뒤에는 출력 컬럼만 참조할 수 있으므로 출력 스코프만 준다.
        """
        if len(parts) != 1 or self._core_scope is None:
            return self._scope_from_outputs(parts[0], ctes)
        scope = _Scope(relations=list(self._core_scope.relations), ctes=ctes)
        # FROM 관계에서 이미 해석되는 이름을 출력 스코프에도 넣으면 "컬럼이 모호합니다"가
        # 된다. 출력 스코프는 FROM에 없는 진짜 별칭(SELECT ... AS X)만 담는다.
        from_columns = {
            column.upper() for rel in self._core_scope.relations for column in rel.columns
        }
        aliases = frozenset(
            item.upper() for item in parts[0] if item and item.upper() not in from_columns
        )
        if aliases:
            scope.add(_Relation(alias="*", columns=aliases, qualified=None, kind="output"))
        return scope

    def _scope_from_outputs(self, outputs: list[str], ctes: dict[str, frozenset[str]]) -> _Scope:
        scope = _Scope(ctes=ctes)
        scope.add(_Relation(alias="*", columns=frozenset(item.upper() for item in outputs if item), qualified=None, kind="output"))
        return scope

    def _parse_select_core(self, ctes: dict[str, frozenset[str]]) -> list[str]:
        self.c.expect_ident("SELECT")
        if self.c.is_ident("DISTINCT", "ALL", "UNIQUE"):
            self.c.bump()
        select_items = self._parse_select_list_raw()
        if self.c.is_ident("INTO"):
            raise InvalidSqlError("SELECT INTO는 허용되지 않습니다", position=self.c.pos())
        self.c.expect_ident("FROM")
        scope = _Scope(ctes=ctes)
        self._parse_from_clause(scope)
        self._core_scope = scope
        if self.c.is_ident("WHERE"):
            self.c.bump()
            self._parse_or_expr(scope)
        if self.c.is_ident("CONNECT") or self.c.is_ident("START"):
            raise InvalidSqlError("계층 쿼리는 허용되지 않습니다", position=self.c.pos())
        if self.c.is_ident("GROUP"):
            self.c.bump()
            self.c.expect_ident("BY")
            self._parse_group_by(scope)
        if self.c.is_ident("HAVING"):
            self.c.bump()
            self._parse_or_expr(scope)
        outputs: list[str] = []
        for item in select_items:
            outputs.extend(self._authorize_select_item(item, scope))
        return outputs

    def _parse_select_list_raw(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            start = self.c.i
            if self.c.is_op("*"):
                self.c.bump()
                items.append({"start": start, "end": self.c.i, "alias": None})
            elif (
                self.c.is_ident()
                and self.c.peek(1) is not None
                and self.c.peek(1).kind == "PUNCT"
                and self.c.peek(1).value == "."
                and self.c.peek(2) is not None
                and self.c.peek(2).kind == "OP"
                and self.c.peek(2).value == "*"
            ):
                self._ident_name()
                self.c.expect_punct(".")
                if not self.c.is_op("*"):
                    raise InvalidSqlError("별칭.* 형태가 올바르지 않습니다", position=self.c.pos())
                self.c.bump()
                items.append({"start": start, "end": self.c.i, "alias": None})
            else:
                self._parse_or_expr(_Scope(), authorize=False)
                expr_end = self.c.i  # 별칭 토큰은 표현식에서 제외한다
                alias: str | None = None
                if self.c.is_ident("AS"):
                    self.c.bump()
                    alias = self._ident_name()
                elif self.c.is_ident() and (self.c.upper() or "") not in (
                    _PRED_STOP | _CLAUSE_STOP | _JOIN_INTRO | {"FROM", "INTO"}
                ):
                    alias = self._ident_name()
                items.append({"start": start, "end": expr_end, "alias": alias})
            if self.c.is_punct(","):
                self.c.bump()
                continue
            break
        if not items:
            raise InvalidSqlError("SELECT 목록이 비어 있습니다", position=self.c.pos())
        return items

    def _authorize_select_item(self, item: dict[str, Any], scope: _Scope) -> list[str]:
        inner = _SelectParser(self.c.tokens[item["start"] : item["end"]], self.catalog, self.connection)
        inner._cte_env = scope.ctes
        outputs = inner._parse_select_expr(scope)
        alias = item["alias"]
        if alias:
            return [alias]
        return outputs

    def _parse_select_expr(self, scope: _Scope) -> list[str]:
        if self.c.is_op("*"):
            self.c.bump()
            names: list[str] = []
            for rel in scope.relations:
                names.extend(list(rel.columns))
            if not names:
                raise InvalidSqlError("SELECT * 를 검증할 수 없습니다", position=self.c.pos())
            if not self.c.at_end():
                raise InvalidSqlError("SELECT * 형태가 올바르지 않습니다", position=self.c.pos())
            return names
        if (
            self.c.is_ident()
            and self.c.peek(1) is not None
            and self.c.peek(1).kind == "PUNCT"
            and self.c.peek(1).value == "."
            and self.c.peek(2) is not None
            and self.c.peek(2).kind == "OP"
            and self.c.peek(2).value == "*"
        ):
            alias = self._ident_name()
            self.c.expect_punct(".")
            if not self.c.is_op("*"):
                raise InvalidSqlError("별칭.* 형태가 올바르지 않습니다", position=self.c.pos())
            self.c.bump()
            rel = scope.by_alias(alias)
            if rel is None:
                raise UnknownObjectError(f"카탈로그에 없는 식별자: {alias}")
            if not self.c.at_end():
                raise InvalidSqlError("SELECT 목록이 모호합니다", position=self.c.pos())
            return list(rel.columns)
        saved = self.c.i
        if self.c.is_ident():
            parts = [self._ident_name()]
            while self.c.is_punct("."):
                nxt = self.c.peek(1)
                if nxt is not None and nxt.kind == "OP" and nxt.value == "*":
                    break
                self.c.bump()
                parts.append(self._ident_name())
            if self.c.at_end():
                self.c.i = saved
                self._parse_or_expr(scope, authorize=True)
                return [parts[-1]]
            self.c.i = saved
        self._parse_or_expr(scope, authorize=True)
        if not self.c.at_end():
            raise InvalidSqlError("SELECT 표현식이 모호합니다", position=self.c.pos())
        return []

    def _parse_from_clause(self, scope: _Scope) -> None:
        self._parse_from_item(scope)
        while True:
            if self.c.is_punct(","):
                self.c.bump()
                self._parse_from_item(scope)
                continue
            if self._at_join():
                self._parse_join(scope)
                continue
            break

    def _at_join(self) -> bool:
        if self.c.is_ident("JOIN"):
            return True
        if self.c.is_ident("INNER", "CROSS") and self._ahead_is("JOIN"):
            return True
        if self.c.is_ident("LEFT", "RIGHT", "FULL"):
            return True
        if self.c.is_ident("NATURAL"):
            return True
        return False

    def _ahead_is(self, *names: str) -> bool:
        token = self.c.peek(1)
        return token is not None and token.kind == "IDENT" and token.upper in names

    def _parse_join(self, scope: _Scope) -> None:
        natural = False
        if self.c.is_ident("NATURAL"):
            natural = True
            self.c.bump()
        if self.c.is_ident("INNER"):
            self.c.bump()
        elif self.c.is_ident("CROSS"):
            self.c.bump()
            self.c.expect_ident("JOIN")
            self._parse_from_item(scope)
            return
        elif self.c.is_ident("LEFT", "RIGHT", "FULL"):
            self.c.bump()
            if self.c.is_ident("OUTER"):
                self.c.bump()
        self.c.expect_ident("JOIN")
        self._parse_from_item(scope)
        if natural:
            return
        if self.c.is_ident("ON"):
            self.c.bump()
            self._parse_or_expr(scope)
            return
        if self.c.is_ident("USING"):
            self.c.bump()
            self.c.expect_punct("(")
            while True:
                self._resolve_column((self._ident_name(),), scope)
                if self.c.is_punct(","):
                    self.c.bump()
                    continue
                break
            self.c.expect_punct(")")
            return
        raise InvalidSqlError("JOIN은 ON 또는 USING이 필요합니다", position=self.c.pos())

    def _parse_from_item(self, scope: _Scope) -> None:
        if self.c.is_punct("("):
            self.c.bump()
            if self.c.is_ident("SELECT", "WITH"):
                outputs, _ = self._parse_paren_query_from_open(scope.ctes)
                alias = self._optional_alias()
                if alias is None:
                    raise InvalidSqlError("인라인 뷰는 별칭이 필요합니다", position=self.c.pos())
                scope.add(_Relation(alias=alias, columns=frozenset(o.upper() for o in outputs if o), qualified=None, kind="subquery"))
                return
            nested = _Scope(ctes=scope.ctes)
            self._parse_from_clause(nested)
            self.c.expect_punct(")")
            alias = self._optional_alias()
            if alias is None:
                for rel in nested.relations:
                    scope.add(rel)
            else:
                cols: set[str] = set()
                for rel in nested.relations:
                    cols.update(rel.columns)
                scope.add(_Relation(alias=alias, columns=frozenset(cols), qualified=None, kind="subquery"))
            return
        if self.c.is_ident("TABLE", "THE", "XMLTABLE", "JSON_TABLE"):
            raise InvalidSqlError("테이블 함수는 허용되지 않습니다", position=self.c.pos())
        parts = self._qualified_name()
        if self.c.is_punct("("):
            raise InvalidSqlError("테이블 함수는 허용되지 않습니다", position=self.c.pos())
        if self.c.is_ident("PARTITION", "SUBPARTITION", "SAMPLE", "AS"):
            if self.c.is_ident("AS"):
                pass
            else:
                raise InvalidSqlError("파티션/SAMPLE 지정은 허용되지 않습니다", position=self.c.pos())
        alias = self._optional_alias()
        rel = self._resolve_from_name(parts, alias, scope)
        scope.add(rel)

    def _parse_paren_query_from_open(self, ctes: dict[str, frozenset[str]]) -> tuple[list[str], int]:
        # opening '(' already consumed
        depth = 1
        start = self.c.i
        i = start
        tokens = self.c.tokens
        while i < len(tokens):
            token = tokens[i]
            if token.kind == "PUNCT" and token.value == "(":
                depth += 1
            elif token.kind == "PUNCT" and token.value == ")":
                depth -= 1
                if depth == 0:
                    inner = _SelectParser(tokens[start:i], self.catalog, self.connection)
                    inner._cte_env = ctes
                    outputs = inner.parse_statement()
                    self.c.i = i + 1
                    return outputs, i + 1
            i += 1
        raise InvalidSqlError("종료되지 않은 괄호입니다", position=self.c.pos())

    def _resolve_from_name(self, parts: tuple[str, ...], alias: str | None, scope: _Scope) -> _Relation:
        if len(parts) > 2:
            raise InvalidSqlError("FROM 식별자가 모호합니다", position=self.c.pos())
        name = parts[0] if len(parts) == 1 else ".".join(parts)
        if len(parts) == 1 and parts[0] in scope.ctes:
            cols = scope.ctes[parts[0]]
            return _Relation(alias=alias or parts[0], columns=cols, qualified=None, kind="cte")
        obj = self._require_object(name)
        columns = frozenset(_column_names(obj))
        rel_alias = alias or obj.name
        return _Relation(alias=rel_alias, columns=columns, qualified=obj.qualified, kind="table")

    def _require_object(self, name: str) -> Any:
        fn = getattr(self.catalog, "require_object", None)
        if not callable(fn):
            raise UnknownObjectError(f"카탈로그에 없는 식별자: {name}")
        obj = fn(self.connection, name)
        db_link = getattr(obj, "db_link", None)
        if db_link:
            raise InvalidSqlError("DB 링크는 허용되지 않습니다")
        return obj

    def _optional_alias(self) -> str | None:
        if self.c.is_ident("AS"):
            self.c.bump()
            return self._ident_name()
        if not self.c.is_ident():
            return None
        word = self.c.upper() or ""
        if word in _CLAUSE_STOP or word in _JOIN_INTRO or word in {"ON", "USING", "WHERE", "GROUP", "HAVING", "SET"}:
            return None
        return self._ident_name()

    def _parse_group_by(self, scope: _Scope) -> None:
        while True:
            if self.c.is_ident("ROLLUP", "CUBE", "GROUPING"):
                raise InvalidSqlError("GROUPING SETS/ROLLUP/CUBE는 허용되지 않습니다", position=self.c.pos())
            self._parse_or_expr(scope)
            if self.c.is_punct(","):
                self.c.bump()
                continue
            break

    def _parse_order_by(self, scope: _Scope, *, allow_numbers: bool) -> None:
        self.c.expect_ident("ORDER")
        self.c.expect_ident("BY")
        while True:
            if allow_numbers and self.c.kind() == "NUMBER":
                self.c.bump()
            else:
                self._parse_or_expr(scope)
            if self.c.is_ident("ASC", "DESC"):
                self.c.bump()
            if self.c.is_ident("NULLS"):
                self.c.bump()
                self.c.expect_ident("FIRST", "LAST")
            if self.c.is_punct(","):
                self.c.bump()
                continue
            break

    def _parse_fetch_offset(self) -> None:
        if self.c.is_ident("OFFSET"):
            self.c.bump()
            self._limit_value()
            if self.c.is_ident("ROW", "ROWS"):
                self.c.bump()
        if self.c.is_ident("FETCH"):
            self.c.bump()
            self.c.expect_ident("FIRST", "NEXT")
            self._limit_value()
            if self.c.is_ident("PERCENT"):
                raise InvalidSqlError("FETCH PERCENT는 허용되지 않습니다", position=self.c.pos())
            self.c.expect_ident("ROW", "ROWS")
            if self.c.is_ident("ONLY"):
                self.c.bump()
            elif self.c.is_ident("WITH"):
                raise InvalidSqlError("FETCH WITH TIES는 허용되지 않습니다", position=self.c.pos())
            else:
                raise InvalidSqlError("FETCH FIRST n ROWS ONLY만 허용됩니다", position=self.c.pos())

    def _limit_value(self) -> None:
        if self.c.kind() == "BIND":
            self.c.bump()
            return
        if self.c.kind() == "NUMBER":
            self.c.bump()
            return
        raise InvalidSqlError("FETCH/OFFSET 값은 바인드 또는 정수여야 합니다", position=self.c.pos())

    def _parse_or_expr(self, scope: _Scope, *, authorize: bool = True) -> None:
        self._parse_and_expr(scope, authorize=authorize)
        while self.c.is_ident("OR"):
            self.c.bump()
            self._parse_and_expr(scope, authorize=authorize)

    def _parse_and_expr(self, scope: _Scope, *, authorize: bool = True) -> None:
        self._parse_not_expr(scope, authorize=authorize)
        while self.c.is_ident("AND"):
            self.c.bump()
            self._parse_not_expr(scope, authorize=authorize)

    def _parse_not_expr(self, scope: _Scope, *, authorize: bool = True) -> None:
        while self.c.is_ident("NOT"):
            self.c.bump()
        self._parse_pred(scope, authorize=authorize)

    def _parse_pred(self, scope: _Scope, *, authorize: bool = True) -> None:
        if self.c.is_ident("EXISTS"):
            self.c.bump()
            self._parse_subquery(scope, authorize=authorize)
            return
        self._parse_concat(scope, authorize=authorize)
        if self.c.is_op() and self.c.peek() and self.c.peek().value in _COMPARE_OPS:
            self.c.bump()
            if self.c.is_ident("ANY", "SOME", "ALL"):
                self.c.bump()
                self._parse_subquery(scope, authorize=authorize)
                return
            self._parse_concat(scope, authorize=authorize)
            self._optional_outer_join_mark()
            return
        if self.c.is_ident("IS"):
            self.c.bump()
            if self.c.is_ident("NOT"):
                self.c.bump()
            if self.c.is_ident("NULL"):
                self.c.bump()
                return
            raise InvalidSqlError("IS NULL만 허용됩니다", position=self.c.pos())
        if self.c.is_ident("NOT"):
            self.c.bump()
        if self.c.is_ident("IN"):
            self.c.bump()
            self._parse_in_list(scope, authorize=authorize)
            return
        if self.c.is_ident("BETWEEN"):
            self.c.bump()
            self._parse_concat(scope, authorize=authorize)
            self.c.expect_ident("AND")
            self._parse_concat(scope, authorize=authorize)
            return
        if self.c.is_ident("LIKE", "LIKEC", "LIKE2", "LIKE4"):
            self.c.bump()
            self._parse_concat(scope, authorize=authorize)
            if self.c.is_ident("ESCAPE"):
                raise InvalidSqlError("LIKE ESCAPE는 허용되지 않습니다", position=self.c.pos())
            return

    def _optional_outer_join_mark(self) -> None:
        if (
            self.c.is_punct("(")
            and self.c.peek(1) is not None
            and self.c.peek(1).kind == "OP"
            and self.c.peek(1).value == "+"
            and self.c.peek(2) is not None
            and self.c.peek(2).kind == "PUNCT"
            and self.c.peek(2).value == ")"
        ):
            self.c.bump()
            self.c.bump()
            self.c.bump()

    def _parse_in_list(self, scope: _Scope, *, authorize: bool) -> None:
        self.c.expect_punct("(")
        if self.c.is_ident("SELECT", "WITH"):
            outputs, _ = self._parse_paren_query_from_open(scope.ctes)
            del outputs
            return
        if self.c.at_end():
            raise InvalidSqlError("IN 목록이 비어 있습니다", position=self.c.pos())
        self._parse_or_expr(scope, authorize=authorize)
        while self.c.is_punct(","):
            self.c.bump()
            self._parse_or_expr(scope, authorize=authorize)
        self.c.expect_punct(")")

    def _parse_concat(self, scope: _Scope, *, authorize: bool) -> None:
        self._parse_add(scope, authorize=authorize)
        while self.c.is_op("||"):
            self.c.bump()
            self._parse_add(scope, authorize=authorize)

    def _parse_add(self, scope: _Scope, *, authorize: bool) -> None:
        self._parse_mul(scope, authorize=authorize)
        while self.c.is_op("+", "-"):
            self.c.bump()
            self._parse_mul(scope, authorize=authorize)

    def _parse_mul(self, scope: _Scope, *, authorize: bool) -> None:
        self._parse_unary(scope, authorize=authorize)
        while self.c.is_op("*", "/"):
            self.c.bump()
            self._parse_unary(scope, authorize=authorize)

    def _parse_unary(self, scope: _Scope, *, authorize: bool) -> None:
        while self.c.is_op("+", "-"):
            self.c.bump()
        self._parse_atom(scope, authorize=authorize)

    def _parse_atom(self, scope: _Scope, *, authorize: bool) -> None:
        if self.c.kind() == "BIND":
            self.c.bump()
            self._optional_outer_join_mark()
            return
        if self.c.kind() == "NUMBER":
            raise InvalidSqlError(
                "값은 바인드 변수만 허용됩니다",
                position=self.c.pos(),
                hint="숫자 리터럴 대신 :name 바인드를 사용하세요",
            )
        if self.c.kind() == "STRING":
            raise InvalidSqlError(
                "값은 바인드 변수만 허용됩니다",
                position=self.c.pos(),
                hint="문자열 리터럴 대신 :name 바인드를 사용하세요",
            )
        if self.c.is_punct("("):
            self.c.bump()
            if self.c.is_ident("SELECT", "WITH"):
                self._parse_paren_query_from_open(scope.ctes)
                return
            self._parse_or_expr(scope, authorize=authorize)
            self.c.expect_punct(")")
            return
        if self.c.is_ident("CASE"):
            self._parse_case(scope, authorize=authorize)
            return
        if self.c.is_ident("CAST"):
            self._parse_cast(scope, authorize=authorize)
            return
        if self.c.is_ident("EXTRACT"):
            self._parse_extract(scope, authorize=authorize)
            return
        if self.c.is_ident("TRIM"):
            self._parse_trim(scope, authorize=authorize)
            return
        if self.c.is_ident("NULL"):
            self.c.bump()
            return
        if self.c.is_ident() and (self.c.upper() or "") in _KEYWORD_ATOMS:
            self.c.bump()
            return
        if self.c.is_op("*"):
            self.c.bump()
            return
        if self.c.is_ident():
            self._parse_name_or_call(scope, authorize=authorize)
            return
        raise InvalidSqlError(
            f"표현식을 해석할 수 없습니다: {self.c.upper() or 'EOF'}",
            position=self.c.pos(),
        )

    def _parse_name_or_call(self, scope: _Scope, *, authorize: bool) -> None:
        parts = [self._ident_name()]
        while self.c.is_punct("."):
            nxt = self.c.peek(1)
            if nxt is not None and nxt.kind == "OP" and nxt.value == "*":
                break
            self.c.bump()
            parts.append(self._ident_name())
        if self.c.is_punct("("):
            self._parse_call(parts, scope, authorize=authorize)
            return
        if authorize:
            if len(parts) == 1 and parts[0] in _PSEUDOCOLUMNS:
                self._optional_outer_join_mark()
                return
            if (
                len(parts) >= 2
                and scope.by_alias(parts[0]) is None
                and not any(
                    item.qualified == ".".join(parts[:2])
                    or item.alias == parts[0]
                    or (item.qualified and item.qualified.endswith("." + parts[0]))
                    for item in scope.relations
                )
            ):
                raise UnknownObjectError(
                    f"카탈로그에 없는 식별자: {'.'.join(parts)}",
                    hint="테이블 별칭·컬럼명을 확인하세요. 패키지/스키마 함수 호출은 허용되지 않습니다",
                )
            self._resolve_column(tuple(parts), scope)
        self._optional_outer_join_mark()

    def _parse_call(self, parts: list[str], scope: _Scope, *, authorize: bool) -> None:
        if len(parts) != 1:
            raise InvalidSqlError(
                "패키지/스키마 함수 호출은 허용되지 않습니다",
                position=self.c.pos(),
            )
        name = parts[0]
        if authorize and name not in _READONLY_CALLS:
            raise InvalidSqlError(
                f"허용되지 않은 함수입니다: {name}",
                position=self.c.pos(),
                hint="카탈로그 객체와 읽기 전용 내장 함수만 사용할 수 있습니다",
            )
        self.c.expect_punct("(")
        args: list[int] = []
        if not self.c.is_punct(")"):
            while True:
                args.append(self.c.i)
                if name in _FORMAT_LITERAL_FUNCS and len(args) == 2 and self.c.kind() == "STRING":
                    self.c.bump()
                elif self.c.is_op("*"):
                    self.c.bump()
                elif self.c.is_ident("DISTINCT", "ALL") and name == "COUNT":
                    self.c.bump()
                    if self.c.is_op("*"):
                        self.c.bump()
                    else:
                        self._parse_or_expr(scope, authorize=authorize)
                else:
                    self._parse_or_expr(scope, authorize=authorize)
                if self.c.is_punct(","):
                    self.c.bump()
                    continue
                break
        self.c.expect_punct(")")
        if self.c.is_ident("WITHIN"):
            self.c.bump()
            self.c.expect_ident("GROUP")
            self.c.expect_punct("(")
            self._parse_order_by(scope, allow_numbers=False)
            self.c.expect_punct(")")
        if self.c.is_ident("OVER"):
            self._parse_over(scope, authorize=authorize)
        if self.c.is_ident("KEEP"):
            raise InvalidSqlError("KEEP 집계는 허용되지 않습니다", position=self.c.pos())

    def _parse_over(self, scope: _Scope, *, authorize: bool) -> None:
        self.c.expect_ident("OVER")
        self.c.expect_punct("(")
        if self.c.is_ident("PARTITION"):
            self.c.bump()
            self.c.expect_ident("BY")
            while True:
                self._parse_or_expr(scope, authorize=authorize)
                if self.c.is_punct(","):
                    self.c.bump()
                    continue
                break
        if self.c.is_ident("ORDER"):
            self._parse_order_by(scope, allow_numbers=False)
        if self.c.is_ident("ROWS", "RANGE"):
            self.c.bump()
            self._parse_window_frame()
        self.c.expect_punct(")")

    def _parse_window_frame(self) -> None:
        if self.c.is_ident("BETWEEN"):
            self.c.bump()
            self._parse_frame_bound()
            self.c.expect_ident("AND")
            self._parse_frame_bound()
            return
        self._parse_frame_bound()

    def _parse_frame_bound(self) -> None:
        if self.c.is_ident("UNBOUNDED"):
            self.c.bump()
            self.c.expect_ident("PRECEDING", "FOLLOWING")
            return
        if self.c.is_ident("CURRENT"):
            self.c.bump()
            self.c.expect_ident("ROW")
            return
        if self.c.kind() == "NUMBER":
            self.c.bump()
            self.c.expect_ident("PRECEDING", "FOLLOWING")
            return
        raise InvalidSqlError("윈도우 프레임이 올바르지 않습니다", position=self.c.pos())

    def _parse_case(self, scope: _Scope, *, authorize: bool) -> None:
        self.c.expect_ident("CASE")
        simple = not self.c.is_ident("WHEN")
        if simple:
            self._parse_or_expr(scope, authorize=authorize)
        saw_when = False
        while self.c.is_ident("WHEN"):
            saw_when = True
            self.c.bump()
            self._parse_or_expr(scope, authorize=authorize)
            self.c.expect_ident("THEN")
            self._parse_or_expr(scope, authorize=authorize)
        if not saw_when:
            raise InvalidSqlError("CASE에 WHEN이 필요합니다", position=self.c.pos())
        if self.c.is_ident("ELSE"):
            self.c.bump()
            self._parse_or_expr(scope, authorize=authorize)
        self.c.expect_ident("END")

    def _parse_cast(self, scope: _Scope, *, authorize: bool) -> None:
        self.c.expect_ident("CAST")
        self.c.expect_punct("(")
        self._parse_or_expr(scope, authorize=authorize)
        self.c.expect_ident("AS")
        self._parse_data_type()
        self.c.expect_punct(")")

    def _parse_data_type(self) -> None:
        if not self.c.is_ident():
            raise InvalidSqlError("데이터 타입이 필요합니다", position=self.c.pos())
        while self.c.is_ident() and (self.c.upper() or "") in _TYPE_WORDS:
            self.c.bump()
            if self.c.is_punct("("):
                self.c.bump()
                if self.c.kind() != "NUMBER":
                    raise InvalidSqlError("타입 길이는 숫자여야 합니다", position=self.c.pos())
                self.c.bump()
                if self.c.is_punct(","):
                    self.c.bump()
                    if self.c.kind() != "NUMBER":
                        raise InvalidSqlError("타입 길이는 숫자여야 합니다", position=self.c.pos())
                    self.c.bump()
                if self.c.is_ident("CHAR", "BYTE"):
                    self.c.bump()
                self.c.expect_punct(")")
        if self.c.is_ident("WITH"):
            self.c.bump()
            if self.c.is_ident("LOCAL"):
                self.c.bump()
            self.c.expect_ident("TIME")
            self.c.expect_ident("ZONE")

    def _parse_extract(self, scope: _Scope, *, authorize: bool) -> None:
        self.c.expect_ident("EXTRACT")
        self.c.expect_punct("(")
        self.c.expect_ident("YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "TIMEZONE_HOUR", "TIMEZONE_MINUTE", "TIMEZONE_REGION", "TIMEZONE_ABBR")
        self.c.expect_ident("FROM")
        self._parse_or_expr(scope, authorize=authorize)
        self.c.expect_punct(")")

    def _parse_trim(self, scope: _Scope, *, authorize: bool) -> None:
        self.c.expect_ident("TRIM")
        self.c.expect_punct("(")
        if self.c.is_ident("LEADING", "TRAILING", "BOTH"):
            self.c.bump()
            if self.c.kind() == "STRING":
                raise InvalidSqlError("값은 바인드 변수만 허용됩니다", position=self.c.pos())
            if self.c.is_ident("FROM"):
                self.c.bump()
        self._parse_or_expr(scope, authorize=authorize)
        if self.c.is_ident("FROM"):
            self.c.bump()
            self._parse_or_expr(scope, authorize=authorize)
        self.c.expect_punct(")")

    def _parse_subquery(self, scope: _Scope, *, authorize: bool) -> None:
        self.c.expect_punct("(")
        self._parse_paren_query_from_open(scope.ctes)

    def _resolve_column(self, parts: tuple[str, ...], scope: _Scope) -> None:
        if not parts or len(parts) > 3:
            raise InvalidSqlError("컬럼 참조가 모호합니다", position=self.c.pos())
        if len(parts) == 1:
            name = parts[0]
            if name in _PSEUDOCOLUMNS:
                return
            matches = [rel for rel in scope.relations if name in rel.columns]
            if len(matches) == 1:
                return
            if len(matches) > 1:
                raise InvalidSqlError(f"컬럼이 모호합니다: {name}")
            if any(rel.kind != "table" and not rel.columns for rel in scope.relations):
                raise InvalidSqlError(f"컬럼을 검증할 수 없습니다: {name}")
            raise UnknownObjectError(f"카탈로그에 없는 식별자: {name}")
        if len(parts) == 2:
            alias, col = parts
            rel = scope.by_alias(alias)
            if rel is None:
                for item in scope.relations:
                    if item.qualified and item.qualified.endswith(f".{alias}"):
                        rel = item
                        break
            if rel is None:
                raise UnknownObjectError(f"카탈로그에 없는 식별자: {alias}.{col}")
            if col not in rel.columns:
                raise UnknownObjectError(f"카탈로그에 없는 식별자: {alias}.{col}")
            return
        owner, table, col = parts
        key = f"{owner}.{table}"
        rel = None
        for item in scope.relations:
            if item.qualified == key or item.alias == table:
                rel = item
                break
        if rel is None:
            raise UnknownObjectError(f"카탈로그에 없는 식별자: {key}.{col}")
        if col not in rel.columns:
            raise UnknownObjectError(f"카탈로그에 없는 식별자: {key}.{col}")

    def _ident_name(self) -> str:
        token = self.c.peek()
        if token is None or token.kind != "IDENT":
            raise InvalidSqlError("식별자가 필요합니다", position=self.c.pos())
        if token.upper in _FORBIDDEN_KEYWORDS:
            raise InvalidSqlError(f"차단된 키워드입니다: {token.upper}", position=token.pos)
        self.c.bump()
        return token.upper

    def _qualified_name(self) -> tuple[str, ...]:
        parts = [self._ident_name()]
        while self.c.is_punct("."):
            self.c.bump()
            parts.append(self._ident_name())
        return tuple(parts)


def _column_names(obj: Any) -> list[str]:
    columns = getattr(obj, "columns", ())
    names: list[str] = []
    for col in columns:
        name = getattr(col, "name", None)
        if isinstance(name, str) and name:
            names.append(name.upper())
    return names


class _WhereParser:
    def __init__(
        self,
        tokens: Sequence[Token],
        *,
        allowed_columns: AbstractSet[str],
        table_name: str | None,
        owner: str | None,
    ) -> None:
        self.c = _TokenCursor(tokens)
        self.allowed_columns = allowed_columns
        self.table_name = table_name
        self.owner = owner
        self.bind_names: list[str] = []

    def parse(self) -> str:
        text = self._parse_or()
        if not self.c.at_end():
            raise InvalidSqlError(
                f"where 술어 뒤에 추가 토큰이 있습니다: {self.c.upper()}",
                position=self.c.pos(),
            )
        return text

    def _parse_or(self) -> str:
        parts = [self._parse_and()]
        while self.c.is_ident("OR"):
            self.c.bump()
            parts.append(self._parse_and())
        return " OR ".join(parts)

    def _parse_and(self) -> str:
        parts = [self._parse_not()]
        while self.c.is_ident("AND"):
            self.c.bump()
            parts.append(self._parse_not())
        return " AND ".join(parts)

    def _parse_not(self) -> str:
        negated = False
        while self.c.is_ident("NOT"):
            self.c.bump()
            negated = not negated
        body = self._parse_primary()
        return f"NOT {body}" if negated else body

    def _parse_primary(self) -> str:
        if self.c.is_punct("("):
            self.c.bump()
            inner = self._parse_or()
            self.c.expect_punct(")")
            return f"({inner})"
        return self._parse_comparison()

    def _parse_comparison(self) -> str:
        column = self._parse_column()
        if self.c.is_ident("IS"):
            self.c.bump()
            not_null = False
            if self.c.is_ident("NOT"):
                self.c.bump()
                not_null = True
            self.c.expect_ident("NULL")
            return f"{column} IS {'NOT ' if not_null else ''}NULL"
        negated = False
        if self.c.is_ident("NOT"):
            self.c.bump()
            negated = True
        if self.c.is_ident("IN"):
            if negated:
                # already consumed NOT
                pass
            self.c.bump()
            self.c.expect_punct("(")
            binds = [self._bind()]
            while self.c.is_punct(","):
                self.c.bump()
                binds.append(self._bind())
            self.c.expect_punct(")")
            keyword = "NOT IN" if negated else "IN"
            return f"{column} {keyword} ({', '.join(binds)})"
        if self.c.is_ident("BETWEEN"):
            self.c.bump()
            low = self._bind()
            self.c.expect_ident("AND")
            high = self._bind()
            keyword = "NOT BETWEEN" if negated else "BETWEEN"
            return f"{column} {keyword} {low} AND {high}"
        if self.c.is_ident("LIKE"):
            self.c.bump()
            pattern = self._bind()
            keyword = "NOT LIKE" if negated else "LIKE"
            return f"{column} {keyword} {pattern}"
        if negated:
            raise InvalidSqlError("NOT 뒤에 IN/BETWEEN/LIKE만 올 수 있습니다", position=self.c.pos())
        if self.c.is_op() and self.c.peek() and self.c.peek().value in _COMPARE_OPS:
            op = self.c.bump().value
            rhs = self._bind()
            return f"{column} {op} {rhs}"
        raise InvalidSqlError("where 비교가 올바르지 않습니다", position=self.c.pos())

    def _parse_column(self) -> str:
        if not self.c.is_ident():
            raise InvalidSqlError("where에는 카탈로그 컬럼만 허용됩니다", position=self.c.pos())
        parts = [self._ident()]
        while self.c.is_punct("."):
            self.c.bump()
            parts.append(self._ident())
        if len(parts) == 1:
            name = parts[0]
        elif len(parts) == 2:
            prefix, name = parts
            if self.table_name and prefix not in {self.table_name}:
                raise UnknownObjectError(f"카탈로그에 없는 식별자: {prefix}.{name}")
        elif len(parts) == 3:
            owner, table, name = parts
            if self.owner and owner != self.owner:
                raise UnknownObjectError(f"카탈로그에 없는 식별자: {owner}.{table}.{name}")
            if self.table_name and table != self.table_name:
                raise UnknownObjectError(f"카탈로그에 없는 식별자: {owner}.{table}.{name}")
        else:
            raise InvalidSqlError("컬럼 참조가 모호합니다", position=self.c.pos())
        if name not in self.allowed_columns:
            raise UnknownObjectError(f"카탈로그에 없는 식별자: {name}")
        return quote_identifier(name, max_parts=1)

    def _ident(self) -> str:
        token = self.c.bump()
        if token.kind != "IDENT":
            raise InvalidSqlError("식별자가 필요합니다", position=token.pos)
        if token.upper in _FORBIDDEN_KEYWORDS or token.upper in {
            "SELECT",
            "FROM",
            "WHERE",
            "GROUP",
            "HAVING",
            "ORDER",
            "UNION",
            "JOIN",
        }:
            raise InvalidSqlError(f"where에 허용되지 않은 키워드입니다: {token.upper}", position=token.pos)
        return token.upper

    def _bind(self) -> str:
        token = self.c.peek()
        if token is None or token.kind != "BIND":
            raise InvalidSqlError(
                "where 조건의 값은 바인드 변수만 허용됩니다",
                position=self.c.pos(),
                hint="문자열/숫자 리터럴 대신 :name 바인드를 사용하세요",
            )
        self.c.bump()
        self.bind_names.append(token.value)
        return f":{token.value}"
