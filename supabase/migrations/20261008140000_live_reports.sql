-- The live report for the dashboard (#168): `nhl live report --supabase` upserts each report's
-- JSON here (src/nhl_edge/live/report.py), so the dashboard shows the report's own figures,
-- every one with its weekly block bootstrap interval, rather than computing any itself. The
-- committed reports/live/report-<date>.json stay the record.
-- Reads are for the dashboard's owners only (public.is_dashboard_owner, the paper ledger
-- migration); the service role writes.

create table public.live_reports (
  as_of date primary key,
  kind text not null check (kind in ('interim', 'formal review')),
  policy_version text not null,
  report jsonb not null,
  code_version text not null,
  created_at timestamptz not null default now()
);

comment on table public.live_reports is
  'The live report (nhl live report) as of each date, as JSON: coverage, CLV with its interval, '
  'the model comparisons, calibration and the alerts. Interim until the formal review.';

alter table public.live_reports enable row level security;
revoke all on table public.live_reports from anon, authenticated, service_role;
-- A day's report is recomputed as its settlements come in, so the service role may replace it.
grant select, insert, update on table public.live_reports to service_role;
grant select on table public.live_reports to authenticated;
create policy live_reports_owner_reads on public.live_reports
  for select to authenticated using (public.is_dashboard_owner());
