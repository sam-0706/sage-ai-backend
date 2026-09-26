"""Device sign-in for desktop/mobile + session management."""
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from app.auth.deps import Principal, get_principal
from app.core.errors import NotFound
from app.repositories.db import rows, transaction
from app.services import device_auth

router = APIRouter(prefix="/auth", tags=["auth"])


class DeviceStartIn(BaseModel):
    client: str = Field(pattern="^(desktop|mobile|cli)$")
    device_name: str | None = Field(default=None, max_length=80)


class DeviceCodeIn(BaseModel):
    user_code: str = Field(min_length=9, max_length=9)


class GoogleCompleteIn(BaseModel):
    ticket: str = Field(min_length=70, max_length=200)


@router.post("/google/complete")
async def google_complete(body: GoogleCompleteIn, p: Principal = Depends(get_principal)):
    code = device_auth.verify_browser_ticket(body.ticket)
    return await device_auth.approve(code, p.id, True)


class DeviceTokenIn(BaseModel):
    device_code: str = Field(min_length=20, max_length=200)


@router.post("/device/start", summary="Start device sign-in (public). Open verification_url in the system browser.")
async def device_start(body: DeviceStartIn):
    return await device_auth.start(body.client, body.device_name)


@router.post("/device/token", summary="Poll for the session token (public). status: pending | approved | denied | expired | consumed")
async def device_token(body: DeviceTokenIn):
    return await device_auth.exchange(body.device_code)


@router.get("/device/{user_code}", summary="Describe a pending sign-in request (public)")
async def device_describe(user_code: str):
    r = await device_auth.describe(user_code)
    return {"user_code": r["user_code"], "client": r["client"], "device_name": r["device_name"], "status": r["status"]}


@router.post("/device/approve", summary="Approve a device sign-in with the signed-in web session (Clerk)")
async def device_approve(body: DeviceCodeIn, p: Principal = Depends(get_principal)):
    return await device_auth.approve(body.user_code, p.id, True) | {"email": p.email}


@router.post("/device/deny")
async def device_deny(body: DeviceCodeIn, p: Principal = Depends(get_principal)):
    return await device_auth.approve(body.user_code, p.id, False)


@router.post("/logout", summary="Revoke the current device session")
async def logout(authorization: str | None = Header(default=None), _: Principal = Depends(get_principal)):
    token = (authorization or "").split(" ", 1)[-1]
    if token.startswith("sds_"):
        await device_auth.revoke(token)
    return {"signed_out": True}


@router.get("/sessions", summary="My signed-in devices")
async def sessions(p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"items": rows(await conn.fetch(
            """select id, client, device_name, created_at, last_used_at, expires_at from app_sessions
               where user_id = $1 and revoked_at is null and expires_at > now() order by created_at desc""", p.id))}


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        n = await conn.execute("update app_sessions set revoked_at = now() where id = $1 and user_id = $2 and revoked_at is null",
                               session_id, p.id)
    if n.endswith(" 0"):
        raise NotFound("Session not found")
    return {"revoked": True}
