"""Shared, persistent result cache backed by SQLite.

Why not a per-process dict: prod runs the API under 2 uvicorn workers, so an
in-memory cache only caught ~half the repeat requests (the other half hit the
other worker's cold cache), and every restart threw the cache away. The slow
live score paths (noise especially: a ~1s road/rail/aircraft/ML stack that
misses the pre-baked regional cache) therefore kept recomputing.

One SQLite file, opened in WAL mode, is shared across both workers and survives
restarts, so it self-populates: the first visit to a point pays the cost, every
later visit — any worker, after any restart — is a ~sub-ms lookup. Reads are
lock-free under WAL; writes are serialised behind a short busy-timeout. Every
operation degrades gracefully to "no cache" on any error, so a cache problem can
never break scoring.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

_DEFAULT_TTL = 86400.0  # 24h — road/satellite/terrain inputs change very slowly

_conns: dict[str, sqlite3.Connection] = {}
_locks: dict[str, threading.Lock] = {}
_setup_lock = threading.Lock()


def _conn(db_path: str) -> sqlite3.Connection | None:
    c = _conns.get(db_path)
    if c is not None:
        return c
    try:
        with _setup_lock:
            c = _conns.get(db_path)
            if c is not None:
                return c
            c = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=3000")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute(
                "CREATE TABLE IF NOT EXISTS cache "
                "(k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL)"
            )
            c.commit()
            _conns[db_path] = c
            _locks[db_path] = threading.Lock()
            return c
    except Exception:
        return None


def get(db_path: str, key: str, ttl: float = _DEFAULT_TTL) -> dict | None:
    """Return the cached payload for key if present and fresher than ttl."""
    try:
        c = _conn(db_path)
        if c is None:
            return None
        row = c.execute("SELECT v, ts FROM cache WHERE k = ?", (key,)).fetchone()
        if row is not None and (time.time() - row[1]) < ttl:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def put(db_path: str, key: str, value: dict) -> None:
    """Store payload for key. Never raises."""
    try:
        c = _conn(db_path)
        if c is None:
            return
        lock = _locks.get(db_path)
        if lock is None:
            return
        payload = json.dumps(value)
        with lock:
            c.execute(
                "INSERT OR REPLACE INTO cache (k, v, ts) VALUES (?, ?, ?)",
                (key, payload, time.time()),
            )
            c.commit()
    except Exception:
        pass
