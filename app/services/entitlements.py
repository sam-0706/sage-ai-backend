"""Server-side plan entitlements and usage metering (PRD: Pricing Guardrails)."""
from typing import Literal
from uuid import UUID

import asyncpg

from app.core.errors import QuotaExceeded
from app.repositories.db import row

Meter = Literal["ai_requests", "chat_messages", "voice_seconds", "autoapply_calls"]


async def active_subscription(conn: asyncpg.Connection, user_id: UUID) -> dict | None:
    return row(await conn.fetchrow(
        """select s.*, s.status::text as status, p.name as plan_name, p.features, p.voice_minutes
           from subscriptions s join billing_plans p on p.code = s.plan_code
           where s.user_id = $1 and s.status = 'active' and (s.period_end is null or s.period_end > now())""", user_id))


def summarize(sub: dict | None) -> dict | None:
    if not sub:
        return None
    return {
        "plan_code": sub["plan_code"], "plan_name": sub["plan_name"], "status": sub["status"], "is_test": sub["is_test"],
        "period_start": sub["period_start"], "period_end": sub["period_end"], "features": sub["features"],
        "voice": {"allowance_seconds": sub["voice_seconds_allowance"], "used_seconds": sub["voice_seconds_used"],
                  "remaining_seconds": max(0, sub["voice_seconds_allowance"] - sub["voice_seconds_used"])},
        "ai_requests": {"allowance": sub["ai_requests_allowance"], "used": sub["ai_requests_used"],
                        "remaining": max(0, sub["ai_requests_allowance"] - sub["ai_requests_used"])},
        "chat_messages": {"allowance": sub["chat_messages_allowance"], "used": sub["chat_messages_used"],
                          "remaining": max(0, sub["chat_messages_allowance"] - sub["chat_messages_used"])},
        "autoapply_calls": {"allowance": sub["autoapply_calls_allowance"], "used": sub["autoapply_calls_used"],
                            "remaining": max(0, sub["autoapply_calls_allowance"] - sub["autoapply_calls_used"])},
    }


async def consume(conn: asyncpg.Connection, user_id: UUID, meter: Meter, amount: int = 1, *, allow_overdraw: bool = False) -> None:
    """Atomically consume `amount` from the active subscription; raises QuotaExceeded when exhausted."""
    used, allowance = f"{meter}_used", f"{meter}_allowance"
    guard = "" if allow_overdraw else f"and {used} + $2 <= {allowance}"
    updated = await conn.fetchval(
        f"""update subscriptions set {used} = {used} + $2
            where user_id = $1 and status = 'active' and (period_end is null or period_end > now()) {guard}
            returning id""", user_id, amount)
    if updated is None:
        raise QuotaExceeded(f"Your plan's {meter.replace('_', ' ')} allowance is used up",
                            details={"meter": meter})


async def remaining(conn: asyncpg.Connection, user_id: UUID, meter: Meter) -> int:
    sub = await active_subscription(conn, user_id)
    if not sub:
        return 0
    return max(0, sub[f"{meter}_allowance"] - sub[f"{meter}_used"])
