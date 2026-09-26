import time

import asyncpg

_cache: tuple[float, dict[str, bool]] | None = None
TTL = 30.0


async def all_flags(conn: asyncpg.Connection, *, fresh: bool = False) -> dict[str, bool]:
    global _cache
    if not fresh and _cache and time.monotonic() - _cache[0] < TTL:
        return _cache[1]
    flags = {r["key"]: r["enabled"] for r in await conn.fetch("select key, enabled from feature_flags")}
    _cache = (time.monotonic(), flags)
    return flags


async def is_enabled(conn: asyncpg.Connection, key: str) -> bool:
    return (await all_flags(conn)).get(key, False)


def invalidate() -> None:
    global _cache
    _cache = None
