-- GOSIM 2026 survey-agent: LLM usage statistics (read-only).
-- Every query below = the BASE CTE block followed by the query body.
-- Real teams: teams.is_hidden = false, name not like '%(test)%', leader not in site_settings.excluded_accounts.
-- LLM run: has rows in private.observer_model_calls (proxy, before 2026-10-04 09:43:25Z)
--          or private.observer_run_egress (direct calls after the switch).
-- llm_real excludes one team that routed a non-LLM "telemetry-ack" endpoint through the model proxy.
-- actual_tokens = 0 is treated as "failed / no usage reported" (no error text is stored).

-- ===== BASE (prepend to every query) =====
with rt as (select id, name from public.teams where not is_hidden and name not ilike '%(test)%'
              and leader_id not in (select jsonb_array_elements_text(value)::uuid from public.site_settings where key='excluded_accounts')),
calls as (select c.run_id, count(*) calls, count(*) filter (where c.actual_tokens=0) zero_calls,
                 sum(c.actual_tokens) tokens, min(c.created_at) first_call, max(c.created_at) last_call,
                 percentile_cont(0.5) within group (order by extract(epoch from c.settled_at-c.created_at)) p50_lat,
                 string_agg(distinct regexp_replace(p.base_url,'^https?://([^/:]+).*','\1'),',') phost,
                 string_agg(distinct array_to_string(p.models,'/'),',') pmodel
          from private.observer_model_calls c left join private.observer_providers p on p.id=c.provider_id group by 1),
eg as (select run_id, sum(connections) conns, sum(refused) refused, sum(bytes_up) up, sum(bytes_down) down,
              string_agg(distinct host,',') ehost from private.observer_run_egress group by 1),
runs as (select r.id run_id, b.id batch_id, b.team_id, t.name team, ph.slug phase, b.purpose, s.slug card, r.status, r.score,
                b.revision_id, b.model_disabled,
                r.created_at, r.started_at, r.finished_at, (r.created_at >= '2026-10-04 09:43:25+00') post_switch,
                c.calls, c.zero_calls, c.tokens, c.first_call, c.last_call, c.p50_lat, c.phost, c.pmodel,
                e.conns, e.refused, e.up, e.down, e.ehost,
                coalesce(c.phost, e.ehost) host,
                (c.run_id is not null or e.run_id is not null) llm
         from public.observer_runs r join public.observer_batches b on b.id=r.batch_id
         join rt t on t.id=b.team_id join public.phases ph on ph.id=b.phase_id
         join public.scenarios s on s.id=r.scenario_id
         left join calls c on c.run_id=r.id left join eg e on e.run_id=r.id),
vend as (select *, case
   when host ~ 'kimi.com|kimi.ai' then 'Kimi Coding Plan'
   when host ~ 'moonshot' then 'Moonshot API'
   when host ~ 'deepseek.com' then 'DeepSeek 官方'
   when host ~ 'bigmodel' then 'GLM 官方'
   when host is null then null
   when host ~ 'telemetry' then '非LLM(遥测回执)'
   else '第三方中转' end vendor,
 case when coalesce(pmodel,'') ~* 'telemetry' or host ~ 'telemetry' then '非LLM(遥测回执)'
   when host ~ 'kimi|moonshot' or coalesce(pmodel,'') ~* 'kimi|^k3' then 'Kimi/Moonshot'
   when host ~ 'deepseek|taotoken|siliconflow|paratera' or coalesce(pmodel,'') ~* 'deepseek' then 'DeepSeek'
   when host ~ 'bigmodel|bafang' or coalesce(pmodel,'') ~* 'glm' then 'GLM'
   when coalesce(pmodel,'') ~* 'gpt' then 'GPT(经中转)'
   when coalesce(pmodel,'') ~* 'grok' then 'Grok(经中转)'
   when host is null then null else '其他' end family,
 llm and not (coalesce(pmodel,'') ~* 'telemetry' or coalesce(host,'') ~ 'telemetry') llm_real
 from runs)

-- ===== Q1a per phase/purpose =====
select phase, purpose, count(*) runs, count(*) filter (where llm) llm_runs, count(distinct team_id) teams, count(distinct team_id) filter (where llm_real) llm_teams from vend group by 1,2;

