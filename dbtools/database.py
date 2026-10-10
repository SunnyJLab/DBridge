"""Oracle adapter for MCP DB tools.

Importing this module does not load ``oracledb``. The driver is imported on
first pool use. User SQL is not accepted or validated here; tool services own
SELECT-only guardrails. This module executes caller-supplied SQL with binds,
timeouts, row caps, and value normalization.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from typing import Any, NoReturn, Protocol, TypeVar

from .errors import (
    BadParamError,
    InvalidSqlError,
    NoAccessError,
    TimeoutError as ToolTimeoutError,
    error_code,
    _ACCESS_ORA_CODES,
    _TIMEOUT_FULL_CODES,
)
from .sql_safety import validate_select_sql

log = logging.getLogger("pool")

T = TypeVar("T")

DEFAULT_QUERY_TIMEOUT_S = 30.0
DEFAULT_POOL_MAX = 5
DEFAULT_MAX_ROWS = 100
LOB_CHAR_LIMIT = 2000
TRUNCATION_MARK = "(truncated)"

# ORA-01013: user requested cancel of current operation (call timeout / cancel).
# ORA-01014 is shutdown in progress, not a timeout.
_TIMEOUT_ORA_CODES = frozenset({1013})
_INVALID_SQL_ORA_CODES = frozenset({1745})

_oracledb: Any | None = None


class AdapterError(Exception):
    """Driver or adapter failure that is not a mapped tool error.

    Tool services may translate this. Timeout and privilege/connectivity
    failures are raised as :class:`ToolTimeoutError` / :class:`NoAccessError`
    instead when those classes are available.
    """

    def __init__(self, message: str, *, ora: str | None = None) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("AdapterError message must be a non-empty string")
        self.message = message.strip()
        self.ora = ora if ora else None
        super().__init__(self.message)


@dataclass(frozen=True)
class QueryResult:
    """Column array + row array. Values are JSON-friendly Python types."""

    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]]
    elapsed_ms: int
    truncated: bool
    column_types: tuple[str, ...] = ()

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class ConnectionConfig:
    name: str
    dsn: str
    user: str
    password_env: str | None
    pool_max: int
    password: str | None = None


@dataclass(frozen=True)
class ConnectionStatus:
    name: str
    dsn: str
    state: str
    latency_ms: int | None
    pool_used: int
    pool_max: int
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "dsn": self.dsn,
            "state": self.state,
            "latency_ms": self.latency_ms,
            "pool_used": self.pool_used,
            "pool_max": self.pool_max,
        }
        if self.error:
            payload["error"] = self.error
        return payload


class Database(Protocol):
    """Minimal surface used by catalog and tool handlers."""

    def connection_names(self) -> tuple[str, ...]: ...

    def connection_config(self, name: str) -> ConnectionConfig: ...

    def execute(
        self,
        connection: str,
        sql: str,
        *,
        binds: Mapping[str, Any] | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: float | None = None,
    ) -> QueryResult:
        """Run one read-only statement. 시그니처는 이것 하나뿐이다 — 호출부는
        ``common.execution.execute`` 로 직접 호출하고 대안 시그니처를 탐색하지 않는다."""
        ...

    def parse(self, connection: str, sql: str, *, timeout_s: float | None = None) -> bool:
        """Parse one statement server-side without executing it."""
        ...

    def transaction(
        self,
        connection: str,
        callback: Callable[[Any], T],
        *,
        timeout_s: float | None = None,
    ) -> T:
        """Committing transaction. ``explain`` 의 PLAN_TABLE 쓰기 전용이다."""
        ...

    def snapshot(self) -> tuple[ConnectionStatus, ...]: ...

    def close(self) -> None: ...


class OracleDatabase:
    """Named, lazy, thread-safe python-oracledb pools."""

    def __init__(
        self,
        config: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        query_timeout: float | None = None,
    ) -> None:
        connections, timeout = _parse_config(config, query_timeout)
        self._timeout_s = timeout
        self._configs: dict[str, ConnectionConfig] = connections
        self._pools: dict[str, Any] = {}
        self._pool_timedwait: dict[str, bool] = {}
        self._pool_acquire_locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()
        self._closed = False

    def connection_names(self) -> tuple[str, ...]:
        return tuple(self._configs)

    def connection_config(self, name: str) -> ConnectionConfig:
        return self._require_config(name)

    def execute(
        self,
        connection: str,
        sql: str,
        *,
        binds: Mapping[str, Any] | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: float | None = None,
    ) -> QueryResult:
        if not isinstance(sql, str) or not sql.strip():
            raise BadParamError("SQL이 비어 있습니다")
        row_cap = _require_max_rows(max_rows)
        bind_values = _normalize_binds(binds)
        timeout = self._resolve_timeout(timeout_s)
        started = time.perf_counter()
        deadline = started + timeout

        def _run(conn: Any) -> QueryResult:
            cursor = conn.cursor()
            try:
                _set_call_timeout(conn, deadline)
                cursor.execute(sql, bind_values)
                columns = _column_names(cursor)
                column_types = tuple(
                    item[1].name.removeprefix("DB_TYPE_")
                    for item in (cursor.description or ())
                )
                fetch_cap = row_cap + 1
                _set_call_timeout(conn, deadline)
                fetched = cursor.fetchmany(fetch_cap)
                truncated = len(fetched) > row_cap
                if truncated:
                    fetched = fetched[:row_cap]
                rows = [
                    tuple(
                        _normalize_value(cell, conn=conn, deadline=deadline)
                        for cell in row
                    )
                    for row in fetched
                ]
            finally:
                try:
                    _set_call_timeout(conn, deadline)
                except ToolTimeoutError:
                    _cancel_connection(conn)
                cursor.close()
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            return QueryResult(
                columns=columns,
                rows=rows,
                elapsed_ms=elapsed_ms,
                truncated=truncated,
                column_types=column_types,
            )

        return self._with_connection(
            connection,
            _run,
            timeout_s=timeout,
            commit=False,
            deadline=deadline,
        )

    def parse(self, connection: str, sql: str, *, timeout_s: float | None = None) -> bool:
        """Parse one statement server-side without executing it (``validate_sql``).

        ``Cursor.parse()`` 는 Oracle 에 파싱만 요청한다 — 행을 읽지 않고 계획도
        만들지 않으므로 대형 테이블을 건드리지 않는다. 구문·식별자·권한 오류는
        여기서 드라이버 예외로 올라오고 호출부가 ``valid: false`` 로 변환한다.
        DBMS_SQL 권한은 필요하지 않다.
        """
        statement = validate_select_sql(sql)
        timeout = self._resolve_timeout(timeout_s)
        deadline = time.perf_counter() + timeout

        def _run(conn: Any) -> bool:
            cursor = conn.cursor()
            try:
                _set_call_timeout(conn, deadline)
                cursor.parse(statement)
            finally:
                cursor.close()
            return True

        return self._with_connection(
            connection, _run, timeout_s=timeout, commit=False, deadline=deadline
        )

    def transaction(
        self,
        connection: str,
        callback: Callable[[Any], T],
        *,
        timeout_s: float | None = None,
    ) -> T:
        """Run ``callback(connection)`` and commit — 이 프록시의 유일한 쓰기 경로.

        SELECT-only 정책의 예외는 ``explain`` 하나다. Oracle ``EXPLAIN PLAN`` 은
        조회가 아니라 전용 PLAN_TABLE 에 INSERT 하고, 도구가 끝에서 자기
        STATEMENT_ID 행만 DELETE 한다. 그래서 커밋이 필요하다.

        보안 경계: 호출부는 ``explain`` 뿐이고, 대상 테이블은 ``config.explain
        .plan_table`` 에 명시된 것만 허용한다 (임의 테이블 금지). 업무 테이블에
        대한 쓰기 권한은 계정 자체에 부여하지 않는 것을 전제한다.
        """
        if not callable(callback):
            raise BadParamError("transaction callback이 호출 가능하지 않습니다")
        timeout = self._resolve_timeout(timeout_s)
        deadline = time.perf_counter() + timeout
        return self._with_connection(
            connection,
            callback,
            timeout_s=timeout,
            commit=True,
            deadline=deadline,
        )

    def snapshot(self) -> tuple[ConnectionStatus, ...]:
        return tuple(self._status(name) for name in self._configs)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pools = list(self._pools.items())
            self._pools.clear()
            self._pool_timedwait.clear()
            self._pool_acquire_locks.clear()
        for name, pool in pools:
            try:
                pool.close()
            except Exception:
                log.exception("pool close failed: %s", name)

    def _require_config(self, name: str) -> ConnectionConfig:
        if not isinstance(name, str) or not name.strip():
            raise BadParamError("커넥션 이름이 비어 있습니다")
        try:
            return self._configs[name]
        except KeyError:
            raise BadParamError(
                f"등록되지 않은 커넥션: {name}",
                hint="list_connections로 커넥션 이름을 확인하세요",
            ) from None

    def _resolve_timeout(self, timeout_s: float | None) -> float:
        value = self._timeout_s if timeout_s is None else timeout_s
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise BadParamError("query timeout은 숫자(초)여야 합니다")
        if value <= 0:
            raise BadParamError("query timeout은 0보다 커야 합니다")
        return float(value)

    def _pool(self, name: str) -> Any:
        cfg = self._require_config(name)
        with self._lock:
            if self._closed:
                raise AdapterError("database adapter is closed")
            pool = self._pools.get(name)
            if pool is not None:
                return pool
            pool, timedwait = _create_pool(cfg)
            self._pools[name] = pool
            self._pool_timedwait[name] = timedwait
            self._pool_acquire_locks.setdefault(name, threading.Lock())
            return pool

    def _with_connection(
        self,
        name: str,
        callback: Callable[[Any], T],
        *,
        timeout_s: float,
        commit: bool,
        deadline: float | None = None,
    ) -> T:
        if deadline is None:
            deadline = time.perf_counter() + timeout_s
        pool = self._pool(name)
        conn: Any | None = None
        poison = False
        with self._lock:
            timedwait = bool(self._pool_timedwait.get(name, False))
            acquire_lock = self._pool_acquire_locks.get(name)
        try:
            try:
                conn = self._acquire_bounded(
                    pool,
                    deadline,
                    timedwait=timedwait,
                    acquire_lock=acquire_lock,
                )
            except Exception as exc:
                _raise_translated(exc)
            try:
                _set_call_timeout(conn, deadline)
                result = callback(conn)
            except Exception:
                poison = True
                _fail_closed_cleanup(conn, deadline)
                raise
            try:
                _set_call_timeout(conn, deadline)
                if commit:
                    conn.commit()
                else:
                    conn.rollback()
            except Exception:
                poison = True
                _fail_closed_cleanup(conn, deadline)
                raise
            return result
        except Exception as exc:
            _raise_translated(exc)
            raise
        finally:
            _return_connection(pool, conn, drop=poison)

    def _acquire_bounded(
        self,
        pool: Any,
        deadline: float,
        *,
        timedwait: bool,
        acquire_lock: threading.Lock | None,
    ) -> Any:
        remaining_s = deadline - time.perf_counter()
        if remaining_s <= 0:
            raise ToolTimeoutError("query timeout exceeded")
        if acquire_lock is None:
            acquire_lock = threading.Lock()
        got = acquire_lock.acquire(timeout=remaining_s)
        if not got:
            raise ToolTimeoutError("query timeout exceeded")
        try:
            remaining_ms = _remaining_ms(deadline)
            _apply_pool_wait_timeout(pool, remaining_ms, timedwait=timedwait)
            return pool.acquire()
        finally:
            acquire_lock.release()

    def _status(self, name: str) -> ConnectionStatus:
        cfg = self._configs[name]
        pool_max = cfg.pool_max
        pool_used = 0
        with self._lock:
            pool = self._pools.get(name)
        if pool is not None:
            pool_used = _pool_busy(pool)
        started = time.perf_counter()
        try:
            self.execute(
                name,
                "SELECT 1 FROM DUAL",
                max_rows=1,
                timeout_s=min(self._timeout_s, 5.0),
            )
            latency_ms = max(0, int((time.perf_counter() - started) * 1000))
            with self._lock:
                pool = self._pools.get(name)
            if pool is not None:
                pool_used = _pool_busy(pool)
            return ConnectionStatus(
                name=cfg.name,
                dsn=cfg.dsn,
                state="ok",
                latency_ms=latency_ms,
                pool_used=pool_used,
                pool_max=pool_max,
            )
        except Exception as exc:
            log.warning("connection %s status failed: %s", name, exc)
            error = error_code(exc)
            with self._lock:
                pool = self._pools.get(name)
            if pool is not None:
                pool_used = _pool_busy(pool)
            return ConnectionStatus(
                name=cfg.name,
                dsn=cfg.dsn,
                state="error",
                latency_ms=None,
                pool_used=pool_used,
                pool_max=pool_max,
                error=error,
            )


def _parse_config(
    config: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    query_timeout: float | None,
) -> tuple[dict[str, ConnectionConfig], float]:
    if isinstance(config, Mapping) and "connections" in config:
        raw_list = config["connections"]
        proxy = config.get("proxy") if isinstance(config.get("proxy"), Mapping) else {}
        timeout = query_timeout
        if timeout is None and isinstance(proxy, Mapping) and "query_timeout" in proxy:
            timeout = proxy["query_timeout"]
    elif isinstance(config, Mapping) and "name" in config:
        raw_list = [config]
        timeout = query_timeout
    else:
        raw_list = config
        timeout = query_timeout
    if timeout is None:
        timeout = DEFAULT_QUERY_TIMEOUT_S
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise BadParamError("proxy.query_timeout은 0보다 큰 숫자(초)여야 합니다")
    if not isinstance(raw_list, Sequence) or isinstance(raw_list, (str, bytes)):
        raise BadParamError("connections는 객체 배열이어야 합니다")
    parsed: dict[str, ConnectionConfig] = {}
    for index, item in enumerate(raw_list):
        cfg = _parse_connection(item, index)
        if cfg.name in parsed:
            raise BadParamError(f"커넥션 이름이 중복됩니다: {cfg.name}")
        parsed[cfg.name] = cfg
    return parsed, float(timeout)


def _parse_connection(item: Any, index: int) -> ConnectionConfig:
    if not isinstance(item, Mapping):
        raise BadParamError(f"connections[{index}]는 객체여야 합니다")
    name = item.get("name")
    dsn = item.get("dsn")
    user = item.get("user")
    password_env = item.get("password_env")
    password = item.get("password")
    pool_max = item.get("pool_max", DEFAULT_POOL_MAX)
    if not isinstance(name, str) or not name.strip():
        raise BadParamError(f"connections[{index}].name이 비어 있습니다")
    if not isinstance(dsn, str) or not dsn.strip():
        raise BadParamError(f"커넥션 {name!r}의 dsn이 비어 있습니다")
    if not isinstance(user, str) or not user.strip():
        raise BadParamError(f"커넥션 {name!r}의 user가 비어 있습니다")
    if password is not None:
        if not isinstance(password, str):
            raise BadParamError(f"커넥션 {name!r}의 password가 문자열이 아닙니다")
        if password == "":
            raise BadParamError(f"커넥션 {name!r}의 password가 비어 있습니다")
    env_name: str | None = None
    if password_env is not None:
        if not isinstance(password_env, str):
            raise BadParamError(f"커넥션 {name!r}의 password_env가 문자열이 아닙니다")
        stripped_env = password_env.strip()
        if stripped_env:
            env_name = stripped_env
    if password is None and env_name is None:
        raise BadParamError(
            f"커넥션 {name!r}의 password 또는 password_env가 필요합니다"
        )
    if not isinstance(pool_max, int) or isinstance(pool_max, bool) or pool_max < 1:
        raise BadParamError(f"커넥션 {name!r}의 pool_max는 1 이상의 정수여야 합니다")
    return ConnectionConfig(
        name=name.strip(),
        dsn=dsn.strip(),
        user=user.strip(),
        password_env=env_name,
        pool_max=pool_max,
        password=password,
    )


def _require_oracledb() -> Any:
    global _oracledb
    if _oracledb is not None:
        return _oracledb
    try:
        import oracledb as driver
    except ImportError as exc:
        raise AdapterError(
            "oracledb driver is not installed",
        ) from exc
    _oracledb = driver
    return driver


def _driver_pool_getmode(driver: Any, name: str) -> Any | None:
    value = getattr(driver, f"POOL_GETMODE_{name}", None)
    if value is not None:
        return value
    enum = getattr(getattr(driver, "pool", None), "PoolGetMode", None)
    return getattr(enum, name, None)


def _bounded_pool_getmode(driver: Any) -> tuple[Any, bool]:
    timedwait = _driver_pool_getmode(driver, "TIMEDWAIT")
    if timedwait is not None:
        return timedwait, True
    nowait = _driver_pool_getmode(driver, "NOWAIT")
    if nowait is not None:
        return nowait, False
    raise AdapterError("oracledb driver does not support bounded pool acquire")


def _create_pool(cfg: ConnectionConfig) -> tuple[Any, bool]:
    if cfg.password is not None:
        password = cfg.password
    else:
        env_name = cfg.password_env
        password = os.environ.get(env_name) if env_name else None
        if not isinstance(password, str) or password == "":
            raise AdapterError(
                f"환경변수 {env_name}가 없거나 비어 있습니다",
            )
    driver = _require_oracledb()
    getmode, timedwait = _bounded_pool_getmode(driver)
    kwargs: dict[str, Any] = {
        "user": cfg.user,
        "password": password,
        "dsn": cfg.dsn,
        "min": 0,
        "max": cfg.pool_max,
        "increment": 1,
        "getmode": getmode,
    }
    if timedwait:
        kwargs["wait_timeout"] = max(1, int(DEFAULT_QUERY_TIMEOUT_S * 1000))
    try:
        pool = driver.create_pool(**kwargs)
    except TypeError:
        if not timedwait or "wait_timeout" not in kwargs:
            raise AdapterError(
                "oracledb create_pool does not accept bounded getmode",
            ) from None
        kwargs.pop("wait_timeout")
        try:
            pool = driver.create_pool(**kwargs)
        except TypeError:
            raise AdapterError(
                "oracledb create_pool does not accept bounded getmode",
            ) from None
    except Exception as exc:
        if isinstance(password, str) and password:
            text = str(exc).replace(password, "***") or "driver error"
            try:
                wrapped: BaseException = type(exc)(text)
            except Exception:
                wrapped = Exception(text)
            _raise_translated(wrapped)
        _raise_translated(exc)
    if timedwait and not hasattr(pool, "wait_timeout"):
        closer = getattr(pool, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                log.debug(
                    "pool close after missing wait_timeout failed",
                    exc_info=True,
                )
        raise AdapterError("oracledb pool lacks wait_timeout")
    return pool, timedwait


def _apply_pool_wait_timeout(pool: Any, timeout_ms: int, *, timedwait: bool) -> None:
    if not timedwait:
        return
    try:
        pool.wait_timeout = timeout_ms
    except Exception as exc:
        raise AdapterError("oracledb pool wait_timeout cannot be set") from exc


def _remaining_ms(deadline: float) -> int:
    left = deadline - time.perf_counter()
    ms = int(left * 1000)
    if left <= 0 or ms < 1:
        raise ToolTimeoutError("query timeout exceeded")
    return ms


def _set_call_timeout(conn: Any, deadline: float) -> None:
    remaining_ms = _remaining_ms(deadline)
    try:
        conn.call_timeout = remaining_ms
    except Exception as exc:
        raise AdapterError("driver does not support call_timeout") from exc


def _cancel_connection(conn: Any) -> None:
    cancel = getattr(conn, "cancel", None)
    if not callable(cancel):
        return
    try:
        cancel()
    except Exception:
        log.debug("connection cancel failed", exc_info=True)


def _fail_closed_cleanup(conn: Any, deadline: float) -> None:
    _cancel_connection(conn)
    try:
        leftover = deadline - time.perf_counter()
        if leftover > 0:
            try:
                conn.call_timeout = max(1, int(leftover * 1000))
            except Exception:
                pass
        else:
            try:
                conn.call_timeout = 1
            except Exception:
                pass
        _safe_rollback(conn)
    except Exception:
        log.debug("timeout cleanup failed", exc_info=True)


def _return_connection(pool: Any, conn: Any, *, drop: bool) -> None:
    if conn is None:
        return
    if drop:
        drop_fn = getattr(pool, "drop", None)
        if callable(drop_fn):
            try:
                drop_fn(conn)
                return
            except Exception:
                log.exception("pool.drop failed")
    try:
        pool.release(conn)
    except Exception:
        log.exception("pool.release failed")
        drop_fn = getattr(pool, "drop", None)
        if callable(drop_fn):
            try:
                drop_fn(conn)
            except Exception:
                log.exception("pool.drop after release failure failed")


def _safe_rollback(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        log.debug("rollback failed", exc_info=True)


def _pool_busy(pool: Any) -> int:
    for attr in ("busy", "opened"):
        value = getattr(pool, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _require_max_rows(max_rows: Any) -> int:
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
        raise BadParamError("max_rows는 1 이상의 정수여야 합니다")
    return max_rows


def _normalize_binds(binds: Mapping[str, Any] | None) -> dict[str, Any]:
    if binds is None:
        return {}
    if not isinstance(binds, Mapping):
        raise BadParamError("binds는 객체여야 합니다")
    out: dict[str, Any] = {}
    for raw_key, value in binds.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise BadParamError("바인드 이름이 비어 있습니다")
        key = raw_key.strip()
        if key.startswith(":"):
            key = key[1:]
        if not key:
            raise BadParamError("바인드 이름이 비어 있습니다")
        out[key] = value
    return out


# 커서 description 에서 결과 컬럼명을 뽑는다. 카탈로그 오브젝트에서 뽑는
# sql_safety._column_names 와는 다른 함수다 (이름만 같음).
def _column_names(cursor: Any) -> tuple[str, ...]:
    description = getattr(cursor, "description", None)
    if not description:
        return ()
    names: list[str] = []
    for item in description:
        name = item[0] if isinstance(item, Sequence) else getattr(item, "name", None)
        names.append("" if name is None else str(name))
    return tuple(names)


def _normalize_value(
    value: Any,
    *,
    conn: Any | None = None,
    deadline: float | None = None,
) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return format(value, "f")
    if isinstance(value, datetime):
        if value.microsecond:
            return value.isoformat(sep=" ", timespec="microseconds")
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dt_time):
        return value.isoformat()
    if isinstance(value, bytes):
        text = value.hex()
        return _truncate_text(text)
    if isinstance(value, bytearray):
        return _truncate_text(bytes(value).hex())
    if isinstance(value, memoryview):
        return _truncate_text(value.tobytes().hex())
    reader = getattr(value, "read", None)
    if callable(reader):
        if conn is not None and deadline is not None:
            _set_call_timeout(conn, deadline)
        try:
            chunk = reader(LOB_CHAR_LIMIT + 1)
        except TypeError:
            chunk = reader()
        except (ToolTimeoutError, AdapterError):
            raise
        except Exception:
            return _truncate_text(str(value))
        if isinstance(chunk, bytes):
            return _truncate_text(chunk.hex())
        if chunk is None:
            return None
        return _truncate_text(str(chunk))
    if isinstance(value, str):
        return _truncate_text(value)
    return _truncate_text(str(value))


def _truncate_text(text: str) -> str:
    if len(text) <= LOB_CHAR_LIMIT:
        return text
    return text[:LOB_CHAR_LIMIT] + TRUNCATION_MARK


def _error_fields(exc: BaseException) -> tuple[int | None, str | None, str]:
    args0 = exc.args[0] if exc.args else None
    code = getattr(args0, "code", None)
    if code is None:
        code = getattr(exc, "code", None)
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if not isinstance(code, int):
        code = None
    full = getattr(args0, "full_code", None)
    if not isinstance(full, str) or not full.strip():
        full = getattr(exc, "full_code", None)
    if isinstance(full, str):
        full = full.strip()
    else:
        full = None
    message = getattr(args0, "message", None)
    if not isinstance(message, str) or not message.strip():
        message = str(exc)
    message = message.strip()
    if full is None and code is not None:
        full = f"ORA-{code:05d}"
    if full is None:
        extracted = _extract_ora_token(message)
        if extracted:
            full = extracted
    return code, full, message


def _extract_ora_token(message: str) -> str | None:
    upper = message.upper()
    for prefix in ("ORA-", "DPI-", "DPY-"):
        start = upper.find(prefix)
        if start < 0:
            continue
        token = [message[start : start + len(prefix)]]
        i = start + len(prefix)
        while i < len(message) and message[i].isdigit():
            token.append(message[i])
            i += 1
        if len(token) > 1:
            return "".join(token)
    return None


def _ora_in(code: int | None, full: str | None, codes: frozenset[int]) -> bool:
    if code in codes:
        return True
    if not full:
        return False
    token = full.upper()
    if token.startswith("ORA-"):
        digits = token[4:]
        if digits.isdigit() and int(digits) in codes:
            return True
    return False


def _is_timeout(code: int | None, full: str | None, message: str) -> bool:
    if _ora_in(code, full, _TIMEOUT_ORA_CODES):
        return True
    if full and full.upper() in _TIMEOUT_FULL_CODES:
        return True
    lowered = message.lower()
    return "timed out" in lowered or "timeout" in lowered or "call timeout" in lowered


def _is_access(code: int | None, full: str | None) -> bool:
    return _ora_in(code, full, _ACCESS_ORA_CODES)


def _is_invalid_sql(code: int | None, full: str | None) -> bool:
    return _ora_in(code, full, _INVALID_SQL_ORA_CODES)


def _raise_translated(exc: BaseException) -> NoReturn:
    if isinstance(
        exc,
        (
            ToolTimeoutError,
            NoAccessError,
            BadParamError,
            InvalidSqlError,
            AdapterError,
        ),
    ):
        raise exc
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        raise exc
    code, full, message = _error_fields(exc)
    if _is_timeout(code, full, message):
        raise ToolTimeoutError(message, ora=full) from exc
    if _is_invalid_sql(code, full):
        raise InvalidSqlError(message, ora=full) from exc
    if _is_access(code, full):
        raise NoAccessError(message, ora=full) from exc
    raise AdapterError(message, ora=full) from exc
