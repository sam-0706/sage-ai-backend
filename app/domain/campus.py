from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator

class PlanRequest(BaseModel):
 kind: Literal['semester','activity','class_recommendations'] = 'semester'
 goal: str = Field(min_length=3,max_length=500)
 weeks: int = Field(default=8,ge=1,le=24)
 focus: str = Field(default='placements',max_length=120)

class Step(BaseModel):
 title: str
 category: Literal['learning','project','internship','networking','application','revision']
 day: int
 minutes: int
 deliverable: str
 real_world_use: str

class Milestone(BaseModel):
 week: int
 title: str
 evidence: str

class LearningPlan(BaseModel):
 title: str
 summary: str
 target_role: str
 salary_context: str
 weekly_hours: float
 milestones: list[Milestone]
 tasks: list[Step]
 recommended_internships: int
 recommended_projects: int
 class_priorities: list[str]
 assumptions: list[str]

class TaskUpdate(BaseModel):
 status: Literal['pending','done']
 evidence: str = Field(default='',max_length=2000)

class DeadlineIn(BaseModel):
 title: str = Field(min_length=2,max_length=180)
 category: Literal['assignment','academic_fee','bus_fee','other_fee']
 due_at: datetime
 amount: int = Field(default=0,ge=0,le=50000000)
 @field_validator('due_at')
 @classmethod
 def aware(cls,v):
  if v.tzinfo is None: raise ValueError('Include the timezone')
  return v

class InterviewIn(BaseModel):
 job_id: str
 resume_text: str = Field(min_length=30,max_length=16000)

class CheckoutIn(BaseModel):
 idempotency_key: str = Field(min_length=8,max_length=120)

class JobDiscoveryIn(BaseModel):
 role: str | None = Field(default=None,max_length=120)
 location: str | None = Field(default=None,max_length=120)
 work_mode: Literal['any','remote','hybrid','onsite'] = 'any'

class DiscoveredJob(BaseModel):
 title: str
 company: str
 location: str
 work_mode: str
 employment_type: str
 salary: str | None = None
 posted_at: str | None = None
 source_name: str
 source_url: str
 apply_url: str
 skills: list[str]
 match_score: int = Field(ge=0,le=100)
 why_it_fits: list[str]
 gaps: list[str]

class JobDiscoveryResult(BaseModel):
 summary: str
 jobs: list[DiscoveredJob] = Field(max_length=5)
 searched_at: str
 search_notes: list[str]
