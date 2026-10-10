"""In-memory MCP session store. Lock covers dict transitions only."""

from __future__ import annotations

import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

STREAM_SENTINEL = object()


@dataclass
class Session:
    """One MCP session: id, initialize 완료 여부, 그리고 붙어 있는 SSE 스트림."""
    id: str
    created_at: float
    last_activity: float
    initialized: bool = False
    queue: queue.Queue = field(default_factory=queue.Queue)
    close_event: threading.Event = field(default_factory=threading.Event)
    sse_active: bool = False
    stream_token: str | None = None
    stream_socket: Any = None


class SessionStore:
    """세션 생성·조회·만료를 한 락 아래에서 관리한다.

    ``sweep()`` 이 유휴 세션을 정리하고 ``close_all()`` 은 종료 시 스트림 소켓까지
    닫는다. 스트림 소켓을 들고 있는 이유는 ``stop()`` 때 블로킹된 SSE 응답을
    강제로 깨워야 하기 때문이다.
    """
    def __init__(self, *, idle_timeout_s: float = 1800.0) -> None:
        self._idle_timeout_s = idle_timeout_s
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        now = time.time()
        session = Session(
            id=secrets.token_urlsafe(32),
            created_at=now,
            last_activity=now,
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._is_expired(session, now):
                self._drop_locked(session)
                return None
            session.last_activity = now
            return session

    def mark_initialized(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.initialized = True
            session.last_activity = time.time()
            return True

    def attach_stream(self, session_id: str) -> str | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.sse_active:
                return None
            token = secrets.token_urlsafe(16)
            session.sse_active = True
            session.stream_token = token
            session.last_activity = time.time()
            return token

    def set_stream_socket(self, session_id: str, token: str, sock: Any) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.stream_token != token:
                return
            session.stream_socket = sock

    def detach_stream(self, session_id: str, token: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.stream_token != token:
                return False
            session.sse_active = False
            session.stream_token = None
            session.stream_socket = None
            return True

    def active_stream_sockets(self) -> list[Any]:
        with self._lock:
            return [
                session.stream_socket
                for session in self._sessions.values()
                if session.stream_socket is not None
            ]

    def inspect(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def close(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        self._signal(session)
        return True

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._signal(session)

    def sweep(self) -> int:
        now = time.time()
        expired: list[Session] = []
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if self._is_expired(session, now):
                    expired.append(self._sessions.pop(session_id))
        for session in expired:
            self._signal(session)
        return len(expired)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def notify(self, session_id: str, payload: dict[str, Any]) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        session.queue.put(payload)
        return True

    def _is_expired(self, session: Session, now: float) -> bool:
        return (now - session.last_activity) > self._idle_timeout_s

    def _drop_locked(self, session: Session) -> None:
        self._sessions.pop(session.id, None)
        self._signal(session)

    @staticmethod
    def _signal(session: Session) -> None:
        session.close_event.set()
        try:
            session.queue.put_nowait(STREAM_SENTINEL)
        except queue.Full:
            pass


__all__ = ["STREAM_SENTINEL", "Session", "SessionStore"]
