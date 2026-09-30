"""
In-memory store of grading sessions: one per signed-in teacher.

Nothing is written to disk or a database. A session disappears when the
teacher clears it, when a new one is started, after SESSION_TTL_MINUTES of
inactivity, or when the server restarts. This keeps student information
(names from QR codes, answers, scan snippets) out of long-term storage.

Because sessions are in memory, run the app as a SINGLE process
(uvicorn with one worker), otherwise requests could land on a process that
does not hold the teacher's session.
"""

import threading
import time

from .config import settings
from .grading import GradingSession

_sessions: dict[str, GradingSession] = {}
_lock = threading.Lock()


def _expired(s: GradingSession) -> bool:
    return time.time() - s.touched > settings.session_ttl_minutes * 60


def _sweep() -> None:
    """Drop every expired session (called on each access; cheap)."""
    for user_id in [u for u, s in _sessions.items() if _expired(s)]:
        del _sessions[user_id]


def get(user_id: str) -> GradingSession | None:
    with _lock:
        _sweep()
        return _sessions.get(user_id)


def start(user_id: str) -> GradingSession:
    """Start a fresh session, discarding any previous one for this teacher."""
    with _lock:
        _sweep()
        session = GradingSession(user_id=user_id)
        _sessions[user_id] = session
        return session


def clear(user_id: str) -> None:
    with _lock:
        _sessions.pop(user_id, None)