-- ===== Q1b daily trend (Beijing date, excl. cancelled) =====
select (created_at at time zone 'Asia/Shanghai')::date d_cst, count(distinct team_id) teams, count(distinct team_id) filter (where llm_real) llm_teams, count(*) runs, count(*) filter (where llm_real) llm_runs from vend where status<>'cancelled' group by 1 order by 1;

-- ===== Q1c headline team counts =====
select count(distinct team_id) teams_any, count(distinct team_id) filter (where llm_real) real_llm_teams, count(distinct team_id) filter (where llm_real and post_switch) post_sw, count(distinct team_id) filter (where llm_real and family='Kimi/Moonshot') kimi, count(distinct team_id) filter (where llm_real and vendor='Kimi Coding Plan') kimi_cp, count(distinct team_id) filter (where llm_real and family='DeepSeek') ds from vend;

-- ===== Q1d model family / channel =====
select family, vendor, count(distinct team_id) teams, count(*) runs, count(*) filter (where post_switch) runs_post, sum(calls) proxy_calls, sum(zero_calls) zero_calls, sum(tokens) tokens, sum(conns) egress_conns from vend where llm group by 1,2 order by 3 desc;

-- ===== Q1e vendor combos per team =====
, tv as (select team_id, string_agg(distinct family,' + ') vs from vend where llm_real group by 1) select vs, count(*) teams from tv group by 1 order by 2 desc;

-- ===== Q2a failure (0-token) rate & latency by vendor =====
select family, vendor, count(distinct team_id) teams, sum(calls) calls, sum(zero_calls) zero, round(100.0*sum(zero_calls)/nullif(sum(calls),0),1) zero_pct, round(sum(tokens)::numeric/nullif(sum(calls)-sum(zero_calls),0)) tok_per_ok_call, round(avg(p50_lat)::numeric,2) p50_lat_s from vend where calls>0 group by 1,2 order by 4 desc;

-- ===== Q2b teams whose calls mostly fail =====
, t as (select team_id, sum(calls) c, sum(zero_calls) z from vend where calls>0 and llm_real group by 1) select count(*) teams, count(*) filter (where z>0.5*c) mostly_fail, count(*) filter (where z=c) all_fail, count(*) filter (where z=0) no_fail from t;

-- ===== Q2c calls/tokens per run by card (proxy era) =====
select phase, card, count(*) runs, percentile_cont(0.5) within group (order by calls) p50_calls, percentile_cont(0.9) within group (order by calls) p90_calls, max(calls) max_calls, round(avg(tokens)) avg_tok, percentile_cont(0.5) within group (order by tokens) p50_tok from vend where calls>0 and llm_real group by 1,2 order by 1,2;

-- ===== Q2d egress per run by card (direct era) =====
select phase, card, count(*) runs, percentile_cont(0.5) within group (order by conns) p50_conns, percentile_cont(0.9) within group (order by conns) p90_conns, round(percentile_cont(0.5) within group (order by (up+down)/1e3)::numeric) p50_kb from vend where conns>0 group by 1,2 order by 1,2;

-- ===== Q3a run-level scores, practice α–δ =====
select card, llm_real, count(*) runs, count(distinct team_id) teams, round(avg(score)::numeric) mean, round(percentile_cont(0.5) within group (order by score)::numeric) median, round(percentile_cont(0.9) within group (order by score)::numeric) p90 from vend where status='scored' and card in ('v4-practice-alpha','v4-practice-beta','v4-practice-gamma','v4-practice-delta') group by 1,2 order by 1,2;

-- ===== Q3b team best complete batch (4 cards), by whether that batch used LLM =====
, bt as (select batch_id, team_id, team, bool_or(llm_real) llm, avg(score) overall, count(*) n, min(created_at) t from vend where phase='practice-projects' and card in ('v4-practice-alpha','v4-practice-beta','v4-practice-gamma','v4-practice-delta') and status='scored' group by 1,2,3 having count(*)=4), best as (select distinct on (team_id) * from bt order by team_id, overall desc) select llm, count(*) teams, round(avg(overall)::numeric) mean, round(percentile_cont(0.25) within group (order by overall)::numeric) p25, round(percentile_cont(0.5) within group (order by overall)::numeric) median, round(percentile_cont(0.75) within group (order by overall)::numeric) p75 from best group by 1;

