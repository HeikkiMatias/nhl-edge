# 0024. Gate 2: B3 goes forward, though the one-time 2025-26 test does not show it beating B2

- Status: Accepted
- Date: 2026-10-03

## Context

Gate 2 (docs/plan.md §1, §6; ADR 0002) asks whether the player layer adds to the team-and-goalie baseline. The test is whether B3 beats B2 on future games, overall and after trades, injuries and lineup changes. That includes the one-time hockey-only test on 2025-26, which is now spent. Hard rule 7 allows no pass or fail on point estimates, so a pass needs the paired interval clear of zero.

The three tests disagree in strength:
- The development seasons and the hockey validation show B3 ahead, with intervals clear of zero.
- 2025-26 shows B3 ahead too, but its interval includes zero.

The verdict decides which model phase 4 compares with the market.

On 2025-26, both models are over-confident: their calibration slopes are 0.49 and 0.55. B2 barely beats a coin flip. An inputs-only check, reading no results, found no data shift: the spread of ΔS, ΔG and Δĝ is within the range of 2021-22 to 2024-25. Lineup projections miss as often as before, and the actual starter gets the usual probability.

## Options

1. **Not shown on 2025-26, B3 goes forward** (chosen). The record says gate 2's wording is not met. B3 still becomes phase 4's model, and the live 2026-27 market test is its real exam. Cost: phase 4 builds on a layer whose gain was confirmed only on seasons we developed and validated on.
2. **Pass on the weight of evidence.** The two earlier tests carry the verdict, and 2025-26 counts as a noisy year that doesn't contradict them. Cost: this calls a pass on an interval that includes zero, against hard rule 7.
3. **Fail.** B2 stays phase 4's model, and B3's inputs become the first suspects (ADR 0023). Cost: it drops a layer that every test puts ahead, on the strength of one season whose interval includes zero.

## Decision

Option 1, chosen by the owner on 2026-10-03.

Every test points the same way, and 2025-26's interval includes the earlier gain of about -0.008. Its inputs show no data fault. So B3 replaces B2 as the model phase 4 compares with B1.

The record states plainly that the one-time test did not confirm the gain on its own. It also notes that the lineup-change subset is too small to judge in every test so far, with 20 to 39 games each.

## Backtest evidence

B3 minus B2, paired log loss, 95% weekly block bootstrap (reports/backtest/runs.csv):

| Test | Run | Games | B3 minus B2 |
| --- | --- | --- | --- |
| Development, E1 (2018-19, 2021-22) | `backtest-20261003-b1a7b04` | 2,583 | -0.0079 [-0.0130, -0.0028] |
| Development, E2 | same | 2,573 | -0.0078 [-0.0130, -0.0028] |
| Hockey validation (2023-24, 2024-25) | `backtest-hockey-20261003-93d0f92` | 2,624 | -0.0078 [-0.0130, -0.0027] |
| One-time 2025-26 | `backtest-hockey-20261003-82fbada` | 1,312 | -0.0030 [-0.0094, +0.0034] |

On 2025-26, by subset:
- trade: -0.0003 [-0.0073, +0.0060] over 845 games;
- injury: -0.0073 [-0.0161, +0.0020] over 847;
- lineup change: +0.0654 [+0.0621, +0.1275] over 20 games in two weeks;
- any: -0.0034 [-0.0103, +0.0033] over 1,114.

**Calibration slope:**

| | Hockey validation | 2025-26 |
| --- | --- | --- |
| B3 | 0.94 [0.80, 1.08] | 0.55 [0.29, 0.80] |
| B2 | 0.83 [0.70, 0.96] | 0.49 [0.24, 0.77] |

**Against the market:** on the development seasons, B3 minus B1 is +0.0033 [-0.0013, +0.0077] on E1 and -0.0000 [-0.0047, +0.0043] on E2. B3 does not yet beat the recalibrated market.

## Consequences

- **Phase 4** compares B3, not B2, with B1. Hard rule 3 still scores the player layer against B2, so B3 minus B2 stays in every backtest report.
- **No held-out season is left for hockey alone.** The next independent evidence is the live 2026-27 market test, scored against both B1 and B2.
- **The model card says gate 2 is not confirmed on 2025-26,** and gives the 2025-26 over-confidence as a known weakness.
- **The lineup-change subset** never reaches 40 games in any test, even over two seasons. A later gate needs a broader definition, decided before results, or must drop it.

## Revisit when

- **Live 2026-27 puts B3 behind B2** (B3 minus B2's interval above zero), or still level at the season's end. Then B2 becomes phase 4's reference again, and B3's inputs are the first suspects (ADR 0023).
- **Or 2026-27 is over-confident like 2025-26** for both models. That points to a shift the inputs don't see, a modeling question beyond this verdict.
