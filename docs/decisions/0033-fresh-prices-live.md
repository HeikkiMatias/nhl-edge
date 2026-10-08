# 0033. Fresh prices live: the decision quote, and the closing proxy's freshness and lead time

- Status: Accepted (by the owner, 2026-10-07)
- Amended: 2026-10-07, the quote's age at publication (accepted by the owner, 2026-10-07)
- Date: 2026-10-07

## Context

Live bets are decided on Pinnacle's 12:45 ET quote (ADRs 0028 and 0030) and judged by CLV against Pinnacle's closing proxy, the last fresh pre-game snapshot (hard rule 5, ADR 0032). Three limits decide which quotes count, so they are fixed before the first live run and the first counted prediction:
- **The decision quote.** The owner ruled on 2026-10-07 that the midday snapshot and the decision both fall between 12:45 and 13:15 ET, and that Pinnacle's quote is at most 5 minutes old at the decision. An ADR records this (plan, task 3).
- **The closing proxy's freshness.** It must be chosen from quote ages alone, never from live closes or CLV, since it decides which bets enter the CLV measure (plan, task 4).
- **The closing proxy's maximum lead time.** It must be chosen from the snapshot schedule alone (ADR 0032, as amended in #179).

**What a quote's age looks like.** The age is `snapshot_utc − last_update_utc`. Pinnacle's h2h quotes in `odds_snapshots` cover 2026-09-28 to 10-07: 556 quotes over 40 snapshots.

| Slot | Quotes | Median age | Largest |
| --- | --- | --- | --- |
| morning (07:05) | 142 | 18 s | 24 s |
| midday (12:45) | 108 | 13 s | 28 s |
| pre7 (18:45) | 154 | 3 s | 8 s |
| pre8 (19:45) | 96 | 7 s | 603 s |
| pre10 (21:45) | 56 | 7 s | 15 s |

The one stale pair came 3 minutes before an Islanders start on 2026-10-03, after Pinnacle had stopped updating the game. That is the case a freshness limit exists for.

**What the snapshot schedule allows.** Pre-game snapshots run at 18:45, 19:45 and 21:45 ET, each when a game starts within 90 minutes. Of the 2,624 regular-season starts in 2024-25 and 2025-26:
- 2,125 (81%) have a pre-game slot within 90 minutes before them;
- 59 more (2%), the 21:30 starts, have one within 120 minutes;
- 440 (17%) have none before them: matinees, and starts before 18:45.

The midday snapshot is the decision quote itself, so it can never be a game's close. A bet's CLV against its own price would measure only the margin.

## Options

1. **The proxy's freshness:** 5 minutes, task 3's limit (the plan's starting point); or a tighter 1 or 2 minutes. On the logged quotes all three give the same result.
2. **The proxy's maximum lead time:** 90 minutes, the pre-game slots' own trigger; or 120 minutes, which adds the 21:30 starts.

## Decision

Chosen by the owner on 2026-10-07:
- **The decision quote** (the owner's rulings, 2026-10-07):
  - The midday snapshot's `snapshot_utc` and the decision fall between 12:45 and 13:15 ET. The check reads the time, not the `midday` label, since a late fallback snapshot keeps the label (`resolve_slot`). Outside the window the day is skipped, never reconstructed.
  - Pinnacle's quote is at most **5 minutes** old at the decision: `prediction_utc − last_update_utc`. The decision instant is fixed when the run starts. A game whose quote is older gets a "no fresh price" row, and no prediction or bet.
- **The closing proxy** of a game is Pinnacle's quote from the latest snapshot that meets all of these:
  - it was taken after the day's decision snapshot and before the game's start;
  - its quote is at most **5 minutes** old at that snapshot, `snapshot_utc − last_update_utc`;
  - it was taken at most **90 minutes** before the start.

  A snapshot that fails the freshness limit is skipped, and an earlier one stands in if it qualifies.
- **When none qualifies**, the bet has no valid proxy:
  - **"no pre-game snapshot"** when no pre-game slot is scheduled within 90 minutes before the start. Such bets fall outside ADR 0032's coverage floor.
  - **"stale"** or **"missing"** otherwise. These count against the floor.
- These limits read quote ages, snapshot times and the schedule only. No live close, CLV or result informed them.

## Backtest evidence

None: history has no Pinnacle quotes or timestamps. The ages and counts above come from `odds_snapshots` and `games` in the lake on 2026-10-07.

## Consequences

- Task 3's `nhl predict` applies the decision rules. Task 4 (#21) derives `is_closing_proxy` in the odds replay under the proxy rules, and its audit reports the lead times.
- About 17% of games, mostly matinees and starts before 18:45, can't be judged by CLV with the current slots. Their bets are still logged and settled, and they are counted apart (ADR 0032).

## Revisit when

- The eligible share of bets falls well short of 81%. More snapshot slots would be a new decision, made before the games they cover.
- Or stale quotes become common in a slot, which would show Pinnacle's feed behaving differently from these first ten days.

## Amendment, 2026-10-07: the quote's age at publication

**Context.** The decision instant is fixed when the run starts, but the ledger is published minutes later, once the lake is pulled and B2 and B3 are read. Since #184 the ledger records that time as `published_utc`, the actual clock. Codex's P0 on #184 pointed out the gap: a run that stalled would still log a bet at a midday quote that was no longer on offer when the decision was published.

**Options:** a limit of 15 or 10 minutes on the quote's age at publication; or none, freshness at the decision only, with the gap recorded and reported.

**Decision,** chosen by the owner on 2026-10-07:
- Pinnacle's quote is also at most **15 minutes** old at publication: `published_utc − last_update_utc`.
- A game whose quote is older gets the same "no fresh price" row, with no prediction or bet.
- The 5-minute limit at the decision stands.
- The clock is read again after the ledger is built and just before the write. If a game has started by then, or its quote has aged past the limit, the day is decided again at the later clock: that game gets its row, and the other games keep theirs.

**Why 15 minutes.** The gap between the decision and publication is expected to be about 3 to 5 minutes on CI, for the lake pull and the B2 and B3 fits. The limit catches a stall without costing an ordinary day. The first live runs' `published_utc` will show the actual gap.

The limit reads clock times and quote ages only. No live close, CLV or result informed it.
