"""Apply version-controlled SQL migrations in supabase/migrations, in order, exactly once.

Usage:  python -m scripts.migrate            # apply pending
        python -m scripts.migrate --status   # list applied / pending
"""
import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

from app.core.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "supabase" / "migrations"


async def main(status_only: bool) -> None:
    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url, statement_cache_size=0)
    try:
        await conn.execute(
            """create table if not exists schema_migrations (
                 version text primary key, checksum text not null, applied_at timestamptz not null default now())"""
        )
        await conn.execute("alter table schema_migrations enable row level security")
        applied = {r["version"]: r["checksum"] for r in await conn.fetch("select version, checksum from schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            version = path.stem
            if version in applied:
                flag = "" if applied[version] == checksum else "  (WARNING: file changed after apply)"
                print(f"applied   {version}{flag}")
                continue
            if status_only:
                print(f"pending   {version}")
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("insert into schema_migrations(version, checksum) values ($1, $2)", version, checksum)
            print(f"APPLIED   {version}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main("--status" in sys.argv))
