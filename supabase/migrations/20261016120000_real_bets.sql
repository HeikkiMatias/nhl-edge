-- Real stakes beside the paper ledger (#268, ADR 0039): the day's alerted bets and the owner's
-- entries from the dashboard's form. R2 stays the record (real/live/): `nhl real alert` inserts
-- the alert rows here, and `nhl real sync` copies each entry to R2 once.
-- - real_alerts: one row per paper bet of a day, alerted with its minimum price and its stake
--   at each price (stakes, a list of {price, stake}), or skipped with the reason in status.
--   Insert-only, by the service role.
-- - real_entries: what the owner did with an alerted bet: placed (the matched price, the stake
--   placed, the venue's bet id and when it was placed) or not placed (with a reason). Inserted
--   by a dashboard owner through the form, one per bet, and never updated or deleted: a mistake
--   is corrected at the weekly check against the venue's statement (nhl real correct).
-- The form's rules hold here, not only in the browser: an entry needs an alerted bet, a second
-- one is refused, and a bet placed at or after its game's start or before its alert is refused.
-- An entry made after the start is accepted and marked late (the owner, 2026-10-16).
-- Reads and the form are for the dashboard's owners only (public.is_dashboard_owner(), from the
-- paper ledger migration, which comes first). anon sees nothing.

create table public.real_alerts (
  game_date date not null,
  game_id bigint not null,
  season integer not null check (season % 10000 = season / 10000 + 1),
  start_utc timestamptz not null,
  home text not null check (home ~ '^[A-Z]{3}$'),
  away text not null check (away ~ '^[A-Z]{3}$'),
  policy_version text not null,
  mode text not null check (mode in ('practice', 'real', 'paused', 'stopped', 'closed')),
  venue text not null,
  status text not null,
  side text not null check (side in ('home', 'away')),
  paper_price numeric(8, 3) not null check (paper_price > 1),
  min_price numeric(8, 3) not null check (min_price > 1 and min_price <= paper_price),
  max_stake numeric(10, 2) not null check (max_stake >= 0),
  bankroll numeric(10, 2) not null check (bankroll > 0),
  stakes jsonb not null check (jsonb_typeof(stakes) = 'array'),
  alert_utc timestamptz not null,
  created_at timestamptz not null default now(),
  primary key (game_date, game_id),
  constraint real_alerts_home_is_not_away check (home <> away),
  -- Only practice and real days alert a bet, before its game, with a stake.
  constraint real_alerts_alerted_before_the_start check (
    status <> 'alerted'
    or (mode in ('practice', 'real') and alert_utc < start_utc and max_stake > 0)
  )
);

comment on table public.real_alerts is
  'Each paper bet of a day as alerted for a real (or practice) stake after the 12:45 ET '
  'decision, mirrored from R2 (real/live/alerts/). stakes lists the stake at each price from '
  'min_price to the paper price; above it the stake is max_stake.';

create table public.real_entries (
  id bigint generated always as identity primary key,
  game_date date not null,
  game_id bigint not null,
  outcome text not null check (outcome in ('placed', 'not placed')),
  matched_price numeric(8, 3) check (matched_price > 1),
  stake numeric(10, 2) check (stake > 0),
  bet_id text check (length(btrim(bet_id)) between 1 and 200),
  placed_utc timestamptz,
  reason text check (reason in ('the price fell under the minimum', 'not matched', 'missed it')),
  entered_utc timestamptz not null default now(),
  late boolean not null default false,
  entered_by uuid not null default auth.uid(),
  -- One entry per bet: a second is refused.
  constraint real_entries_one_per_bet unique (game_date, game_id),
  constraint real_entries_of_an_alert foreign key (game_date, game_id)
    references public.real_alerts (game_date, game_id),
  constraint real_entries_complete check (
    (
      outcome = 'placed'
      and matched_price is not null and stake is not null and bet_id is not null
      and placed_utc is not null and reason is null
    )
    or (
      outcome = 'not placed'
      and matched_price is null and stake is null and bet_id is null
      and placed_utc is null and reason is not null
    )
  )
);

comment on table public.real_entries is
  'What the owner did with each alerted bet, from the dashboard''s form; copied once to R2 '
  '(real/live/entries/), the record. Never updated: corrections are appended in R2.';

-- The form's rules, whatever the browser sends: the bet was alerted; a placed bet was placed
-- after its alert, before its game's start and not in the future. The entry's time, its lateness
-- and its author are set here, never taken from the request.
create function public.real_entry_checks() returns trigger
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
  new.entered_utc := now();
  new.entered_by := auth.uid();
  new.late := new.entered_utc >= alert.start_utc;
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

create trigger real_entries_checks
  before insert on public.real_entries
  for each row execute function public.real_entry_checks();

alter table public.real_alerts enable row level security;
alter table public.real_entries enable row level security;
revoke all on table public.real_alerts, public.real_entries from anon, authenticated, service_role;

-- The pipeline inserts the alerts and reads the entries.
grant select, insert on table public.real_alerts to service_role;
grant select on table public.real_entries to service_role;

-- The dashboard's owners read both and insert entries through the form: only the entry's own
-- columns, never its time, lateness or author.
grant select on table public.real_alerts, public.real_entries to authenticated;
grant insert (game_date, game_id, outcome, matched_price, stake, bet_id, placed_utc, reason)
  on table public.real_entries to authenticated;
create policy real_alerts_owner_reads on public.real_alerts
  for select to authenticated using (public.is_dashboard_owner());
create policy real_entries_owner_reads on public.real_entries
  for select to authenticated using (public.is_dashboard_owner());
create policy real_entries_owner_inserts on public.real_entries
  for insert to authenticated with check (public.is_dashboard_owner());
