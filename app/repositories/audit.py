from uuid import UUID

import asyncpg

from app.core.context import get_correlation_id


async def record(conn: asyncpg.Connection, action: str, *, actor_id: UUID | None = None, target_type: str | None = None,
                 target_id: str | UUID | None = None, metadata: dict | None = None) -> None:
    await conn.execute(
        """insert into audit_events (actor_id, action, target_type, target_id, metadata, correlation_id)
           values ($1, $2, $3, $4, $5, $6)""",
        actor_id, action, target_type, str(target_id) if target_id else None, metadata or {}, get_correlation_id())
