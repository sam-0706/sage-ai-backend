"""Goal profile and signals (PRD Epic 2 / 2B). Every value carries its provenance: user, demo, system, import."""
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from app.core.errors import NotFound
from app.repositories import audit
from app.repositories.db import row, rows

MODE_FIELDS = {
    "student": ["program", "semester", "subjects", "career_goal", "availability", "institution_name", "notes", "specialisation", "batch", "section", "daily_minutes", "salary_lpa", "target_role", "preferred_locations", "skills", "experience_summary", "graduation_year", "internships_completed", "resume_summary", "onboarding_version"],
    "professional": ["target_role", "skill_goals", "courses", "weekly_availability_hours", "interview_dates",
                     "portfolio_gaps", "current_role", "notes"],
    "founder": ["venture_name", "venture_stage", "current_milestone", "customer_questions", "experiments",
                "important_dates", "decisions", "notes"],
}
REQUIRED_FOR_CONFIDENCE = {
    "student": ["program", "semester", "subjects", "career_goal"],
    "professional": ["target_role", "skill_goals", "weekly_availability_hours"],
    "founder": ["venture_stage", "current_milestone"],
}

SIGNAL_COLS = """id, type, label, subject, value_num, value_text, threshold, unit, due_at, observed_at,
                 source::text as source, status, dispute_note, metadata, created_at, updated_at"""


async def get_profile(conn: asyncpg.Connection, user_id: UUID, mode: str) -> dict:
    p = row(await conn.fetchrow(
        "select user_id, mode::text as mode, data, field_sources, availability, consent, demo_profile_key, updated_at "
        "from goal_profiles where user_id = $1", user_id))
    if not p:
        p = {"user_id": user_id, "mode": mode, "data": {}, "field_sources": {}, "availability": {}, "consent": {},
             "demo_profile_key": None, "updated_at": None}
    p["fields"] = {k: {"value": v, "source": p["field_sources"].get(k, "user")} for k, v in p["data"].items()}
    missing = [f for f in REQUIRED_FOR_CONFIDENCE.get(p["mode"], []) if not p["data"].get(f)]
    p["missing_fields"] = missing
    p["completeness"] = round(1 - len(missing) / max(1, len(REQUIRED_FOR_CONFIDENCE.get(p["mode"], []))), 2)
    p["allowed_fields"] = MODE_FIELDS.get(p["mode"], [])
    return p


async def upsert_profile(conn: asyncpg.Connection, user_id: UUID, mode: str, data: dict, availability: dict | None,
                         consent: dict | None) -> dict:
    existing = await get_profile(conn, user_id, mode)
    allowed = set(MODE_FIELDS[mode])
    clean = {k: v for k, v in data.items() if k in allowed}
    merged = {**(existing["data"] if existing["mode"] == mode else {}), **clean}
    sources = {**(existing["field_sources"] if existing["mode"] == mode else {}), **{k: "user" for k in clean}}
    await conn.execute(
        """insert into goal_profiles (user_id, mode, data, field_sources, availability, consent)
           values ($1, $2::experience_mode, $3, $4, coalesce($5, '{}'::jsonb), coalesce($6, '{}'::jsonb))
           on conflict (user_id) do update set mode = excluded.mode, data = excluded.data, field_sources = excluded.field_sources,
             availability = coalesce($5, goal_profiles.availability), consent = coalesce($6, goal_profiles.consent)""",
        user_id, mode, merged, sources, availability, consent)
    await conn.execute("update users set mode = $2::experience_mode, onboarded_at = coalesce(onboarded_at, now()) where id = $1",
                       user_id, mode)
    return await get_profile(conn, user_id, mode)


async def list_demo_profiles(conn: asyncpg.Connection, mode: str | None) -> list[dict]:
    q = "select key, mode::text as mode, display_name, summary, profile, signals, label from demo_profiles"
    return rows(await conn.fetch(q + " where mode = $1::experience_mode order by key", mode) if mode
                else await conn.fetch(q + " order by mode, key"))


