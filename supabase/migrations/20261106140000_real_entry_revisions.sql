-- The owner's own revisions of a real bet's entry (ADR 0058, accepted 2026-11-05; #398 step 4):
-- appended from the dashboard's form until the game's start, never an edit. A revision is a
-- complete new entry for the bet (placed, with its price, stake, bet id and time; or not placed,
-- with its reason) or its withdrawal ("undo": the bet is unrecorded again), with a note saying
-- why. The entry and every revision stay; `nhl real sync` copies each revision to R2 once
-- (real/live/revisions/), and the record applies the entry, then its revisions in order, then
-- the weekly check's corrections. From the start on, only the weekly check corrects.
-- The rules hold here, whatever the browser sends:
-- - a revision revises an entry from the form (Kalshi's automatic entries are the pipeline's);
-- - it is made before the game's start;
-- - a placed bet's time is after its alert, before the start and not in the future;
-- - its number, time and author are set here, never taken from the request.
-- Reads and inserts are for the dashboard's owners only; the service role reads, for the sync.

create table public.real_entry_revisions (
  id bigint generated always as identity primary key,
  game_date date not null,
  game_id bigint not null,
  n integer not null check (n >= 1),
  outcome text not null check (outcome in ('placed', 'not placed', 'withdrawn')),
  matched_price numeric(8, 3) check (matched_price > 1),
  stake numeric(10, 2) check (stake > 0),
  bet_id text check (length(btrim(bet_id)) between 1 and 200),
  placed_utc timestamptz,
  reason text check (reason in ('the price fell under the minimum', 'not matched', 'missed it')),
  note text not null check (length(btrim(note)) between 1 and 200),
  revised_utc timestamptz not null default now(),
  revised_by uuid not null default auth.uid(),
  constraint real_entry_revisions_numbered unique (game_date, game_id, n),
  constraint real_entry_revisions_of_an_entry foreign key (game_date, game_id)
    references public.real_entries (game_date, game_id),
  constraint real_entry_revisions_complete check (
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
    or (
      outcome = 'withdrawn'
      and matched_price is null and stake is null and bet_id is null
      and placed_utc is null and reason is null
    )
  )
);

comment on table public.real_entry_revisions is
  'The owner''s revisions of a real (or practice) bet''s entry from the form, until its game''s '
  'start (ADR 0058): each a complete new entry, or its withdrawal, with a note. Never updated; '
  'copied once to R2 (real/live/revisions/), the record.';

create function public.real_revision_checks() returns trigger
  language plpgsql
  security definer
  set search_path = ''
as $$
declare
  alert public.real_alerts%rowtype;
  entry public.real_entries%rowtype;
  last_outcome text;
begin
  select * into alert from public.real_alerts
    where game_date = new.game_date and game_id = new.game_id;
  if not found or alert.status <> 'alerted' then
    raise exception 'no alerted bet for game % on %', new.game_id, new.game_date;
  end if;
  select * into entry from public.real_entries
    where game_date = new.game_date and game_id = new.game_id;
  if not found then
    raise exception 'no entry to revise: record the bet first';
  end if;
  if entry.source = 'kalshi' then
    raise exception 'an automatic Kalshi entry is the pipeline''s, from its fills';
  end if;
  new.revised_utc := now();
  new.revised_by := auth.uid();
  if new.revised_utc >= alert.start_utc then
    raise exception 'the game has started: the weekly check corrects it now';
  end if;
  select outcome into last_outcome from public.real_entry_revisions
    where game_date = new.game_date and game_id = new.game_id
    order by n desc limit 1;
  if new.outcome = 'withdrawn' and last_outcome = 'withdrawn' then
    raise exception 'the entry is already withdrawn';
  end if;
  if new.outcome = 'placed' then
    if new.placed_utc >= alert.start_utc then
      raise exception 'a bet placed at or after its game''s start is not a bet of the policy';
    end if;
    if new.placed_utc < alert.alert_utc then
      raise exception 'placed before its alert';
    end if;
    if new.placed_utc > new.revised_utc then
      raise exception 'placed in the future';
    end if;
  end if;
  -- The next number of the bet's revisions; the unique constraint refuses a race.
  select coalesce(max(n), 0) + 1 into new.n from public.real_entry_revisions
    where game_date = new.game_date and game_id = new.game_id;
  return new;
end;
$$;

revoke all on function public.real_revision_checks() from public, anon, authenticated;

create trigger real_entry_revisions_checks
  before insert on public.real_entry_revisions
  for each row execute function public.real_revision_checks();

alter table public.real_entry_revisions enable row level security;
revoke all on table public.real_entry_revisions from anon, authenticated, service_role;
-- The pipeline reads the revisions, to copy each to R2 once.
grant select on table public.real_entry_revisions to service_role;
-- The dashboard's owners read them and append through the form: only the revision's own
-- columns, never its number, time or author.
grant select on table public.real_entry_revisions to authenticated;
grant insert (game_date, game_id, outcome, matched_price, stake, bet_id, placed_utc, reason, note)
  on table public.real_entry_revisions to authenticated;
create policy real_entry_revisions_owner_reads on public.real_entry_revisions
  for select to authenticated using (public.is_dashboard_owner());
create policy real_entry_revisions_owner_inserts on public.real_entry_revisions
  for insert to authenticated with check (public.is_dashboard_owner());
