"""Pre-onboard the waitlist export into SAGE AI.

Each row becomes an `invited` user with an experience mode, a goal profile stub, and a Waitlist Beta
subscription. On first Clerk sign-in with the same verified email, the account is bound and activated.
Only waitlisted emails can sign in while the `open_signup` flag is off.

The CSV contains personal data — keep it out of version control. Pass its path explicitly:

    python -m scripts.seed_waitlist /path/to/bitsom-waitlist.csv [--dry-run]

Idempotent: re-running updates existing rows by email and never duplicates users.
"""
import asyncio
import csv
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from scripts._db import connect

IST = timezone(timedelta(hours=5, minutes=30))

SEGMENT_TO_MODE = {
    "student": "student",
    "bitsom student": "student",
    "aspirant": "student",
    "working professional": "professional",
    "founder": "founder",
    "founder (funded)": "founder",
}


def clean_phone(raw: str) -> str | None:
    # Export wraps values as ="+91 9xxxxxxxxx" to stop spreadsheets reformatting them.
    v = raw.strip().removeprefix("=").strip('"').strip()
    digits = re.sub(r"[^\d+]", "", v)
    return digits or None


def parse_joined(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y, %H:%M").replace(tzinfo=IST)
    except ValueError:
        return None


def load_rows(path: str) -> list[dict]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            email = (r.get("Email") or "").strip().lower()
            if not email:
                continue
            segment = (r.get("Role") or "").strip()
            phone = clean_phone(r.get("Phone") or "")
            wa_same = (r.get("WhatsApp same as phone") or "").strip().lower() == "yes"
            out.append({
                "email": email,
                "full_name": (r.get("Name") or "").strip() or None,
                "phone": phone,
                "whatsapp": phone if wa_same else clean_phone(r.get("WhatsApp") or ""),
                "linkedin_url": (r.get("LinkedIn") or "").strip() or None,
                "segment": segment,
                "mode": SEGMENT_TO_MODE.get(segment.lower(), "student"),
                "source": (r.get("Source") or "").strip() or None,
                "joined_at": parse_joined(r.get("Joined (IST)") or ""),
            })
    return out


async def main(path: str, dry_run: bool) -> None:
    settings = get_settings()
    data = load_rows(path)
    emails = [d["email"] for d in data]
    dupes = [e for e, n in Counter(emails).items() if n > 1]
    print(f"rows: {len(data)}  unique emails: {len(set(emails))}  duplicate emails: {dupes or 'none'}")
    print("modes:", dict(Counter(d["mode"] for d in data)))
    print("segments:", dict(Counter(d["segment"] for d in data)))
    if dry_run:
        return

    superadmins = {e.lower() for e in settings.superadmin_emails}
    conn = await connect()
    try:
        async with conn.transaction():
            bitsom_id = await conn.fetchval(
                """insert into institutions (slug, name, is_demo) values ('bitsom', 'BITS School of Management (BITSOM)', false)
                   on conflict (slug) do update set name = excluded.name returning id""")
            plan = await conn.fetchrow("select * from billing_plans where code = 'waitlist_beta'")
            created = updated = 0
            for d in data:
                role = "superadmin" if d["email"] in superadmins else "member"
                inst = bitsom_id if d["segment"].lower() == "bitsom student" else None
                rec = await conn.fetchrow(
                    """insert into users (email, full_name, phone, whatsapp, linkedin_url, mode, role, status,
                                          institution_id, waitlist_segment, waitlist_source, waitlist_joined_at, onboarded_at)
                       values ($1,$2,$3,$4,$5,$6::experience_mode,$7::access_role,'invited',$8,$9,$10,$11, now())
                       on conflict (email) do update set
                         full_name = coalesce(users.full_name, excluded.full_name),
                         phone = coalesce(users.phone, excluded.phone),
                         whatsapp = coalesce(users.whatsapp, excluded.whatsapp),
                         linkedin_url = coalesce(users.linkedin_url, excluded.linkedin_url),
                         waitlist_segment = excluded.waitlist_segment,
                         waitlist_source = excluded.waitlist_source,
                         waitlist_joined_at = excluded.waitlist_joined_at,
                         institution_id = coalesce(users.institution_id, excluded.institution_id),
                         role = case when excluded.role = 'superadmin' then 'superadmin'::access_role else users.role end
                       returning id, (xmax = 0) as inserted""",
                    d["email"], d["full_name"], d["phone"], d["whatsapp"], d["linkedin_url"], d["mode"], role, inst,
                    d["segment"], d["source"], d["joined_at"])
                created += rec["inserted"]
                updated += not rec["inserted"]
                await conn.execute(
                    """insert into goal_profiles (user_id, mode, data, field_sources)
                       values ($1, $2::experience_mode, $3, $4) on conflict (user_id) do nothing""",
                    rec["id"], d["mode"], {"segment": d["segment"]}, {"segment": "import"})
                await conn.execute(
                    """insert into subscriptions (user_id, plan_code, period_end, voice_seconds_allowance,
                                                  ai_requests_allowance, chat_messages_allowance, autoapply_calls_allowance, is_test, source)
                       select $1, code, now() + make_interval(days => period_days), (voice_minutes * 60)::int,
                              ai_requests, chat_messages, autoapply_calls, true, 'waitlist_import'
                       from billing_plans where code = 'waitlist_beta'
                       on conflict do nothing""", rec["id"])
            await conn.execute(
                """insert into audit_events (action, target_type, metadata) values ('waitlist.imported', 'users', $1)""",
                {"rows": len(data), "created": created, "updated": updated, "plan": plan["code"]})
        total = await conn.fetchval("select count(*) from users where is_demo = false")
        print(f"created: {created}  updated: {updated}  total real users in platform: {total}")
    finally:
        await conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], "--dry-run" in sys.argv))
