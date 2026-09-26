"""Request schemas for the versioned REST API. Responses are JSON objects documented in OpenAPI."""
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

Mode = Literal["student", "professional", "founder"]
Role = Literal["member", "faculty", "advisor", "superadmin"]


class MeUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    whatsapp: str | None = Field(default=None, max_length=20)
    linkedin_url: str | None = Field(default=None, max_length=300)
    mode: Mode | None = None


class RoleRequestIn(BaseModel):
    requested_role: Literal["faculty", "advisor"]
    reason: str = Field(max_length=1000)


class ProfileIn(BaseModel):
    mode: Mode
    data: dict[str, Any] = Field(default_factory=dict)
    availability: dict[str, Any] | None = None
    consent: dict[str, Any] | None = None


class LoadDemoIn(BaseModel):
    key: str


class SignalIn(BaseModel):
    type: str = Field(max_length=40)
    label: str = Field(max_length=200)
    subject: str | None = Field(default=None, max_length=120)
    value_num: float | None = None
    value_text: str | None = Field(default=None, max_length=500)
    threshold: float | None = None
    unit: str | None = Field(default=None, max_length=20)
    due_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class SignalPatch(BaseModel):
    label: str | None = None
    subject: str | None = None
    value_num: float | None = None
    value_text: str | None = None
    threshold: float | None = None
    unit: str | None = None
    due_at: datetime | None = None
    status: Literal["active", "dismissed", "resolved"] | None = None
    dispute_note: str | None = Field(default=None, max_length=1000)


class CallCreate(BaseModel):
    intervention_id: UUID
    destination: str
    consent: bool
    consent_version: str = "v1"
    idempotency_key: str = Field(min_length=8, max_length=100)


class SimulateCallIn(BaseModel):
    intervention_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)


class ActionItemIn(BaseModel):
    action: str = Field(max_length=500)
    owner: str = Field(default="me", max_length=120)
    due_date: date | None = None
    done: bool = False


class PlanPatch(BaseModel):
    summary: str | None = Field(default=None, max_length=4000)
    issue: str | None = Field(default=None, max_length=1000)
    root_cause: str | None = Field(default=None, max_length=2000)
    actions: list[ActionItemIn] | None = None
    owner: str | None = None
    due_date: date | None = None
    risk_level: Literal["low", "medium", "high"] | None = None
    advisor_template_key: str | None = None
    follow_up_at: datetime | None = None


class PlanCreate(BaseModel):
    summary: str = Field(max_length=4000)
    intervention_id: UUID | None = None
    issue: str | None = None
    root_cause: str | None = None
    actions: list[ActionItemIn] = Field(default_factory=list)
    owner: str | None = None
    due_date: date | None = None
    risk_level: Literal["low", "medium", "high"] | None = None
    advisor_template_key: str | None = None


class ShareIn(BaseModel):
    include_transcript: bool = False
    confirm: bool = Field(description="Must be true: sharing is an explicit student action")


class CaseNoteIn(BaseModel):
    kind: Literal["note", "status_change", "follow_up", "resource"] = "note"
    body: str = Field(min_length=1, max_length=4000)
    status: Literal["open", "in_progress", "follow_up", "resolved"] | None = None
    follow_up_at: datetime | None = None


class ChatSessionIn(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class ChatMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    stream: bool = True


class OrderIn(BaseModel):
    plan_code: str
    idempotency_key: str = Field(min_length=8, max_length=100)


class VerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


# ---------------------------------------------------------------- admin
class AdminUserPatch(BaseModel):
    role: Role | None = None
    status: Literal["invited", "active", "suspended"] | None = None
    institution_id: UUID | None = None
    reason: str = Field(min_length=3, max_length=500)


class InviteIn(BaseModel):
    email: str
    full_name: str | None = None
    phone: str | None = None
    mode: Mode = "student"
    role: Role = "member"
    plan_code: str = "waitlist_beta"


class GrantIn(BaseModel):
    plan_code: str | None = None
    extra_voice_minutes: float | None = Field(default=None, ge=0, le=600)
    extra_ai_requests: int | None = Field(default=None, ge=0, le=100000)
    extra_chat_messages: int | None = Field(default=None, ge=0, le=100000)
    reason: str = Field(min_length=3, max_length=500)


class RoleDecisionIn(BaseModel):
    approve: bool
    note: str | None = None


class FlagIn(BaseModel):
    enabled: bool


class KnowledgeTextIn(BaseModel):
    title: str = Field(max_length=200)
    text: str = Field(min_length=40, max_length=400_000)
    category: str | None = None
    audience: list[Mode] | None = None
    institution_id: UUID | None = None
    is_demo: bool = False


class KnowledgeUrlIn(BaseModel):
    url: str
    title: str | None = None
    category: str | None = None
    audience: list[Mode] | None = None
    institution_id: UUID | None = None


class DirectoryIn(BaseModel):
    name: str
    kind: Literal["faculty", "advisor", "office"] = "faculty"
    department: str | None = None
    designation: str | None = None
    subjects: list[str] = Field(default_factory=list)
    expertise: list[str] = Field(default_factory=list)
    office_hours: str | None = None
    contact_hint: str | None = None
    bio: str | None = None
    institution_id: UUID | None = None
    is_demo: bool = True
    user_id: UUID | None = None


class TemplateIn(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]{3,50}$")
    modes: list[Mode]
    category: str
    title: str
    description: str
    help_types: list[str] = Field(default_factory=list)
    prep_questions: list[str] = Field(default_factory=list)
    share_checklist: list[str] = Field(default_factory=list)
    sort_order: int = 100


class AssignIn(BaseModel):
    staff_user_id: UUID
