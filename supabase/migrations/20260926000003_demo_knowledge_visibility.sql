-- Demo knowledge is visible to every beta tester regardless of institution.
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
      and (s.institution_id is null or s.is_demo or p_institution is null or s.institution_id = p_institution)
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
revoke execute on function match_knowledge from anon, authenticated, public;
