"""Seed the synthetic Demo University knowledge base and faculty/advisor directory (idempotent).

    python -m scripts.seed_knowledge
"""
import asyncio
import json
from pathlib import Path

from app.core.errors import AppError
from app.repositories.db import close_pool, transaction
from app.services import knowledge

SEEDS = Path(__file__).resolve().parent.parent / "seeds"
AUDIENCE = {"04_professional_founder_guides": ["professional", "founder", "student"]}
CATEGORY = {"01": "academic_policy", "02": "student_support", "03": "administration", "04": "guides"}


async def main() -> None:
    async with transaction() as conn:
        inst = await conn.fetchval("select id from institutions where slug = 'demo-university'")
    for path in sorted((SEEDS / "knowledge").glob("*.md")):
        text = path.read_text()
        title = text.splitlines()[0].lstrip("# ").strip()
        is_guide = path.stem.startswith("04")
        try:
            src = await knowledge.ingest(title=title, kind="text", text=text, category=CATEGORY[path.stem[:2]],
                                         audience=AUDIENCE.get(path.stem, ["student"]),
                                         institution_id=None if is_guide else inst, is_demo=not is_guide)
            print(f"indexed  {path.name}: {src['chunk_count']} chunks")
        except AppError as e:
            print(f"skipped  {path.name}: {e.code}")

    entries = json.loads((SEEDS / "directory.json").read_text())
    async with transaction() as conn:
        for e in entries:
            exists = await conn.fetchval("select 1 from faculty_directory where name = $1", e["name"])
            if exists:
                continue
            await conn.execute(
                """insert into faculty_directory (institution_id, name, kind, department, designation, subjects, expertise,
                                                  office_hours, contact_hint, bio, is_demo)
                   values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true)""",
                inst, e["name"], e["kind"], e["department"], e["designation"], e["subjects"], e["expertise"],
                e["office_hours"], e["contact_hint"], e["bio"])
        print("directory entries:", await conn.fetchval("select count(*) from faculty_directory"))
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
