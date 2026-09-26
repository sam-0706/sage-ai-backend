"""The single backend auth module (PRD: token verification in one place; authorization server-owned).

Every client (web, Electron, Flutter) sends `Authorization: Bearer <Clerk session token>`.
The token proves identity only. Role, mode and status always come from the SAGE database.
"""
import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, Request

from app.core.config import get_settings
from app.core.errors import Forbidden, Unauthorized
from app.core.logging import log
from app.integrations import clerk_api
from app.repositories import audit, flags
from app.repositories import users as users_repo
from app.repositories.db import transaction

logger = logging.getLogger("sage.auth")


@dataclass(frozen=True)
class Principal:
    id: UUID
    email: str
    role: str
    mode: str
    status: str
    full_name: str | None
    institution_id: UUID | None
    phone: str | None

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    @property
    def is_staff(self) -> bool:
        return self.role in ("faculty", "advisor", "superadmin")

    @classmethod
    def from_row(cls, u: dict) -> "Principal":
        return cls(id=u["id"], email=u["email"], role=u["role"], mode=u["mode"], status=u["status"],
                   full_name=u["full_name"], institution_id=u["institution_id"], phone=u["phone"])


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


async def _resolve_clerk_user(clerk_user_id: str) -> dict:
    """Find or bind the SAGE user for a verified Clerk identity. Idempotent under concurrent first requests."""
    async with transaction() as conn:
        user = await users_repo.get_by_clerk_id(conn, clerk_user_id)
        if user:
            return user

    clerk_user = await clerk_api.get_clerk_user(clerk_user_id)
    emails = clerk_api.verified_emails(clerk_user)
    if not emails:
        raise Forbidden("Your email address is not verified yet", code="email_unverified")
    full_name = " ".join(p for p in (clerk_user.get("first_name"), clerk_user.get("last_name")) if p) or None

    async with transaction() as conn:
        # lock-free idempotency: clerk_user_id is unique; re-check inside the transaction
        user = await users_repo.get_by_clerk_id(conn, clerk_user_id)
        if user:
            return user
        for email in emails:
            candidate = await users_repo.get_by_email(conn, email)
            if candidate:
                if candidate["clerk_user_id"] and candidate["clerk_user_id"] != clerk_user_id:
                    raise Forbidden("This email is already linked to another sign-in", code="identity_conflict")
                user = await users_repo.bind_clerk_identity(conn, candidate["id"], clerk_user_id, full_name)
                await audit.record(conn, "user.identity_bound", actor_id=user["id"], target_type="user",
                                   target_id=user["id"], metadata={"via": "waitlist_email"})
                return user
        if not await flags.is_enabled(conn, "open_signup"):
            raise Forbidden("SAGE AI is in an invite-only beta and this email is not on the waitlist",
                            code="not_on_waitlist", details={"emails": emails})
        user = await users_repo.create_open_signup(conn, emails[0], clerk_user_id, full_name)
        await conn.execute(
            """insert into subscriptions (user_id, plan_code, period_end, voice_seconds_allowance, ai_requests_allowance,
                                          chat_messages_allowance, autoapply_calls_allowance, source)
               select $1, code, now() + make_interval(days => period_days), (voice_minutes*60)::int, ai_requests, chat_messages, autoapply_calls, 'signup'
               from billing_plans where code = 'student_free' on conflict do nothing""", user["id"])
        await audit.record(conn, "user.signed_up", actor_id=user["id"], target_type="user", target_id=user["id"])
        return user


async def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_dev_user_email: str | None = Header(default=None),
) -> Principal:
    settings = get_settings()
    if settings.dev_auth_bypass and x_dev_user_email:
        async with transaction() as conn:
            user = await users_repo.get_by_email(conn, x_dev_user_email.lower())
        if not user:
            raise Unauthorized("Dev user not found")
        log(logger, logging.WARNING, "dev auth bypass used", email=x_dev_user_email)
    else:
        token = _bearer(authorization)
        if token.startswith("sds_"):  # SAGE device session (desktop / mobile)
            from app.services import device_auth
            user_id = await device_auth.user_for_session_token(token)
            async with transaction() as conn:
                user = await users_repo.get_by_id(conn, user_id)
            if not user:
                raise Unauthorized("Account not found")
        else:  # Clerk session JWT (web)
            claims = await asyncio.to_thread(clerk_api.verify_session_token, token)
            user = await _resolve_clerk_user(claims["sub"])

    if user["status"] in ("suspended", "deleted"):
        raise Forbidden("This account is not active", code="account_inactive")
    async with transaction() as conn:
        await users_repo.touch(conn, user["id"])
    principal = Principal.from_row(user)
    request.state.principal = principal
    return principal


def require_roles(*roles: str):
    async def _dep(p: Principal = Depends(get_principal)) -> Principal:
        if p.role not in roles:
            raise Forbidden("You do not have permission to perform this action")
        return p
    return _dep


require_staff = require_roles("faculty", "advisor", "superadmin")
require_superadmin = require_roles("superadmin")
