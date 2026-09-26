"""Schema-validated AI output contracts (PRD: Model Output Contract).

These are sent to OpenAI as strict JSON schemas, so every field is required (use `| None` for optional).
"""
from typing import Literal

from pydantic import BaseModel

RiskLevel = Literal["low", "medium", "high"]


class EvidenceItem(BaseModel):
    fact: str
    source: Literal["user", "demo", "system", "import", "transcript"]


class SelectedIssue(BaseModel):
    signal_ids: list[str]
    category: Literal["attendance", "deadline", "grades", "exam", "placement", "logistics", "planning",
                      "career", "learning", "venture", "other"]
    title: str
    reason: str
    evidence: list[EvidenceItem]
    confidence: float
    missing_info: list[str]


class NextItem(BaseModel):
    title: str
    reason: str


class InterventionBrief(BaseModel):
    call_purpose: str
    opening_question: str
    key_questions: list[str]
    hypotheses: list[str]
    avoid: list[str]


class PriorityOutput(BaseModel):
    selected: SelectedIssue
    next_items: list[NextItem]
    ordering_rationale: str
    intervention_brief: InterventionBrief


class ActionItem(BaseModel):
    action: str
    owner: str
    due_date: str | None  # ISO date YYYY-MM-DD


class Escalation(BaseModel):
    recommended: bool
    template_key: str | None
    reason: str
    questions_to_prepare: list[str]


class ExtractionOutput(BaseModel):
    """PRD minimum: issue, evidence, root cause, action, owner, due date, risk, confidence, escalation."""
    summary: str
    issue: str
    evidence: list[str]
    root_cause: str
    root_cause_category: Literal["schedule_conflict", "subject_difficulty", "health", "personal", "financial",
                                 "motivation", "workload", "information_gap", "other", "unknown"]
    actions: list[ActionItem]
    owner: str
    due_date: str | None
    risk: RiskLevel
    confidence: float
    escalation: Escalation
    transcript_gaps: list[str]
    crisis_detected: bool


class ChatSafety(BaseModel):
    crisis: bool
    category: Literal["none", "self_harm", "medical_emergency", "abuse", "other_urgent"]
