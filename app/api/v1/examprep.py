"""Exam prep: AI cue cards, spaced repetition, voice quiz calls and "where you stand" analysis."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.auth.deps import Principal, get_principal, require_superadmin
from app.core.errors import NotFound
from app.repositories import users as users_repo
from app.repositories.db import transaction
from app.services import calls as calls_svc
from app.services import examprep

router = APIRouter(prefix="/exam-prep", tags=["exam-prep"])


class DeckIn(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    level: str | None = Field(default=None, max_length=60)
    exam: str | None = Field(default=None, max_length=120)
    count: int = Field(default=12, ge=4, le=30)
    notes: str | None = Field(default=None, max_length=20000, description="Optional syllabus/notes to ground the cards")


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=4, description="1 again · 2 hard · 3 good · 4 easy")


class ExamCallIn(BaseModel):
    deck_id: UUID
    destination: str
    consent: bool
    consent_version: str = "v1"
    idempotency_key: str = Field(min_length=8, max_length=100)


class SimulateIn(BaseModel):
    deck_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)


async def _user(p: Principal) -> dict:
    async with transaction() as conn:
        return await users_repo.get_by_id(conn, p.id)


@router.get("/overview", summary="Decks, cards due, streak and recent quiz scores")
async def overview(p: Principal = Depends(get_principal)):
    return await examprep.overview(p.id)


@router.get("/decks")
async def list_decks(p: Principal = Depends(get_principal)):
    return {"items": await examprep.list_decks(p.id)}


@router.post("/decks", status_code=201, summary="Generate a cue-card deck for a topic (optionally grounded in your notes)")
async def create_deck(body: DeckIn, p: Principal = Depends(get_principal)):
    return await examprep.generate_deck(await _user(p), topic=body.topic, level=body.level, exam=body.exam,
                                        count=body.count, notes=body.notes)


@router.get("/decks/{deck_id}")
async def get_deck(deck_id: UUID, p: Principal = Depends(get_principal)):
    return await examprep.get_deck(p.id, deck_id)


@router.delete("/decks/{deck_id}")
async def delete_deck(deck_id: UUID, p: Principal = Depends(get_principal)):
    await examprep.delete_deck(p.id, deck_id)
    return {"deleted": True}


@router.get("/decks/{deck_id}/study", summary="Cards due now (new cards first)")
async def study(deck_id: UUID, limit: int = Query(20, ge=1, le=100), p: Principal = Depends(get_principal)):
    return {"items": await examprep.study_queue(p.id, deck_id, limit)}


@router.post("/cards/{card_id}/review", summary="Record a spaced-repetition review")
async def review(card_id: UUID, body: ReviewIn, p: Principal = Depends(get_principal)):
    return await examprep.review_card(p.id, card_id, body.rating)


@router.get("/calls/preflight", summary="Pre-call screen for a voice quiz on a deck")
async def call_preflight(deck_id: UUID, p: Principal = Depends(get_principal)):
    return await examprep.preflight(await _user(p), deck_id)


@router.post("/calls", status_code=201, summary="Start a consented voice quiz call (idempotent)")
async def create_call(body: ExamCallIn, p: Principal = Depends(get_principal)):
    call = await examprep.create_exam_call(await _user(p), deck_id=body.deck_id, destination=body.destination,
                                           consent=body.consent, consent_version=body.consent_version,
                                           idempotency_key=body.idempotency_key)
    return {**call, "destination": calls_svc.mask(call["destination"])}


@router.get("/calls/{call_id}", summary="Quiz call state; includes the analysis once ready")
async def get_call(call_id: UUID, p: Principal = Depends(get_principal)):
    call = await calls_svc.get_call(p.id, call_id)
    if call["purpose"] != "exam_prep":
        raise NotFound("Exam-prep call not found")
    assessment = None
    if call["extraction_status"] == "succeeded":
        assessment = await examprep.get_assessment(p.id, call_id=call_id)
    return {**call, "destination": "SIMULATED" if call["is_simulated"] else calls_svc.mask(call["destination"]),
            "assessment": assessment}


@router.post("/calls/{call_id}/analyze", summary="Run the analysis again")
async def analyze(call_id: UUID, p: Principal = Depends(get_principal)):
    await calls_svc.get_call(p.id, call_id, reconcile=False)  # ownership check
    return await examprep.run_assessment(call_id, force=True)


@router.post("/calls/simulate", status_code=201, summary="Superadmin QA: labelled simulated quiz (no phone call)")
async def simulate(body: SimulateIn, p: Principal = Depends(require_superadmin)):
    return await examprep.simulate_exam_call(await _user(p), body.deck_id, body.idempotency_key)


@router.get("/assessments")
async def list_assessments(deck_id: UUID | None = None, p: Principal = Depends(get_principal)):
    return {"items": await examprep.list_assessments(p.id, deck_id)}


@router.get("/assessments/{assessment_id}")
async def get_assessment(assessment_id: UUID, p: Principal = Depends(get_principal)):
    return await examprep.get_assessment(p.id, assessment_id=assessment_id)
