-- SAGE AI — initial schema (PRD 1.0)
-- The FastAPI service connects as a privileged role. RLS is enabled on every table with
-- no policies, so the public anon/authenticated PostgREST roles can read nothing.
-- All authorization is enforced server-side by the API (PRD: "authorization remains server-owned").

create extension if not exists pgcrypto;
create extension if not exists citext;
create extension if not exists vector;
create extension if not exists pg_trgm;

-- ---------------------------------------------------------------- enums
create type experience_mode as enum ('student', 'professional', 'founder');
create type access_role     as enum ('member', 'faculty', 'advisor', 'superadmin');
create type user_status     as enum ('invited', 'active', 'suspended', 'deleted');
create type value_source    as enum ('user', 'demo', 'system', 'import');

create type intervention_status as enum (
  'open', 'call_requested', 'in_call', 'awaiting_review', 'plan_accepted', 'closed', 'dismissed');

create type call_status as enum (
  'requested', 'dispatching', 'dispatched', 'in_progress',
  'completed', 'no_answer', 'busy', 'failed', 'cancelled');
create type transcript_status as enum ('none', 'partial', 'available');
create type extraction_status as enum ('not_applicable', 'pending', 'running', 'succeeded', 'failed');

create type plan_status as enum ('draft', 'accepted', 'completed', 'abandoned');
create type case_status as enum ('open', 'in_progress', 'follow_up', 'resolved');
create type order_status as enum ('created', 'paid', 'failed', 'expired');
create type subscription_status as enum ('active', 'expired', 'cancelled');

-- ---------------------------------------------------------------- helpers
create or replace function set_updated_at() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

