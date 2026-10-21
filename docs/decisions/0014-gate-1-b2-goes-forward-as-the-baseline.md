# 0014. Gate 1: B2 does not beat the market on its own, and goes forward as the baseline

- Status: Proposed
- Date: 2026-10-21

## Context

Gate 1 (docs/plan.md §1, §6; ADR 0002) scores B2, the team and goalie model (ADR 0013), against B1, the recalibrated market. The measure is the paired log-loss difference on the development seasons, on E1 and E2. Gate 1 is a checkpoint, not a stop: the player layer is built either way.

The verdict waited for #30, because ADR 0004 left post-game corrections unmeasured and B2 is the first model built on shot records and ice time. Meanwhile phases 3 and 4 went ahead: gate 2 is ADR 0024 and gate 3 is ADR 0031. So this records gate 1 after the fact. B2 and its inputs haven't changed since the frozen run, `backtest-20261005-b7639f5`, which gives the same figures as gate 1's first run, `backtest-20261001-388bb85`.

## Options

1. **Record that B2 adds nothing beyond the market on its own, and keep it as the baseline** (chosen). B2 stays B3's reference (hard rule 3), and nothing in it changes.
2. **Recalibrate B2 per fold now.** It is under-confident. Cost: this changes the reference that gates 2 and 3 were judged against, while the market blend (ADR 0027) already corrects calibration out of sample.
3. **Hold gate 1 until #173 measures corrections at the level of predictions.** Cost: phase 2 stays open on a measure the owner judged small (ADR 0004, 2026-10-21).

## Decision

Option 1, after the owner's call on 2026-10-21 to keep ADR 0004 and run gate 1.
- **The result:** B2 is worse than B1, and its pooled intervals lie above zero on both E1 and E2. That is expected of a model that reads no price, and no pass or fail is read from point estimates (hard rule 7).
- **No bug:** the hand review of its gaps found none.
- **Calibration:** B2's under-confidence is left to the blend, never fixed by retuning B2 on these seasons.

## Backtest evidence

B2 minus B1, paired log loss, 95% weekly block bootstrap, run `backtest-20261005-b7639f5` (reports/backtest/summary.json, runs.csv):

| | Games | Pooled | 2018-19 | 2021-22 |
| --- | ---: | --- | --- | --- |
| E1 | 2,583 | +0.0112 [+0.0044, +0.0185] | +0.0077 [−0.0011, +0.0180] | +0.0146 [+0.0050, +0.0245] |
| E2 | 2,573 | +0.0078 [+0.0013, +0.0146] | +0.0055 [−0.0028, +0.0154] | +0.0100 [−0.0004, +0.0201] |

- **Calibration slope (E1):**
  - pooled 1.35 [1.09, 1.62], so B2 is under-confident;
  - 2018-19: 1.06 [0.59, 1.59];
  - 2021-22: 1.55 [1.25, 1.86].
- **Goalie starts:** Brier 0.415 [0.404, 0.426]. 0.9% of the actual starters were not candidates.
- **Gaps above 8 points (hard rule 8):**
  - 822 of 2,583 games on E1 and 775 of 2,573 on E2;
  - an automated screen of every gap, then a hand review of 153 games (the flagged ones, every gap above 20 points, and a seeded sample of 40);
  - no bug was found (reports/backtest/gap-review.md).
- **Post-game corrections (#30):**
  - 89 games of 2026-27 were fetched again a week after their first copy;
  - 39 of them changed beyond event order;
  - those changes touch about 1% of shot rows and 1.7% of lineup rows, and no goal's scorer;
  - shift charts were compared in 41 of the 89 games.

## Consequences

- Phase 2 closes: #79 and #11.
- **B2's place:** it stays the reference for the player layer in every backtest report (hard rule 3).
- **The model card:** it gets the gate 1 entry.
- **ADR 0004:** it records #30's measurement.
- **#173:** it stays open for the measure at the level of predictions.
- `nhl gap-review` stays available for later reviews of gaps.

## Revisit when

- #173 shows that corrections change predictions, or which bets the policy selects, by more than its interval leaves to chance.
- Or B2's inputs or specification change, which would need a new run of gate 1.
