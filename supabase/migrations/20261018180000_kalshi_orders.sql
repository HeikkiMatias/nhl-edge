-- Kalshi's automatic bets on the dashboard (#287, ADR 0040). Apply it before the venue commit
-- makes Kalshi the real bets' venue: from then on the alerts carry each bet's order, and the
-- pipeline records each bet's entry from its fills.
-- - real_alerts.exchange: an alerted bet's buy order on an exchange venue (its environment, its
--   limit in dollars, contracts, most cost in USD and in euros, effective price, rate and expiry),
--   null on any other venue and for a skipped bet. Written by `nhl real alert` and `sync`.
-- - real_entries.source: 'form' for the owner's entries from the dashboard, as before, or
--   'kalshi' for an automatic bet's entry, inserted by the pipeline (`nhl real sync`) from the
--   record in R2, which is written first. Only the service role can set it: the form's grant
--   leaves it out. An automatic entry keeps the pipeline's time, is never late (its order was
--   written before the start) and has no author. The form's other rules hold for it too.

alter table public.real_alerts
  add column exchange jsonb,
  add constraint real_alerts_exchange_is_an_alerted_order check (
    exchange is null or (jsonb_typeof(exchange) = 'object' and status = 'alerted')
  );

comment on column public.real_alerts.exchange is
  'An alerted bet''s buy order on an exchange venue (ADR 0040): env, limit, contracts, '
  'max_cost_usd, stake, effective, usd_per_eur, expires_utc. Null otherwise.';

alter table public.real_entries
  add column source text not null default 'form',
  add constraint real_entries_source check (source in ('form', 'kalshi'));
alter table public.real_entries alter column entered_by drop not null;
alter table public.real_entries
  add constraint real_entries_author check (source = 'kalshi' or entered_by is not null);

comment on column public.real_entries.source is
  '''form'': the owner''s entry from the dashboard. ''kalshi'': an automatic bet''s entry, '
  'from its fills, inserted by the pipeline from the record in R2 (ADR 0040).';

-- The form's rules, as before, and an automatic entry's: its time is the pipeline's, it is never
-- late, and it has no author.
create or replace function public.real_entry_checks() returns trigger
  language plpgsql
  security definer
  set search_path = ''
as $$
declare
  alert public.real_alerts%rowtype;
begin
  select * into alert from public.real_alerts
    where game_date = new.game_date and game_id = new.game_id;
  if not found or alert.status <> 'alerted' then
    raise exception 'no alerted bet for game % on %', new.game_id, new.game_date;
  end if;
  if new.source = 'kalshi' then
    new.entered_by := null;
    new.late := false;
  else
    new.entered_utc := now();
    new.entered_by := auth.uid();
    new.late := new.entered_utc >= alert.start_utc;
  end if;
  if new.outcome = 'placed' then
    if new.placed_utc >= alert.start_utc then
      raise exception 'a bet placed at or after its game''s start is not a bet of the policy';
    end if;
    if new.placed_utc < alert.alert_utc then
      raise exception 'placed before its alert';
    end if;
    if new.placed_utc > new.entered_utc then
      raise exception 'placed in the future';
    end if;
  end if;
  return new;
end;
$$;

revoke all on function public.real_entry_checks() from public, anon, authenticated;

-- The pipeline inserts the automatic entries, source included; the form's grant stays as it was,
-- without source, so an entry from the dashboard is always 'form'.
grant insert on table public.real_entries to service_role;
