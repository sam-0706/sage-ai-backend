"""Scheduled jobs. Vercel Cron sends `Authorization: Bearer $CRON_SECRET`."""
import hmac

from fastapi import APIRouter, Header

from app.core.config import get_settings
from app.core.errors import Unauthorized
from app.workers.jobs import run_maintenance

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


def _check(authorization: str | None) -> None:
    secret = get_settings().cron_secret
    if not secret or not authorization or not hmac.compare_digest(authorization, f"Bearer {secret}"):
        raise Unauthorized("Invalid cron credentials")


@router.get("/cron/maintenance")
async def maintenance(authorization: str | None = Header(default=None)):
    _check(authorization)
    return await run_maintenance()
