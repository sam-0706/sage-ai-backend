"""Device sign-in for desktop/mobile clients (RFC 8628-style).

1. Client → POST /v1/auth/device/start → {device_code (secret), user_code, verification_url}
2. User opens verification_url in the system browser, signs in with Clerk, confirms the same user_code, approves.
3. Client polls POST /v1/auth/device/token with device_code → receives an opaque session token (once).

Session tokens (`sds_…`) are random, stored only as SHA-256 hashes, revocable, and expire after 30 days.
Authorization still comes from the SAGE user row — the token only proves identity.
"""
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import get_settings
from app.core.errors import AppError, NotFound, Unauthorized
from app.repositories import audit
from app.repositories.db import row, transaction

DEVICE_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(days=30)
POLL_INTERVAL = 3
_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"  # no vowels / look-alikes


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _user_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


async def start(client: str, device_name: str | None) -> dict:
    device_code = secrets.token_urlsafe(32)
    async with transaction() as conn:
        for _ in range(5):
            code = _user_code()
            inserted = await conn.fetchval(
                """insert into device_auth_requests (device_code_hash, user_code, client, device_name, expires_at)
                   values ($1,$2,$3,$4,$5) on conflict (user_code) do nothing returning id""",
                _hash(device_code), code, client, (device_name or "")[:80] or None, datetime.now(UTC) + DEVICE_TTL)
            if inserted:
                break
        else:
            raise AppError("Could not allocate a sign-in code, please retry", code="device_code_unavailable", status_code=503)
    base = get_settings().public_base_url.rstrip("/")
    return {"device_code": device_code, "user_code": code, "verification_url": f"{base}/auth/device?code={code}",
            "expires_in": int(DEVICE_TTL.total_seconds()), "interval": POLL_INTERVAL}


async def describe(user_code: str) -> dict:
    async with transaction() as conn:
        r = row(await conn.fetchrow("select user_code, client, device_name, status, expires_at from device_auth_requests "
                                    "where user_code = $1", user_code.upper().strip()))
    if not r:
        raise NotFound("Unknown sign-in code")
    if r["status"] == "pending" and r["expires_at"] < datetime.now(UTC):
        r["status"] = "expired"
    return r


async def approve(user_code: str, user_id: UUID, approve_: bool = True) -> dict:
    async with transaction() as conn:
        r = row(await conn.fetchrow("select * from device_auth_requests where user_code = $1 for update", user_code.upper().strip()))
        if not r:
            raise NotFound("Unknown sign-in code")
        if r["status"] != "pending" or r["expires_at"] < datetime.now(UTC):
            raise AppError("This sign-in code has expired or was already used", code="device_code_invalid", status_code=410)
        status = "approved" if approve_ else "denied"
        await conn.execute("update device_auth_requests set status = $2, user_id = $3, approved_at = now() where id = $1",
                           r["id"], status, user_id)
        await audit.record(conn, f"auth.device_{status}", actor_id=user_id, target_type="device_auth", target_id=r["id"],
                           metadata={"client": r["client"], "device_name": r["device_name"]})
    return {"status": status, "client": r["client"], "device_name": r["device_name"]}


async def exchange(device_code: str) -> dict:
    async with transaction() as conn:
        r = row(await conn.fetchrow("select * from device_auth_requests where device_code_hash = $1 for update", _hash(device_code)))
        if not r:
            raise Unauthorized("Unknown device code", code="invalid_device_code")
        if r["status"] == "pending":
            if r["expires_at"] < datetime.now(UTC):
                await conn.execute("update device_auth_requests set status = 'expired' where id = $1", r["id"])
                return {"status": "expired"}
            return {"status": "pending", "interval": POLL_INTERVAL}
        if r["status"] in ("denied", "expired", "consumed"):
            return {"status": r["status"]}
        # approved → issue the session exactly once
        token = "sds_" + secrets.token_urlsafe(36)
        expires = datetime.now(UTC) + SESSION_TTL
        await conn.execute(
            "insert into app_sessions (token_hash, user_id, client, device_name, expires_at) values ($1,$2,$3,$4,$5)",
            _hash(token), r["user_id"], r["client"], r["device_name"], expires)
        await conn.execute("update device_auth_requests set status = 'consumed' where id = $1", r["id"])
        await audit.record(conn, "auth.session_issued", actor_id=r["user_id"], target_type="device_auth", target_id=r["id"],
                           metadata={"client": r["client"]})
    return {"status": "approved", "access_token": token, "token_type": "Bearer", "expires_at": expires.isoformat()}


async def user_for_session_token(token: str) -> UUID:
    async with transaction() as conn:
        r = await conn.fetchrow(
            """update app_sessions set last_used_at = now()
               where token_hash = $1 and revoked_at is null and expires_at > now()
               returning user_id""", _hash(token))
    if not r:
        raise Unauthorized("Session expired or revoked — please sign in again", code="session_invalid")
    return r["user_id"]


async def revoke(token: str) -> None:
    async with transaction() as conn:
        await conn.execute("update app_sessions set revoked_at = now() where token_hash = $1 and revoked_at is null", _hash(token))
