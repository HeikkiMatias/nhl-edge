# 0006. An SBR opening line counts as public at 10:00 US Eastern on the game date

- Status: Proposed
- Date: 2026-09-29

## Context

The SBR archive (#7) gives each game's opening and closing prices but no time for either. Hard rule 1 needs an `observed_utc` on every input. E2, the historical tradable test (docs/plan.md section 1), predicts at the opener and asks whether the model could have found a bet at that price. So the opener's time is also the feature cutoff for every E2 prediction on 2010-11 to 2022-23. The close is simpler: it is the last price before the start.

A wrong opener time errs in one of two directions:
- **The opener was really posted later**, for example after the starting goalies were known. The price then held more information than the stamp admits. The market looks better than it was, which is conservative for any model edge.
- **The opener was really posted earlier**, such as the evening before, and had moved by the stamped time. E2 would then bet at a stale price with fresher features, which overstates the tradable edge.

## Options

1. **10:00 US Eastern on the game date**, or the start when that is earlier. Last night's results are public by then (10:00 UTC, ADR 0003). Its cost is the stale-price risk when the opener was posted the evening before.
2. **Six hours before each game's start.** Same idea, but the cutoff moves with the start time, so games on one day get different feature cutoffs.
3. **At the start, like the close.** Never early, but it leaves E2 no earlier prediction time than E1, which makes E2 pointless.

## Decision

Option 1, chosen by the owner on 2026-09-29 in the #7 project thread. `sbr_odds.observed_utc` is:
- 10:00 US Eastern on `game_date` for the opener (`OPEN_PUBLIC_AT_ET` and `open_observed_utc` in `ingest/sbr.py`), or `start_utc` when that is earlier;
- `start_utc` for the close.

The schema rejects any price observed after its start and any close not stamped at the start. The rule is the same for every season. Over 14,245 matched games the opener comes a median 9.5 hours before the start (range 0 to 13 hours). Only one game is capped at its start: 2010020024, BOS at PHX in Prague, which started at exactly 10:00 EDT.

## Backtest evidence

None yet. This is a point-in-time convention, not a tuned choice. The first run that relies on it is #10's B0 and B1 for E2 on the development seasons.

## Consequences

- **E2's cutoff:** an E2 prediction on an SBR game runs at the opener's `observed_utc` and reads every other table through its own selector at that time.
- **E1 reads the close at the start.** `known_at` admits only rows observed strictly before the prediction time, so no pre-game prediction ever sees a close. E1, the information test, compares with the close as of the start.
- **Stale-opener risk:** an opener posted the evening before makes E2's edge an upper bound. #10 should size this before trusting an E2 result, for example by how often a game's opener and close differ.
- **Live comparison:** 10:00 ET falls between the live morning (07:05 ET) and midday (12:45 ET) snapshots. Live E2 uses the snapshot's own time, not this rule.
- **Tests:** `tests/leakage/test_sbr_odds.py` covers:
  - no price observed after its start
  - the close unknown until after the start
  - only openers known on game-day morning
  - the 10:00 ET boundary in EDT and EST
  - the cap for an early start

## Revisit when

A source shows when SBR's openers were actually posted. Also when #10 finds that openers often differ from the prices at 10:00 ET, or when an E2 result depends on games whose opener may be stale.
