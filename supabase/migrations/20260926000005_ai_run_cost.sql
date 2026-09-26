-- Provider-reported USD cost per AI run (OpenRouter returns it; OpenAI does not).
alter table ai_runs add column if not exists cost_usd numeric(12,6);
