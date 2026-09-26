"""asyncpg pool tuned for serverless + Supabase's transaction pooler (Supavisor, port 6543).

- statement_cache_size=0: prepared statements are not supported through a transaction pooler.
- Small pool, lazily created per warm instance; Vercel Fluid compute reuses instances across requests.
"""
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from app.core.config import get_settings

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()


async def _init_conn(conn: asyncpg.Connection) -> None:
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                s = get_settings()
                _pool = await asyncpg.create_pool(
                    s.database_url,
                    min_size=s.db_pool_min,
                    max_size=s.db_pool_max,
                    statement_cache_size=0,
                    command_timeout=s.db_command_timeout,
                    max_inactive_connection_lifetime=60,
                    init=_init_conn,
                )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """Explicit transaction boundary (PRD: repositories with explicit transaction boundaries)."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        yield conn


def row(r: asyncpg.Record | None) -> dict | None:
    return dict(r) if r is not None else None


def rows(rs: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in rs]
