# SAGE AI — Backend

**Strategic Action and Growth Engine.** One versioned FastAPI backend serving the React/Vite web app, the Electron desktop
shell and the Flutter mobile app (PRD 1.0).

The product loop:

```
goal profile + signals → rules detect → AI prioritises & writes a brief → consented AI voice check-in
→ verified call state → AI extraction → student edits & accepts plan → (optional) explicit share with staff
```

Plus a **RAG knowledge chatbot** over the college knowledge base and faculty/advisor directory, **exam prep**
(AI cue-card decks → SM-2 spaced repetition → a spoken quiz by an OmniDimension exam-coach agent → a
"where you stand" analysis that feeds weak concepts back into review), and a **metered LLM proxy** that powers the
desktop app's local auto-apply agent without shipping any provider key to user machines.

### Desktop sign-in (device flow)

Desktop/mobile apps call `POST /v1/auth/device/start`, open the returned `verification_url` in the system browser
(Clerk sign-in + "approve this code"), and poll `POST /v1/auth/device/token`. Approval returns a single-use opaque
`sds_…` session token (30 days, SHA-256-hashed at rest, revocable from Settings). The API accepts either a Clerk JWT
(web) or an `sds_` token (desktop/mobile); authorization always comes from the SAGE user row.

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI (Python 3.12), versioned under `/v1`, OpenAPI at `/docs` |
| Database | Supabase Postgres (asyncpg via the transaction pooler), pgvector, pg_trgm |
| Auth | Clerk session JWTs verified via JWKS, one auth module (`app/auth/deps.py`) |
| AI | OpenAI `gpt-5-mini` (structured outputs) + `text-embedding-3-small` |
| Voice | OmniDimension outbound agent, webhook + reconciliation |
| Payments | Razorpay **test mode only** (live keys are refused at startup) |
| Hosting | Vercel Python runtime (Fluid compute), region `hnd1` next to the Supabase DB (Tokyo) |

## Architecture

```
app/
  main.py               app factory: CORS, correlation IDs, security headers, error envelope
  core/                 config (env), JSON logging with redaction, errors, request context
  auth/deps.py          Clerk token → SAGE user (waitlist binding) → Principal; role dependencies
  api/health.py         /health/live (liveness), /health/ready (DB + config readiness)
  api/v1/               thin routers: me, journey, assist (chat/RAG), billing, staff, admin, webhooks, internal
  domain/               AI output contracts (strict JSON schema) + request schemas
  services/             use cases: profile, priority, calls, postcall, plans, knowledge, chat, billing, entitlements
  integrations/         provider adapters: openai_client, omnidim, razorpay, clerk_api, http (timeouts/retries)
  repositories/         asyncpg pool + explicit transactions, users, audit, flags
  workers/jobs.py       reconciliation / retry / expiry jobs (cron + on-read + webhook triggered)
supabase/migrations/    version-controlled SQL (applied by scripts/migrate.py — never at startup)
scripts/                migrate, seed_waitlist, seed_knowledge, setup_voice_agent
seeds/                  synthetic, labelled demo knowledge base + directory
tests/                  unit + API contract tests
```

### Key design decisions

- **Identity vs. authorization.** Clerk proves identity only. Role (`member | faculty | advisor | superadmin`) and experience
  mode (`student | professional | founder`) are separate columns in Postgres and are enforced server-side. Role changes need
  superadmin approval and are audited.
- **Invite-only beta.** The 110 waitlist members are pre-onboarded (`status=invited`, mode derived from their segment,
  Waitlist Beta plan). On first sign-in the Clerk identity is bound by **verified email**. Anyone else gets
  `403 not_on_waitlist` unless the `open_signup` flag is turned on.
- **RLS lockdown.** RLS is enabled on every table with no policies and privileges revoked from `anon`/`authenticated`, so the
  public Supabase keys can read nothing. Only the API (server-side connection) touches data.
- **Voice reliability.** Consent is stored before dispatch; destinations must be the user's own number or an allow-listed
  test number; `(user_id, idempotency_key)` is unique and a partial unique index allows one live call per user. Dispatch is
  never auto-retried (it could ring twice). Final state comes only from the provider's call log, matched by
  `call_request_id` via webhook, reconcile-on-read (stale > 20 s) and the daily cron sweep. Calls with no provider record
  after 30 min become `failed` — transcripts are never fabricated.
- **Separate states.** `calls.status`, `transcript_status` and `extraction_status` are independent. Original AI output
  (`extractions.output`) is immutable; user edits live on `action_plans` with `plan_revisions`.
- **AI contract.** Every AI task returns schema-validated JSON and writes an `ai_runs` row (prompt version, model, request
  id, latency, tokens, validation). Priority falls back to deterministic rules if AI fails; chat and extraction degrade
  gracefully with retry paths. Untrusted text (profiles, transcripts, documents) is delimited and never treated as instructions.
- **RAG.** Knowledge sources are chunked (heading-aware), embedded into `vector(1536)`, and retrieved by hybrid search
  (HNSW cosine + Postgres full-text, reciprocal-rank fusion) in `match_knowledge()`. Answers cite `[n]` snippets; demo
  sources are labelled. A crisis detector pauses coaching and returns emergency guidance.
- **Entitlements.** Plans and allowances (voice seconds, AI requests, chat messages) live in the DB and are consumed
  atomically; clients never hardcode limits.
- **Payments.** Orders are created server-side (idempotent per key), the checkout signature is verified with HMAC, then the
  payment is re-fetched from Razorpay (order id, amount, status) before any entitlement changes. Webhooks are
  signature-verified and deduplicated.
