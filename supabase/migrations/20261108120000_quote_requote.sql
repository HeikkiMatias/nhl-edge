-- The requote record on the dashboard (#412, ADR 0059): quote_days and quote_reports take a third
-- record, kalshi_quotes/requote/1, beside the simulator's and the hold rules'. Its rows are the
-- same sums per game date and variant (the model's fair value at each half-spread, under wait and
-- requote after a starter change, the rule in the hold column), and each night's figures with
-- requote's profit per game less wait's and the starter changes' counts. Sums and counts only:
-- no price, ticker, order book, trade or fill time (#280). Reads stay the dashboard's owners'
-- only; the service role writes, as before.
-- Apply it once. No variable to set: `nhl real quotes --r2 --supabase` already tries the copy
-- each night, says in its log that it waits for this migration, and from the night after copies
-- every requote day kept since 2026-11-09.

alter table public.quote_days drop constraint quote_days_record_check;
alter table public.quote_days add constraint quote_days_record_check
  check (record in ('kalshi_quotes/1', 'kalshi_quotes/holds/1', 'kalshi_quotes/requote/1'));

alter table public.quote_reports drop constraint quote_reports_record_check;
alter table public.quote_reports add constraint quote_reports_record_check
  check (record in ('kalshi_quotes/1', 'kalshi_quotes/holds/1', 'kalshi_quotes/requote/1'));

comment on table public.quote_reports is
  'The simulator''s figures over every kept day as of each night (kalshi_quotes.summary): per '
  'variant its counts and each measure per contract, with its weekly block bootstrap interval '
  'from 4 weeks, counts only before; the hold rules'' paired measure (#385); the requote '
  'record''s paired measure and its starter changes'' counts (ADR 0059).';
