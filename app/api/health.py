"""Liveness vs readiness. Neither exposes secrets or provider responses."""
import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.repositories.db import connection

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live():
    return {"status": "ok"}


@router.get("/health/ready")
async def ready():
    s = get_settings()
    checks: dict[str, str] = {}
    try:
        async with asyncio.timeout(5):
            async with connection() as conn:
                await conn.fetchval("select 1")
                pending = await conn.fetchval("select count(*) from schema_migrations")
        checks["database"] = "ok"
        checks["migrations_applied"] = str(pending)
    except Exception:  # noqa: BLE001
        checks["database"] = "unavailable"
    checks["openai"] = "configured" if s.openai_api_key else "missing"
    checks["clerk"] = "configured" if s.clerk_secret_key else "missing"
    checks["voice"] = "configured" if (s.omnidim_api_key and s.omnidim_agent_id) else "not_configured"
    checks["payments"] = "test_mode" if s.razorpay_test_mode else "not_configured"
    ok = checks["database"] == "ok"
    return JSONResponse({"status": "ready" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)