-- ===== Q3c ever-LLM vs never-LLM teams (best score + effort) =====
, bt as (select batch_id, team_id, team, bool_or(llm_real) llm, avg(score) overall, count(*) n, min(created_at) t from vend where phase='practice-projects' and card in ('v4-practice-alpha','v4-practice-beta','v4-practice-gamma','v4-practice-delta') and status='scored' group by 1,2,3 having count(*)=4), ever as (select team_id, bool_or(llm) ever_llm, max(overall) best, count(*) batches from bt group by 1) select ever_llm, count(*) teams, round(avg(best)::numeric) mean_best, round(percentile_cont(0.5) within group (order by best)::numeric) median_best, round(avg(batches),1) avg_batches from ever group by 1;

-- ===== Q3d within-team paired: best LLM batch vs best non-LLM batch =====
, bt as (select batch_id, team_id, team, bool_or(llm_real) llm, avg(score) overall, count(*) n, min(created_at) t from vend where phase='practice-projects' and card in ('v4-practice-alpha','v4-practice-beta','v4-practice-gamma','v4-practice-delta') and status='scored' group by 1,2,3 having count(*)=4), p as (select team_id, max(overall) filter (where llm) b_llm, max(overall) filter (where not llm) b_no from bt group by 1 having bool_or(llm) and bool_or(not llm)) select count(*) teams, count(*) filter (where b_llm>b_no) llm_better, round(percentile_cont(0.5) within group (order by b_llm-b_no)::numeric) median_diff, round(avg(b_llm-b_no)::numeric) mean_diff from p;

-- ===== Q3e top-12 teams (internal only) =====
, bt as (select batch_id, team_id, team, bool_or(llm_real) llm, avg(score) overall, count(*) n, min(created_at) t from vend where phase='practice-projects' and card in ('v4-practice-alpha','v4-practice-beta','v4-practice-gamma','v4-practice-delta') and status='scored' group by 1,2,3 having count(*)=4), best as (select distinct on (team_id) * from bt order by team_id, overall desc), tv as (select team_id, string_agg(distinct family,'+') fam, sum(calls) calls, sum(conns) conns from vend where llm_real group by 1), cnt as (select team_id, count(*) batches, count(*) filter (where llm) llm_batches from bt group by 1) select rank() over (order by b.overall desc) rk, b.team, round(b.overall::numeric) best, b.llm best_used_llm, c.batches, c.llm_batches, tv.fam, tv.calls proxy_calls, tv.conns egress_conns from best b join cnt c using(team_id) left join tv using(team_id) order by b.overall desc limit 12;

-- ===== Q4a call-rate pattern per run (practice α–δ, proxy era) =====
, x as (select v.*, calls::numeric/s.n_nights r from vend v join public.scenarios s on s.slug=v.card where calls>0 and llm_real and card like 'v4-practice-%') select case when calls<=5 then 'a <=5/run' when r<0.5 then 'b <0.5/night' when r<=1.6 then 'c ~1/night' else 'd >1.6/night' end pattern, count(*) runs, count(distinct team_id) teams, round(avg(score)::numeric,1) avg_score from x group by 1 order by 1;

-- ===== Q4b call timing deciles within run =====
, c as (select least(9,greatest(0,floor(10*extract(epoch from mc.created_at-v.started_at)/nullif(extract(epoch from v.finished_at-v.started_at),0))))::int dec from private.observer_model_calls mc join vend v on v.run_id=mc.run_id where v.llm_real and v.status='scored' and v.card like 'v4-practice-%') select dec, count(*) calls from c group by 1 order by 1;

-- ===== Q5 egress coverage after switch =====
, prev as (select distinct team_id from vend where llm_real and not post_switch) select (team_id in (select team_id from prev)) team_used_llm_before, llm, count(*) runs, count(distinct team_id) teams from vend where post_switch and status='scored' group by 1,2;

-- ===== Q9 with vs without model (本次不提供模型; same team, version and card) =====
select team, revision_id, card,
       count(*) filter (where not model_disabled and status='scored') with_model_runs,
       round(avg(score) filter (where not model_disabled and status='scored')::numeric, 1) with_model_avg,
       count(*) filter (where model_disabled and status='scored') no_model_runs,
       round(avg(score) filter (where model_disabled and status='scored')::numeric, 1) no_model_avg
from vend where purpose='formal' group by 1,2,3
having count(*) filter (where model_disabled) > 0 order by 1,2,3;
