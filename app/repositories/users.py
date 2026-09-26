from uuid import UUID

import asyncpg

from app.repositories.db import row, rows

USER_COLS = """id, clerk_user_id, email::text as email, full_name, phone, whatsapp, linkedin_url, mode::text as mode,
               role::text as role, status::text as status, institution_id, waitlist_segment, is_demo,
               onboarded_at, last_seen_at, created_at, updated_at"""


async def get_by_id(conn: asyncpg.Connection, user_id: UUID) -> dict | None:
    return row(await conn.fetchrow(f"select {USER_COLS} from users where id = $1", user_id))


async def get_by_clerk_id(conn: asyncpg.Connection, clerk_user_id: str) -> dict | None:
    return row(await conn.fetchrow(f"select {USER_COLS} from users where clerk_user_id = $1", clerk_user_id))


async def get_by_email(conn: asyncpg.Connection, email: str) -> dict | None:
    return row(await conn.fetchrow(f"select {USER_COLS} from users where email = $1", email))


async def bind_clerk_identity(conn: asyncpg.Connection, user_id: UUID, clerk_user_id: str, full_name: str | None) -> dict:
    return row(await conn.fetchrow(
        f"""update users set clerk_user_id = $2, status = case when status = 'invited' then 'active'::user_status else status end,
                    full_name = coalesce(full_name, $3), last_seen_at = now()
            where id = $1 returning {USER_COLS}""", user_id, clerk_user_id, full_name))


async def create_open_signup(conn: asyncpg.Connection, email: str, clerk_user_id: str, full_name: str | None) -> dict:
    return row(await conn.fetchrow(
        f"""insert into users (email, clerk_user_id, full_name, status) values ($1, $2, $3, 'active')
            on conflict (email) do update set clerk_user_id = excluded.clerk_user_id
            returning {USER_COLS}""", email, clerk_user_id, full_name))


async def touch(conn: asyncpg.Connection, user_id: UUID) -> None:
    await conn.execute("update users set last_seen_at = now() where id = $1 and (last_seen_at is null or last_seen_at < now() - interval '5 minutes')", user_id)


async def update_self(conn: asyncpg.Connection, user_id: UUID, fields: dict) -> dict:
    allowed = {"full_name", "phone", "whatsapp", "linkedin_url", "mode"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return await get_by_id(conn, user_id)
    sets = ", ".join(f"{k} = ${i + 2}" + ("::experience_mode" if k == "mode" else "") for i, k in enumerate(fields))
    return row(await conn.fetchrow(
        f"update users set {sets}, onboarded_at = coalesce(onboarded_at, now()) where id = $1 returning {USER_COLS}",
        user_id, *fields.values()))


async def list_users(conn: asyncpg.Connection, *, q: str | None, role: str | None, mode: str | None,
                     status: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    where, args = ["is_demo = false"], []
    if q:
        args.append(f"%{q}%")
        where.append(f"(email::text ilike ${len(args)} or full_name ilike ${len(args)})")
    for col, val, cast in (("role", role, "access_role"), ("mode", mode, "experience_mode"), ("status", status, "user_status")):
        if val:
            args.append(val)
            where.append(f"{col} = ${len(args)}::{cast}")
    w = " and ".join(where)
    total = await conn.fetchval(f"select count(*) from users where {w}", *args)
    args += [limit, offset]
    data = rows(await conn.fetch(
        f"select {USER_COLS} from users where {w} order by created_at desc limit ${len(args) - 1} offset ${len(args)}", *args))
    return data, total


async def set_role(conn: asyncpg.Connection, user_id: UUID, role: str) -> dict:
    return row(await conn.fetchrow(f"update users set role = $2::access_role where id = $1 returning {USER_COLS}", user_id, role))


async def set_status(conn: asyncpg.Connection, user_id: UUID, status: str) -> dict:
    return row(await conn.fetchrow(f"update users set status = $2::user_status where id = $1 returning {USER_COLS}", user_id, status))
