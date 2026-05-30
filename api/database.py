"""
AWX Analytics Portal – Database helpers
ThreadedConnectionPool + async-friendly fetch wrappers.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator, List, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

log = logging.getLogger("awx_api.db")

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

# ──────────────────────────────────────────────────────────
# Pool lifecycle
# ──────────────────────────────────────────────────────────
def init_pool(cfg: dict) -> None:
    global _pool
    db = cfg["database"]
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=db.get("pool_min", 2),
        maxconn=db.get("pool_max", 10),
        host=db["host"],
        port=db.get("port", 5432),
        dbname=db["name"],
        user=db["user"],
        password=db["password"],
        connect_timeout=db.get("connect_timeout", 10),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    log.info("DB pool initialised (min=%d, max=%d)", db.get("pool_min",2), db.get("pool_max",10))

def close_pool() -> None:
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        log.info("DB pool closed")

@contextmanager
def get_conn() -> Generator:
    assert _pool is not None, "Database pool not initialised"
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)

# ──────────────────────────────────────────────────────────
# Fetch helpers (run sync inside thread pool via FastAPI)
# ──────────────────────────────────────────────────────────
def fetch_all(sql: str, params: tuple = ()) -> List[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

def fetch_one(sql: str, params: tuple = ()) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

def fetch_scalar(sql: str, params: tuple = ()) -> Any:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return list(row.values())[0] if row else None