- **Errors.** Every error is `{"error": {"code", "message", "details", "correlation_id"}}`; `X-Request-ID` is propagated to
  providers and logs.

## API overview (`/docs` has the full OpenAPI)

All `/v1` routes require `Authorization: Bearer <Clerk session token>` except webhooks and cron.

| Area | Endpoints |
|---|---|
| Desktop/mobile sign-in | `POST /v1/auth/device/start`, `POST /v1/auth/device/token`, browser page `GET /auth/device?code=…`, `POST /v1/auth/device/approve`, `POST /v1/auth/logout`, `GET/DELETE /v1/auth/sessions` |
| Onboarding | `GET /v1/onboarding` (pre-filled from the waitlist), `POST /v1/onboarding` |
| Exam prep | `GET /v1/exam-prep/overview`, `GET/POST /v1/exam-prep/decks`, `GET/DELETE /v1/exam-prep/decks/{id}`, `GET /v1/exam-prep/decks/{id}/study`, `POST /v1/exam-prep/cards/{id}/review`, `GET /v1/exam-prep/calls/preflight`, `POST /v1/exam-prep/calls`, `GET /v1/exam-prep/calls/{id}`, `POST /v1/exam-prep/calls/{id}/analyze`, `GET /v1/exam-prep/assessments[/{id}]` |
| Auto-apply (desktop) | `GET /v1/autoapply/status`, OpenAI-compatible `GET /v1/autoapply/llm/models` and `POST /v1/autoapply/llm/chat/completions` (metered proxy to OpenRouter) |
| Me | `GET /v1/me`, `PATCH /v1/me`, `GET /v1/home`, `POST /v1/me/role-requests`, `DELETE /v1/me/data` |
| Profile & signals | `GET/PUT /v1/profile`, `GET /v1/demo-profiles`, `POST /v1/profile/load-demo`, `GET/POST /v1/signals`, `PATCH /v1/signals/{id}` |
| Priority | `POST /v1/interventions/prioritize`, `GET /v1/interventions[/{id}]`, `POST /v1/interventions/{id}/dismiss` |
| Voice | `GET /v1/calls/preflight`, `POST /v1/calls`, `GET /v1/calls[/{id}]`, `GET /v1/calls/{id}/transcript`, `POST /v1/calls/{id}/cancel`, `POST /v1/calls/{id}/extract`, `POST /v1/calls/simulate` (demo profiles only, labelled) |
| Plans | `GET/POST /v1/plans`, `GET/PATCH /v1/plans/{id}`, `POST /v1/plans/{id}/accept|complete|abandon|share`, `GET /v1/shares`, `DELETE /v1/shares/{id}` |
| Assistant | `POST/GET /v1/chat/sessions`, `GET/POST /v1/chat/sessions/{id}/messages` (SSE when `stream=true`), `GET /v1/knowledge/search`, `GET /v1/directory`, `GET /v1/advisor-templates` |
| Billing (test) | `GET /v1/billing/plans`, `GET /v1/billing/subscription`, `POST /v1/billing/orders`, `POST /v1/billing/verify`, `GET /v1/billing/orders` |
| Staff | `GET /v1/staff/cases`, `GET /v1/staff/cases/{id}`, `POST /v1/staff/cases/{id}/notes` |
| Admin | `/v1/admin/overview`, `users` (list/get/patch/invite/grant), `role-requests`, `flags`, `advisor-templates`, `knowledge` (text/url/upload/delete), `directory`, `ai-runs`, `calls` (+ reconcile), `webhook-events`, `audit`, `cases` (+ assign) |
| Webhooks | `POST /v1/webhooks/omnidim?token=…`, `POST /v1/webhooks/razorpay` |
| Health | `GET /health/live`, `GET /health/ready` |

### Chat streaming (SSE)

`POST /v1/chat/sessions/{id}/messages` with `{"content": "...", "stream": true}` emits:

```
event: meta   data: {"type":"meta","citations":[{"n":1,"title":"…","heading":"…","is_demo":true}],"safety":{"crisis":false}}
event: delta  data: {"type":"delta","text":"…"}
event: done   data: {"type":"done","message_id":"…"}
event: error  data: {"type":"error","code":"ai_unavailable","message":"…"}
```

Use `stream: false` for a single JSON response (simplest for Flutter).

## Local development

```bash
uv venv -p 3.12 && source .venv/bin/activate
uv pip install -r pyproject.toml -r requirements-dev.txt
cp .env.example .env            # fill in values
python -m scripts.migrate       # apply SQL migrations
python -m scripts.seed_knowledge
python -m scripts.seed_waitlist /path/to/waitlist.csv   # CSV contains PII: keep it out of git
uvicorn app.main:app --reload --port 8787
pytest -q
```

For local API testing without a browser, set `DEV_AUTH_BYPASS=true` (only honoured when `APP_ENV=development`) and send
`X-Dev-User-Email: <waitlisted email>`.

## Deployment (Vercel)

- `vercel.json` pins functions to `hnd1` (same region as the database), sets `maxDuration`, and schedules the
  maintenance cron (daily on Hobby; the cron is a backstop — webhooks and reconcile-on-read do the real-time work).
- Set every variable from `.env.example` in the Vercel project (Production), with `APP_ENV=production`,
  `DEV_AUTH_BYPASS=false`, `PUBLIC_BASE_URL=https://sage-ai-backend-hazel.vercel.app`.
- After the first deploy: `python -m scripts.setup_voice_agent` → set `OMNIDIM_AGENT_ID` → redeploy.
- Razorpay webhook (optional): point `https://<domain>/v1/webhooks/razorpay` at events `payment.captured`, `order.paid`
  and set `RAZORPAY_WEBHOOK_SECRET`.