-- ---------------------------------------------------------------- institutions
create table institutions (
  id          uuid primary key default gen_random_uuid(),
  slug        text unique not null,
  name        text not null,
  is_demo     boolean not null default false,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- users
create table users (
  id               uuid primary key default gen_random_uuid(),
  clerk_user_id    text unique,
  email            citext unique not null,
  full_name        text,
  phone            text,
  whatsapp         text,
  linkedin_url     text,
  mode             experience_mode not null default 'student',
  role             access_role not null default 'member',
  status           user_status not null default 'invited',
  institution_id   uuid references institutions(id) on delete set null,
  waitlist_segment text,          -- raw role from waitlist export, e.g. "BITSOM student"
  waitlist_source  text,
  waitlist_joined_at timestamptz,
  is_demo          boolean not null default false,
  onboarded_at     timestamptz,   -- set when user completes mode selection / profile
  last_seen_at     timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index users_role_idx on users(role);
create trigger users_updated before update on users for each row execute function set_updated_at();

create table role_change_requests (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references users(id) on delete cascade,
  requested_role access_role not null,
  reason         text,
  status         text not null default 'pending' check (status in ('pending','approved','rejected')),
  reviewed_by    uuid references users(id),
  reviewed_at    timestamptz,
  created_at     timestamptz not null default now()
);
create unique index role_change_one_pending on role_change_requests(user_id) where status = 'pending';

-- ---------------------------------------------------------------- goal profile & signals
create table goal_profiles (
  user_id        uuid primary key references users(id) on delete cascade,
  mode           experience_mode not null,
  data           jsonb not null default '{}'::jsonb,   -- mode-specific fields
  field_sources  jsonb not null default '{}'::jsonb,   -- field -> value_source
  availability   jsonb not null default '{}'::jsonb,
  consent        jsonb not null default '{}'::jsonb,
  demo_profile_key text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create trigger goal_profiles_updated before update on goal_profiles for each row execute function set_updated_at();

create table demo_profiles (
  key          text primary key,
  mode         experience_mode not null,
  display_name text not null,
  summary      text not null,
  profile      jsonb not null,
  signals      jsonb not null,
  hidden_context text,           -- ground truth used only by the call-fixture simulator
  label        text not null default 'Synthetic demonstration profile — not a real person',
  created_at   timestamptz not null default now()
);

create table signals (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id) on delete cascade,
  type        text not null,           -- attendance, deadline, grade, exam, fee, interview, milestone, ...
  label       text not null,
  subject     text,
  value_num   numeric,
  value_text  text,
  threshold   numeric,
  unit        text,
  due_at      timestamptz,
  observed_at timestamptz not null default now(),
  source      value_source not null default 'user',
  status      text not null default 'active' check (status in ('active','dismissed','resolved')),
  dispute_note text,
  metadata    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index signals_user_idx on signals(user_id, status);
create trigger signals_updated before update on signals for each row execute function set_updated_at();

-- ---------------------------------------------------------------- AI audit
create table ai_runs (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid references users(id) on delete set null,
  task            text not null,        -- prioritize, extract, chat, embed ...
  prompt_version  text not null,
  model           text not null,
  provider_request_id text,
  correlation_id  text,
  latency_ms      integer,
  input_tokens    integer,
  output_tokens   integer,
  status          text not null,        -- succeeded, failed, fallback
  validation_ok   boolean,
  error           text,
  created_at      timestamptz not null default now()
);
create index ai_runs_user_idx on ai_runs(user_id, created_at desc);
create index ai_runs_task_idx on ai_runs(task, created_at desc);

-- ---------------------------------------------------------------- interventions
create table interventions (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,
  status        intervention_status not null default 'open',
  category      text not null,
  title         text not null,
  reason        text not null,
  evidence      jsonb not null default '[]'::jsonb,
  missing_info  jsonb not null default '[]'::jsonb,
  next_items    jsonb not null default '[]'::jsonb,
  brief         jsonb not null default '{}'::jsonb,   -- full validated AI output
  confidence    numeric(4,3),
  is_fallback   boolean not null default false,
  ai_run_id     uuid references ai_runs(id),
  signal_ids    uuid[] not null default '{}',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index interventions_user_idx on interventions(user_id, created_at desc);
create trigger interventions_updated before update on interventions for each row execute function set_updated_at();

-- ---------------------------------------------------------------- calls
create table calls (
  id                    uuid primary key default gen_random_uuid(),
  user_id               uuid not null references users(id) on delete cascade,
  intervention_id       uuid references interventions(id) on delete set null,
  initiated_by          uuid not null references users(id),
  idempotency_key       text not null,
  status                call_status not null default 'requested',
  destination           text not null,
  consent_text          text not null,
  consented_at          timestamptz not null,
  expected_duration_sec integer not null,
  estimated_minutes     numeric(6,2) not null,
  provider              text not null default 'omnidim',
  provider_agent_id     text,
  provider_request_id   text,
  provider_call_log_id  text,
  provider_status       text,
  provider_hangup_reason text,
  duration_seconds      numeric(10,2),
  cost                  jsonb not null default '{}'::jsonb,
  transcript_status     transcript_status not null default 'none',
  extraction_status     extraction_status not null default 'not_applicable',
  is_simulated          boolean not null default false,
  error                 text,
  reconcile_attempts    integer not null default 0,
  last_reconciled_at    timestamptz,
  dispatched_at         timestamptz,
  completed_at          timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (user_id, idempotency_key)
);
create index calls_user_idx on calls(user_id, created_at desc);
create index calls_open_idx on calls(status) where status in ('dispatching','dispatched','in_progress');
create unique index calls_provider_req_idx on calls(provider, provider_request_id) where provider_request_id is not null;
-- at most one live call per user: repeated taps cannot create concurrent calls
create unique index calls_one_live_per_user on calls(user_id)
  where status in ('requested','dispatching','dispatched','in_progress');
create trigger calls_updated before update on calls for each row execute function set_updated_at();

create table transcripts (
  call_id           uuid primary key references calls(id) on delete cascade,
  raw_text          text not null,
  interactions      jsonb not null default '[]'::jsonb,
  recording_url     text,
  provider_summary  text,
  provider_extracted jsonb not null default '{}'::jsonb,
  is_complete       boolean not null default true,
  created_at        timestamptz not null default now()
);

create table extractions (
  id              uuid primary key default gen_random_uuid(),
  call_id         uuid not null references calls(id) on delete cascade,
  user_id         uuid not null references users(id) on delete cascade,
  attempt         integer not null default 1,
  status          extraction_status not null,
  output          jsonb,                -- original, schema-validated AI output (never edited)
  ai_run_id       uuid references ai_runs(id),
  error           text,
  created_at      timestamptz not null default now()
);
create index extractions_call_idx on extractions(call_id, created_at desc);

-- ---------------------------------------------------------------- action plans
create table action_plans (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references users(id) on delete cascade,
  intervention_id  uuid references interventions(id) on delete set null,
  call_id          uuid references calls(id) on delete set null,
  extraction_id    uuid references extractions(id) on delete set null,
  status           plan_status not null default 'draft',
  summary          text not null,
  issue            text,
  root_cause       text,
  actions          jsonb not null default '[]'::jsonb,   -- [{action, owner, due_date}]
  owner            text,
  due_date         date,
  risk_level       text check (risk_level in ('low','medium','high')),
  confidence       numeric(4,3),
  escalation       jsonb not null default '{}'::jsonb,
  advisor_template_key text,
  follow_up_at     timestamptz,
  is_user_edited   boolean not null default false,
  accepted_at      timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index action_plans_user_idx on action_plans(user_id, created_at desc);
create trigger action_plans_updated before update on action_plans for each row execute function set_updated_at();

create table plan_revisions (
  id         uuid primary key default gen_random_uuid(),
  plan_id    uuid not null references action_plans(id) on delete cascade,
  editor_id  uuid not null references users(id),
  before     jsonb not null,
  after      jsonb not null,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------- human support
create table advisor_templates (
  key            text primary key,
  modes          experience_mode[] not null,
  category       text not null,
  title          text not null,
  description    text not null,
  help_types     jsonb not null default '[]'::jsonb,
  prep_questions jsonb not null default '[]'::jsonb,
  share_checklist jsonb not null default '[]'::jsonb,
  label          text not null default 'Template — describes a type of support, not a real person',
  sort_order     integer not null default 100
);

create table faculty_directory (
  id             uuid primary key default gen_random_uuid(),
  institution_id uuid references institutions(id) on delete cascade,
  user_id        uuid references users(id) on delete set null,
  name           text not null,
  kind           text not null default 'faculty' check (kind in ('faculty','advisor','office')),
  department     text,
  designation    text,
  subjects       text[] not null default '{}',
  expertise      text[] not null default '{}',
  office_hours   text,
  contact_hint   text,
  bio            text,
  is_demo        boolean not null default true,
  label          text not null default 'Synthetic demonstration record',
  created_at     timestamptz not null default now()
);

create table cases (
  id               uuid primary key default gen_random_uuid(),
  student_id       uuid not null references users(id) on delete cascade,
  plan_id          uuid references action_plans(id) on delete set null,
  intervention_id  uuid references interventions(id) on delete set null,
  assigned_to      uuid references users(id) on delete set null,
  shared_by_student boolean not null default false,
  include_transcript boolean not null default false,
  status           case_status not null default 'open',
  follow_up_at     timestamptz,
  revoked_at       timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index cases_assignee_idx on cases(assigned_to, status);
create index cases_student_idx on cases(student_id);
create trigger cases_updated before update on cases for each row execute function set_updated_at();

create table case_notes (
  id         uuid primary key default gen_random_uuid(),
  case_id    uuid not null references cases(id) on delete cascade,
  author_id  uuid not null references users(id),
  kind       text not null default 'note' check (kind in ('note','status_change','follow_up','resource')),
  body       text not null,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------- billing
create table billing_plans (
  code            text primary key,
  name            text not null,
  description     text not null,
  price_inr_paise integer not null default 0,
  price_usd_cents integer not null default 0,
  period_days     integer not null default 30,
  voice_minutes   numeric(8,2) not null default 0,
  ai_requests     integer not null default 0,
  chat_messages   integer not null default 0,
  features        jsonb not null default '[]'::jsonb,
  is_purchasable  boolean not null default false,
  is_active       boolean not null default true,
  sort_order      integer not null default 100
);

create table subscriptions (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references users(id) on delete cascade,
  plan_code        text not null references billing_plans(code),
  status           subscription_status not null default 'active',
  period_start     timestamptz not null default now(),
  period_end       timestamptz,
  voice_seconds_allowance integer not null,
  voice_seconds_used      integer not null default 0,
  ai_requests_allowance   integer not null,
  ai_requests_used        integer not null default 0,
  chat_messages_allowance integer not null,
  chat_messages_used      integer not null default 0,
  is_test          boolean not null default true,
  source           text not null default 'system',
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create unique index subscriptions_one_active on subscriptions(user_id) where status = 'active';
create trigger subscriptions_updated before update on subscriptions for each row execute function set_updated_at();

create table payment_orders (
  id                  uuid primary key default gen_random_uuid(),
  user_id             uuid not null references users(id) on delete cascade,
  plan_code           text not null references billing_plans(code),
  idempotency_key     text not null,
  amount              integer not null,
  currency            text not null default 'INR',
  status              order_status not null default 'created',
  razorpay_order_id   text unique,
  razorpay_payment_id text,
  verified_at         timestamptz,
  is_test             boolean not null default true,
  error               text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  unique (user_id, idempotency_key)
);
create trigger payment_orders_updated before update on payment_orders for each row execute function set_updated_at();

-- ---------------------------------------------------------------- webhooks, flags, audit
create table webhook_events (
  id          uuid primary key default gen_random_uuid(),
  provider    text not null,
  dedupe_key  text not null,
  payload     jsonb not null,
  status      text not null default 'received' check (status in ('received','processed','ignored','failed')),
  error       text,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  unique (provider, dedupe_key)
);

create table feature_flags (
  key         text primary key,
  enabled     boolean not null default false,
  description text,
  updated_at  timestamptz not null default now()
);

create table audit_events (
  id             bigint generated always as identity primary key,
  actor_id       uuid references users(id) on delete set null,
  action         text not null,
  target_type    text,
  target_id      text,
  metadata       jsonb not null default '{}'::jsonb,
  correlation_id text,
  created_at     timestamptz not null default now()
);
create index audit_events_created_idx on audit_events(created_at desc);
create index audit_events_target_idx on audit_events(target_type, target_id);

-- ---------------------------------------------------------------- knowledge base (RAG)
create table knowledge_sources (
  id             uuid primary key default gen_random_uuid(),
  institution_id uuid references institutions(id) on delete cascade,
  title          text not null,
  kind           text not null check (kind in ('text','url','pdf','faq','faculty')),
  url            text,
  category       text,
  audience       experience_mode[] not null default '{student,professional,founder}',
  status         text not null default 'pending' check (status in ('pending','ready','failed')),
  checksum       text,
  chunk_count    integer not null default 0,
  is_demo        boolean not null default false,
  error          text,
  created_by     uuid references users(id) on delete set null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create trigger knowledge_sources_updated before update on knowledge_sources for each row execute function set_updated_at();

create table knowledge_chunks (
  id          uuid primary key default gen_random_uuid(),
  source_id   uuid not null references knowledge_sources(id) on delete cascade,
  chunk_index integer not null,
  content     text not null,
  heading     text,
  embedding   vector(1536) not null,
  fts         tsvector generated always as (to_tsvector('english', coalesce(heading,'') || ' ' || content)) stored,
  created_at  timestamptz not null default now(),
  unique (source_id, chunk_index)
);
create index knowledge_chunks_embedding_idx on knowledge_chunks using hnsw (embedding vector_cosine_ops);
create index knowledge_chunks_fts_idx on knowledge_chunks using gin (fts);

-- Hybrid retrieval: reciprocal-rank fusion of vector similarity and full-text rank.
create or replace function match_knowledge(
  query_embedding vector(1536),
  query_text      text,
  match_count     integer default 6,
  p_institution   uuid default null,
  p_mode          experience_mode default null
) returns table (chunk_id uuid, source_id uuid, title text, heading text, content text, url text, is_demo boolean, score double precision)
language sql stable as $$
  with eligible as (
    select c.*, s.title, s.url, s.is_demo
    from knowledge_chunks c join knowledge_sources s on s.id = c.source_id
    where s.status = 'ready'
      and (s.institution_id is null or p_institution is null or s.institution_id = p_institution)
      and (p_mode is null or p_mode = any(s.audience))
  ),
  vec as (
    select id, row_number() over (order by embedding <=> query_embedding) as r
    from eligible order by embedding <=> query_embedding limit match_count * 4
  ),
  txt as (
    select id, row_number() over (order by ts_rank_cd(fts, q) desc) as r
    from eligible, websearch_to_tsquery('english', query_text) q
    where fts @@ q
    order by ts_rank_cd(fts, q) desc limit match_count * 4
  ),
  fused as (
    select id, sum(1.0 / (60 + r)) as score from (select * from vec union all select * from txt) u group by id
  )
  select e.id, e.source_id, e.title, e.heading, e.content, e.url, e.is_demo, f.score
  from fused f join eligible e on e.id = f.id
  order by f.score desc limit match_count;
$$;

-- ---------------------------------------------------------------- chat
create table chat_sessions (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references users(id) on delete cascade,
  title      text not null default 'New conversation',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index chat_sessions_user_idx on chat_sessions(user_id, updated_at desc);
create trigger chat_sessions_updated before update on chat_sessions for each row execute function set_updated_at();

create table chat_messages (
  id         uuid primary key default gen_random_uuid(),
  session_id uuid not null references chat_sessions(id) on delete cascade,
  role       text not null check (role in ('user','assistant')),
  content    text not null,
  citations  jsonb not null default '[]'::jsonb,
  safety     jsonb not null default '{}'::jsonb,
  ai_run_id  uuid references ai_runs(id),
  created_at timestamptz not null default now()
);
create index chat_messages_session_idx on chat_messages(session_id, created_at);

-- ---------------------------------------------------------------- lock down PostgREST access
do $$
declare t record;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t.tablename);
    execute format('revoke all on public.%I from anon, authenticated', t.tablename);
  end loop;
end $$;
revoke execute on function match_knowledge from anon, authenticated, public;
