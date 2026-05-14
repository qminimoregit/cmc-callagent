# src/session_store.py
from __future__ import annotations
"""
Redis-backed session store for CMC Assistant.

Uses a module-level connection pool so all Uvicorn workers (in the same
process) share a single pool — avoids opening a new TCP connection for every
webhook request.

Falls back to a local in-memory dict when REDIS_URL is not set
(useful for local development / single-worker unit tests).
"""

import json
import logging
import os
from typing import Any

import redis as _redis_lib

logger = logging.getLogger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "")
SESSION_TTL: int = 3600   # seconds — sessions expire after 1 hour of inactivity
CALL_START_KEY = "call_start:"

# ── Connection pool — created once per process ─────────────────────────────
_pool: "_redis_lib.ConnectionPool | None" = None

def _get_pool() -> "_redis_lib.ConnectionPool | None":
    """Return a shared connection pool, or None if Redis is not configured."""
    global _pool
    if not REDIS_URL:
        return None
    if _pool is None:
        try:
            _pool = _redis_lib.ConnectionPool.from_url(
                REDIS_URL,
                decode_responses=True,
                max_connections=50,       # handle 50 concurrent webhook threads
                socket_connect_timeout=2,
                socket_timeout=2,
                retry_on_timeout=True,
            )
            logger.info("Redis connection pool created → %s (max_connections=50)", REDIS_URL)
        except Exception as exc:
            logger.error("Redis pool creation failed: %s", exc)
            return None
    return _pool


def _get_redis() -> "_redis_lib.Redis | None":
    """Return a Redis client from the shared pool, or None if unavailable."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        client = _redis_lib.Redis(connection_pool=pool)
        client.ping()
        return client
    except Exception as exc:
        logger.warning("Redis ping failed (%s); falling back to in-memory store.", exc)
        return None


# ── Local fallback (used when REDIS_URL is empty) ──────────────────────────
_local: dict[str, dict] = {}
_local_call_start: dict[str, float] = {}

_DEFAULT_SESSION: dict[str, Any] = {
    "history": [],
    "lang": None,
    "lang_confirmed": False,
    "silence_strikes": 0,
    "last_agent_question": "",
}


# ── Session helpers ────────────────────────────────────────────────────────

def get_session(call_sid: str) -> dict:
    """
    Retrieve session for a call, creating a fresh one if it doesn't exist.
    Always returns a mutable copy — call save_session() after mutating.
    """
    r = _get_redis()
    if r is None:
        return _local.setdefault(call_sid, dict(_DEFAULT_SESSION))

    raw = r.get(f"session:{call_sid}")
    if raw:
        return json.loads(raw)

    # First time — initialise and persist
    session = dict(_DEFAULT_SESSION)
    r.setex(f"session:{call_sid}", SESSION_TTL, json.dumps(session))
    return session


def save_session(call_sid: str, data: dict) -> None:
    """Persist updated session data. Must be called after every mutation."""
    r = _get_redis()
    if r is None:
        _local[call_sid] = data
        return
    r.setex(f"session:{call_sid}", SESSION_TTL, json.dumps(data))


def clear_session(call_sid: str) -> None:
    """Remove a session (call ended / escalated)."""
    r = _get_redis()
    if r is None:
        _local.pop(call_sid, None)
        return
    r.delete(f"session:{call_sid}")


# ── Call-start time helpers (Redis-backed so multi-worker safe) ────────────

def set_call_start(call_sid: str, ts: float) -> None:
    """Record the Unix timestamp when a call began."""
    r = _get_redis()
    if r is None:
        _local_call_start[call_sid] = ts
        return
    r.setex(f"{CALL_START_KEY}{call_sid}", SESSION_TTL, str(ts))


def pop_call_start(call_sid: str, default: float | None = None) -> float:
    """Return and remove the call-start timestamp."""
    r = _get_redis()
    if r is None:
        return _local_call_start.pop(call_sid, default or 0.0)
    raw = r.getdel(f"{CALL_START_KEY}{call_sid}")
    return float(raw) if raw else (default or 0.0)
