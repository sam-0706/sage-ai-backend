-- Desktop sign-in (device flow), onboarding, exam prep (cue cards + voice quiz + analysis), auto-apply LLM metering.

-- ---------------------------------------------------------------- onboarding
alter table users add column if not exists onboarding_completed_at timestamptz;

-- ---------------------------------------------------------------- device sign-in (desktop now, TV/CLI-style flow)
create table device_auth_requests (
  id                uuid primary key default gen_random_uuid(),
  device_code_hash  text unique not null,       -- sha256 of the secret the device polls with
  user_code         text unique not null,       -- short code the user confirms in the browser
  client            text not null check (client in ('desktop','mobile','cli')),
  device_name       text,
  status            text not null default 'pending' check (status in ('pending','approved','denied','consumed','expired')),
  user_id           uuid references users(id) on delete cascade,
  approved_at       timestamptz,
  expires_at        timestamptz not null,
  created_at        timestamptz not null default now()
);
create index device_auth_pending_idx on device_auth_requests(status, expires_at);

create table app_sessions (
  id            uuid primary key default gen_random_uuid(),
  token_hash    text unique not null,          -- sha256 of the bearer token; token itself is never stored
  user_id       uuid not null references users(id) on delete cascade,
  client        text not null,
  device_name   text,
  expires_at    timestamptz not null,
  revoked_at    timestamptz,
  last_used_at  timestamptz,
  created_at    timestamptz not null default now()
);
create index app_sessions_user_idx on app_sessions(user_id);

-- ---------------------------------------------------------------- exam prep: decks & cue cards
create table study_decks (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,
  topic         text not null,
  level         text,
  exam          text,
  title         text not null,
  summary       text not null default '',
  key_concepts  jsonb not null default '[]'::jsonb,
  quick_tips    jsonb not null default '[]'::jsonb,
  common_mistakes jsonb not null default '[]'::jsonb,
  source        text not null default 'ai' check (source in ('ai','ai_from_notes','user')),
  ai_run_id     uuid references ai_runs(id),
  card_count    integer not null default 0,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index study_decks_user_idx on study_decks(user_id, created_at desc);
create trigger study_decks_updated before update on study_decks for each row execute function set_updated_at();

create table study_cards (
  id              uuid primary key default gen_random_uuid(),
  deck_id         uuid not null references study_decks(id) on delete cascade,
  position        integer not null,
  concept         text not null,
  card_type       text not null default 'concept',
  front           text not null,
  back            text not null,
  hint            text,
  mnemonic        text,
  difficulty      text not null default 'medium' check (difficulty in ('easy','medium','hard')),
  -- spaced repetition (SM-2 style)
  ease            numeric(4,2) not null default 2.5,
  interval_days   numeric(8,2) not null default 0,
  reps            integer not null default 0,
  lapses          integer not null default 0,
  due_at          timestamptz not null default now(),
  last_reviewed_at timestamptz,
  created_at      timestamptz not null default now(),
  unique (deck_id, position)
);
create index study_cards_due_idx on study_cards(deck_id, due_at);

create table card_reviews (
  id          bigint generated always as identity primary key,
  card_id     uuid not null references study_cards(id) on delete cascade,
  user_id     uuid not null references users(id) on delete cascade,
  rating      smallint not null check (rating between 1 and 4),   -- 1 again, 2 hard, 3 good, 4 easy
  reviewed_at timestamptz not null default now()
);
create index card_reviews_user_idx on card_reviews(user_id, reviewed_at desc);

-- ---------------------------------------------------------------- exam prep voice quiz: reuse `calls`
alter table calls add column if not exists purpose text not null default 'checkin' check (purpose in ('checkin','exam_prep'));
alter table calls add column if not exists deck_id uuid references study_decks(id) on delete set null;

create table exam_assessments (
  id             uuid primary key default gen_random_uuid(),
  call_id        uuid not null references calls(id) on delete cascade,
  deck_id        uuid references study_decks(id) on delete set null,
  user_id        uuid not null references users(id) on delete cascade,
  attempt        integer not null default 1,
  status         extraction_status not null,
  output         jsonb,
  overall_score  integer,
  readiness      text,
  ai_run_id      uuid references ai_runs(id),
  error          text,
  created_at     timestamptz not null default now()
);
create index exam_assessments_user_idx on exam_assessments(user_id, created_at desc);
create index exam_assessments_call_idx on exam_assessments(call_id, created_at desc);

-- ---------------------------------------------------------------- auto-apply LLM metering (desktop → SAGE proxy → OpenRouter)
alter table billing_plans add column if not exists autoapply_calls integer not null default 0;
alter table subscriptions add column if not exists autoapply_calls_allowance integer not null default 0;
alter table subscriptions add column if not exists autoapply_calls_used integer not null default 0;

update billing_plans set autoapply_calls = case code
  when 'waitlist_beta' then 1500
  when 'student_free'  then 300
  when 'sage_plus'     then 3000
  when 'sage_pro'      then 10000
  when 'campus_pilot'  then 100000
  else 0 end;

update subscriptions s set autoapply_calls_allowance = p.autoapply_calls
  from billing_plans p where p.code = s.plan_code and s.autoapply_calls_allowance = 0;

insert into feature_flags (key, enabled, description) values
  ('exam_prep', true, 'Cue cards, spaced repetition and exam-prep voice quizzes'),
  ('auto_apply', true, 'Desktop auto-apply agent via the SAGE LLM proxy')
on conflict (key) do nothing;

-- ---------------------------------------------------------------- lock down new tables
do $$
declare t text;
begin
  foreach t in array array['device_auth_requests','app_sessions','study_decks','study_cards','card_reviews','exam_assessments'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
  end loop;
end $$;
