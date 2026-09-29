# 0006. SBR prices are observed at the start; E2 assumes the opener at 10:00 US Eastern

- Status: Proposed
- Date: 2026-09-29

## Context

The SBR archive (#7) gives each game's opening and closing prices but no time for either. Hard rule 1 needs an `observed_utc` on every input. E2, the historical tradable test (docs/plan.md section 1), predicts at the opener and asks whether the model could have found a bet at that price. So E2 needs a time for the opener, and that time is also the feature cutoff for every E2 prediction on 2010-11 to 2022-23. The close is simpler: it is the last price before the start.

Any assumed opener time can be wrong in one of two directions:
- **The opener was really posted later**, for example after the starting goalies were known. Anything reading the table at the assumed time would see a price that did not exist yet. Codex flagged this as a P0 on PR #46 when `observed_utc` itself carried the assumed time.
- **The opener was really posted earlier**, such as the evening before, and had moved by the assumed time. E2 would then bet at a stale price with fresher features, which overstates the tradable edge.

## Options

1. **`observed_utc` at 10:00 US Eastern on the game date.** E2 works directly, but every reader of the table, not only E2, sees openers from 10:00 ET whether or not they were up. This is what Codex flagged.
2. **Two timestamps.** `observed_utc` is the start for every price, the only time each was surely public. A separate `assumed_available_utc` carries 10:00 ET for the opener, and only E2 reads it, through its own selector. The assumption is explicit and cannot leak into other readers.
3. **The opener at the start, with no assumed time.** Nothing is assumed, but there is no historical E2 test; E2 would wait for live snapshots.

A fixed lead of six hours before each start was also considered. It gives games on the same day different feature cutoffs.

## Decision

Option 2, chosen by the owner on 2026-09-29 in the #7 project thread. It replaces option 1, which the owner chose earlier the same day before the Codex review.

- **`sbr_odds.observed_utc` is `start_utc` for every price.** `known_at` therefore shows no SBR price before its game starts. The schema requires `observed_utc == start_utc`.
- **`sbr_odds.assumed_available_utc` is when E2 may bet at the price.** For the close it is `start_utc`. For the opener it is 10:00 US Eastern on `game_date` (`OPEN_ASSUMED_AT_ET` and `open_assumed_utc` in `ingest/sbr.py`), with two limits:
  - It is never before the game's schedule is public (`schedule.observed_utc`, ADR 0005), since the row carries the start. So the Lake Tahoe game 2020020290, re-timed on its game day and public at 20:00 UTC, has its opener assumed at 20:00 UTC, not 15:00 UTC.
  - It is never after the start.

  The schema requires it to be no later than the start, and exactly the start for the close.
- **Only E2 reads it**, through `assumed_available_at(frame, prediction_utc)` in `ingest/sbr.py`. That selector also drops games that have started by the prediction time, as `odds.available_at` does for live quotes, since a pre-game price is no longer executable then. The function name is how a reviewer finds every use of the assumption.

Over 14,245 matched games the assumed opener comes a median 9.5 hours before the start (range 0 to 13 hours). Only one game is capped at its start: 2010020024, BOS at PHX in Prague, which started at exactly 10:00 EDT.

## Backtest evidence

None yet. This is a point-in-time convention, not a tuned choice. The first run that relies on it is #10's B0 and B1 for E2 on the development seasons.

## Consequences

- **E2's cutoff:** an E2 prediction on an SBR game runs at the opener's `assumed_available_utc` and reads every other table through its own selector at that time. An E2 report says its prices rest on this assumption.
- **E1 reads the close at the start.** No pre-game prediction sees a close, through either selector. E1, the information test, compares with the close as of the start.
- **Other readers see nothing early.** A feature, rating or audit that reads `sbr_odds` through `known_at` sees each price only once its game has started.
- **Stale-opener risk:** an opener posted the evening before makes E2's edge an upper bound. #10 should size this before trusting an E2 result, for example by how often a game's opener and close differ.
- **Live comparison:** 10:00 ET falls between the live morning (07:05 ET) and midday (12:45 ET) snapshots. Live E2 uses each snapshot's own time, not this rule.
- **Tests:** `tests/leakage/test_sbr_odds.py` covers:
  - every price observed at its start, so `known_at` shows none before it
  - E2 seeing only openers on game-day morning, no close before the start, and no price once the game has started
  - an opener waiting for a schedule made public after 10:00 ET
  - the 10:00 ET boundary in EDT and EST
  - the cap for an early start
  - schema rejections of an early `observed_utc` or a late assumed time

## Revisit when

A source shows when SBR's openers were actually posted. Also when #10 finds that openers often differ from the prices at 10:00 ET, or when an E2 result depends on games whose opener may be stale.