async def load_demo_profile(conn: asyncpg.Connection, user_id: UUID, key: str) -> dict:
    """Replace the user's profile and active signals with a labelled synthetic profile."""
    demo = row(await conn.fetchrow("select *, mode::text as mode_t from demo_profiles where key = $1", key))
    if not demo:
        raise NotFound("Demo profile not found")
    mode = demo["mode_t"]
    await conn.execute(
        """insert into goal_profiles (user_id, mode, data, field_sources, demo_profile_key)
           values ($1, $2::experience_mode, $3, $4, $5)
           on conflict (user_id) do update set mode = excluded.mode, data = excluded.data,
             field_sources = excluded.field_sources, demo_profile_key = excluded.demo_profile_key""",
        user_id, mode, demo["profile"], {k: "demo" for k in demo["profile"]}, key)
    await conn.execute("update users set mode = $2::experience_mode where id = $1", user_id, mode)
    await conn.execute("update signals set status = 'dismissed', dispute_note = 'replaced by demo profile load' "
                       "where user_id = $1 and status = 'active'", user_id)
    now = datetime.now(UTC)
    for s in demo["signals"]:
        due = now + timedelta(days=s["due_in_days"]) if s.get("due_in_days") is not None else None
        await conn.execute(
            """insert into signals (user_id, type, label, subject, value_num, value_text, threshold, unit, due_at, source, metadata)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,'demo',$10)""",
            user_id, s["type"], s["label"], s.get("subject"), s.get("value_num"), s.get("value_text"),
            s.get("threshold"), s.get("unit"), due, {"demo_profile": key})
    await audit.record(conn, "profile.demo_loaded", actor_id=user_id, target_type="demo_profile", target_id=key)
    return await get_profile(conn, user_id, mode)


async def list_signals(conn: asyncpg.Connection, user_id: UUID, include_inactive: bool = False) -> list[dict]:
    where = "" if include_inactive else "and status = 'active'"
    return rows(await conn.fetch(f"select {SIGNAL_COLS} from signals where user_id = $1 {where} order by created_at", user_id))


async def create_signal(conn: asyncpg.Connection, user_id: UUID, s: dict) -> dict:
    return row(await conn.fetchrow(
        f"""insert into signals (user_id, type, label, subject, value_num, value_text, threshold, unit, due_at, source, metadata)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,'user',$10) returning {SIGNAL_COLS}""",
        user_id, s["type"], s["label"], s.get("subject"), s.get("value_num"), s.get("value_text"), s.get("threshold"),
        s.get("unit"), s.get("due_at"), s.get("metadata") or {}))


async def update_signal(conn: asyncpg.Connection, user_id: UUID, signal_id: UUID, patch: dict) -> dict:
    current = row(await conn.fetchrow(f"select {SIGNAL_COLS} from signals where id = $1 and user_id = $2", signal_id, user_id))
    if not current:
        raise NotFound("Signal not found")
    fields = {k: v for k, v in patch.items() if k in {"label", "subject", "value_num", "value_text", "threshold", "unit",
                                                      "due_at", "status", "dispute_note"}}
    if not fields:
        return current
    value_changed = any(k in fields for k in ("value_num", "value_text", "threshold", "due_at"))
    sets = [f"{k} = ${i + 3}" for i, k in enumerate(fields)]
    if value_changed:
        sets.append("source = 'user'")  # a corrected value is now user-entered
    updated = row(await conn.fetchrow(
        f"update signals set {', '.join(sets)} where id = $1 and user_id = $2 returning {SIGNAL_COLS}",
        signal_id, user_id, *fields.values()))
    if value_changed or fields.get("status") == "dismissed" or "dispute_note" in fields:
        # PRD edge case: student disputes the data → record the source discrepancy
        await audit.record(conn, "signal.corrected", actor_id=user_id, target_type="signal", target_id=signal_id,
                           metadata={"before": {k: current.get(k) for k in fields}, "after": fields,
                                     "original_source": current["source"]})
    return updated
