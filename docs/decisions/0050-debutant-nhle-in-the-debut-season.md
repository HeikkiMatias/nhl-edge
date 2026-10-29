# 0050. A debutant's NHLe reaches his prior once his lines are public

- Status: Proposed
- Date: 2026-10-29

## Context

#125, found by the leakage check on #102. A player's league lines are public only from his first boxscore (ADR 0016), and RAPM fixes every player's traits at the season's start (ADR 0020). So for his whole debut season, a debutant's prior reads his age and draft slot only, flagged as having no NHLe. His NHLe first reaches him the season after.

A debutant enters a projected lineup only after his first boxscore (hard rule 9, ADR 0017), which is when his lines turn public. So every rating of him that a prediction reads could already use his NHLe. Only the season-start fixing holds it back.

The owner parked #125 for the next model version on 2026-10-09. The 2026-27 policy is frozen (`policy-20261005-8ec5cf3`), so #125 is decided at the formal review on 2027-04-12, with the other parked changes and with evidence from the training seasons only. This record gives that evidence.

## Options

1. **Keep today's priors.** A debutant waits a season for his NHLe.
2. **a1: refresh his prior.** From the first as-of time after his lines are public, his prior mean reads his NHLe. The season's trait effects and NHLe factors stay fixed. Nothing a season-start fit reads changes, so no debutant shapes the effects or the factors.
3. **a2: refresh his prior and code his rows.** As a1, and his debut-season rows carry his NHLe in the trait columns. Later seasons then learn their effects from debut seasons too, and every player's prior moves with them.
4. **b: date the lines earlier,** at his first appearance on an NHL roster or prospect list.
   - No point-in-time source holds that history. A past season's roster is an after-the-fact view (docs/data-sources.md), the prospect lists are current only, and Elite Prospects' history is paid.
   - It would also change no prediction beyond a2's, since a player is in no projected lineup before his first boxscore.

## Decision

**Proposed: a1, for the model version after the 2027-04-12 review.**
- **The NHLe information is right, and about the right size.** Base's stint residuals regress on a1's change of prediction with a slope near 1 (below), and the interval excludes 0.
- **It improves RAPM's own out-of-sample loss, and the per-game 5v5 xG error,** with intervals that exclude 0.
- **It leaves the season-start fits alone,** so the rule that no future debutant shapes the effects or the factors holds by construction.
- **a2 adds nothing that can be told apart from a1** (a2 minus a1 holds 0), while it moves every player's prior.

The game-level gain is small. The win log loss doesn't move detectably, as expected for players who hold 5% to 7% of the projected 5v5 minutes.

## Backtest evidence

`reports/ratings/debut-nhle-20261029-643cc58.md`, from `scripts/debut_nhle.py`.
- **The fits:** RAPM's 5v5 model, fitted three times with the frozen settings on the training seasons.
- **The scoring:** 2012-13 to 2017-18 (ADR 0011), with 95% weekly block bootstrap intervals. Each figure below is the run's minus base's.
- **The check:** the base fit reproduces the lake's `player_ratings` exactly.

| Measure | a1 | a2 |
| --- | --- | --- |
| Win log loss (the tuning's measure) | −0.00024 [−0.00073, +0.00026] | −0.00022 [−0.00082, +0.00038] |
| 5v5 xG difference, squared error | −0.0012 [−0.0024, −0.0001] | −0.0013 [−0.0027, +0.0002] |
| Each game's own 5v5 stints, squared error per hour | −0.0012 [−0.0020, −0.0004] | −0.0016 [−0.0026, −0.0007] |
| The same, rows with a debutant on the ice | −0.0025 [−0.0043, −0.0007] | −0.0025 [−0.0046, −0.0004] |
| Slope of base's stint residual on the change | 0.92 [0.65, 1.18] | 0.93 [0.67, 1.19] |

- **Who moves:** 113 to 151 skaters a season get an NHLe in their debut season, nearly every debutant (114 to 152). Their games after their lines turn public hold 5.2% to 7.0% of the projected 5v5 minutes.
- **Which way:** their 5v5 offense prior falls by 0.03 to 0.08 xG per hour on average. Today's prior treats a debutant like the reference skater without a recent minor-league season, and the NHLe effects put most debutants below that.
- **a2 against a1:** on the stints, a2 minus a1 is −0.0004 [−0.0011, +0.0001].

The training seasons are in-sample for the frozen settings, which were tuned with today's priors (ADR 0011). The development seasons stay unread until the next version's own evaluation.

## Consequences

- **Code:**
  - `rapm.rate` refreshes the late players' prior means between daily fits.
  - `tests/leakage/` gets the rule: a debutant's lines public mid-season move only his prior, from the first as-of time after; a line public exactly at an as-of time isn't read; the effects, the factors and every earlier rating stay as they were.
- **Live:**
  - `player_league_seasons` is rebuilt only by the yearly refresh on July 2, so in a live season no debutant's lines are in it.
  - a1 needs the table rebuilt before each live `nhl rapm`. That rebuild reads the cached landing pages and makes no request: the ingest fetches a new player's page when he first appears.
- **Tuning and backtest:** the pull (80 hours) was tuned with today's priors. The next version's tuning and backtest run with a1 in place, and B3 reads the ratings as before.
- **b stays open** for a point-in-time prospect source, if one appears.

## Revisit when

- The next version's tuning or backtest shows a1 not helping new players.
- #117's yearly refetch changes the lines a debutant's NHLe reads.
- A point-in-time roster or prospect source appears (option b).
