import asyncpg

from app.core.config import get_settings
from app.repositories.db import _init_conn


async def connect() -> asyncpg.Connection:
    conn = await asyncpg.connect(get_settings().database_url, statement_cache_size=0)
    await _init_conn(conn)
    return conn
