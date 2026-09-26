-- Campus records are served only through the authenticated SAGE API.
create table campus_plans (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references users(id) on delete cascade,
 kind text not null check(kind in ('semester','activity','class_recommendations')),
 output jsonb not null, ai_run_id uuid references ai_runs(id), created_at timestamptz not null default now()
);
create index campus_plans_user_idx on campus_plans(user_id,created_at desc);
create table campus_tasks (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references users(id) on delete cascade,
 plan_id uuid references campus_plans(id) on delete cascade, title text not null, category text not null,
 due_at timestamptz not null, minutes integer not null default 30 check(minutes between 1 and 1440),
 status text not null default 'pending' check(status in ('pending','done')), evidence text not null default '',
 completed_at timestamptz, created_at timestamptz not null default now()
);
create index campus_tasks_user_due_idx on campus_tasks(user_id,due_at);
create table campus_deadlines (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references users(id) on delete cascade,
 title text not null, category text not null check(category in ('assignment','academic_fee','bus_fee','other_fee')),
 due_at timestamptz not null, amount integer not null default 0 check(amount>=0),
 status text not null default 'pending' check(status in ('pending','done','paid')),
 is_demo boolean not null default true, reminder_state text not null default 'pending',
 intervention_id uuid references interventions(id), call_id uuid references calls(id), reason text,
 created_at timestamptz not null default now()
);
create index campus_deadlines_due_idx on campus_deadlines(due_at) where status='pending';
create table campus_opportunities (
 id text primary key, kind text not null, title text not null, organisation text not null,
 location text not null, salary_lpa numeric not null default 0, skills jsonb not null default '[]',
 specialisations jsonb not null default '[]', description text not null, source_url text,
 event_at timestamptz, is_demo boolean not null default true, embedding vector(1536)
);
alter table payment_orders add column purpose text not null default 'subscription' check(purpose in ('subscription','fee'));
alter table payment_orders add column deadline_id uuid references campus_deadlines(id);
alter table study_decks add column coach_kind text not null default 'study' check(coach_kind in ('study','interview'));
alter table study_decks add column job_context jsonb not null default '{}';

do $$ declare t text; begin foreach t in array array['campus_plans','campus_tasks','campus_deadlines','campus_opportunities'] loop
 execute format('alter table %I enable row level security',t);
 execute format('revoke all on %I from anon, authenticated',t);
end loop; end $$;

insert into billing_plans(code,name,description,price_inr_paise,period_days,voice_minutes,ai_requests,chat_messages,autoapply_calls,features,is_purchasable,sort_order) values
('plus','Plus','Study essentials and your first placement plan',49900,30,10,30,100,100,'["academic_dashboard","quick_notes","semester_planner","basic_recommendations","voice_practice"]',true,10),
('pro','Pro','Personalised placement preparation and weekly execution',149900,30,40,100,400,400,'["academic_dashboard","quick_notes","semester_planner","semantic_jobs","activity_planner","class_recommendations","interview_ai","voice_practice","deadline_calls"]',true,20),
('ultra','Ultra','More practice and planning for an intensive placement season',349900,30,120,250,1000,1000,'["academic_dashboard","quick_notes","semester_planner","semantic_jobs","activity_planner","class_recommendations","interview_ai","voice_practice","deadline_calls"]',true,30),
('campus_fee_test','Campus demo fee','Synthetic campus fee checkout; not a payment to BITSoM',0,30,0,0,0,0,'[]',false,999)
on conflict(code) do update set name=excluded.name,description=excluded.description,price_inr_paise=excluded.price_inr_paise,
voice_minutes=excluded.voice_minutes,ai_requests=excluded.ai_requests,chat_messages=excluded.chat_messages,
autoapply_calls=excluded.autoapply_calls,features=excluded.features,is_purchasable=excluded.is_purchasable,sort_order=excluded.sort_order;
update billing_plans set is_active=false where code='campus_fee_test';
-- Retain existing subscriptions while replacing only the online purchase catalogue.
update billing_plans set is_purchasable=false where code not in ('plus','pro','ultra');
alter table payment_orders add column checkout_token_hash text;
alter table payment_orders add column checkout_expires_at timestamptz;
alter table campus_deadlines add column reminder_claimed_at timestamptz;
