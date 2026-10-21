# 0027. The market blend: B3 and the market in one logistic fit per whole-season fold, with a B2 twin and a market-only control

- Status: Accepted (by the owner, 2026-10-04); amended by 0030 (live bets use E1's fit)
- Date: 2026-10-04

## Context

B3 is level with the market, not ahead of it. B3 minus B1 is +0.0033 [−0.0013, +0.0077] on E1 and −0.0000 [−0.0047, +0.0043] on E2 (ADR 0024). docs/plan.md §5 combines the two in a blend:

logit p = a + b_m·logit p_mkt + (b_x + b_u·u)·logit p_model

It is fitted separately for E1 and E2, on out-of-sample predictions of earlier folds only (hard rule 6). B2 and B3 have those from 2018-19 on. B3 is under-confident on the development seasons (slope 1.34) and near calibrated on hockey validation (0.94), and 2021-22's market was itself under-confident (#13). No choice here rests on the one-time 2025-26 test.

## Options

1. **Folds:**
   - whole seasons, so each season learns from every earlier out-of-sample season;
   - monthly refits, which would also score 2018-19, but on noisy early fits.
2. **Calibration:**
   - the blend's own weights;
   - a separate recalibration of B3 first. That is a straight-line rescale of B3's log-odds, which a and b_x fit anyway.
3. **The fit:**
   - plain maximum likelihood, with nothing to tune;
   - a penalized fit, whose strength would have to be tuned on seen seasons.

## Decision

The first option of each, as in the phase 4 plan approved by the owner on 2026-10-04 (#13):
- **Training rows.** Each tested season's blend learns from the folds of 2018-19 up to the season before it, the flagged 2019-20 and 2020-21 included. The rows are each fold's own scored games, with every input known before the tested fold starts, or the fold is refused. 2018-19 has no earlier fold, so the development test is 2021-22.
- **Three fits per experiment and season,** on the same games, by Newton-Raphson with no penalty:
  - **BLEND,** on B3, the model the policy bets with;
  - **BLEND_B2,** on B2, B3's reference (hard rule 3);
  - **BLEND_MARKET,** a + b_m·logit p_mkt only. This is the control, which shows how much of a gain is only the market recalibrated on these games.
- **Reported** (`market/blend.py`, `backtest/blend.py`):
  - paired log loss against B1, BLEND_B2 and BLEND_MARKET;
  - calibration and the fits;
  - the attribution groups;
  - gaps above 8 points against B1, in `gaps_blend.csv`.

## Backtest evidence

Run `backtest-20261004-6f74840` in reports/backtest/runs.csv, on 2021-22, as paired log-loss differences with 95% weekly block bootstrap intervals:

| BLEND minus | E1 (1,312 games) | E2 (1,306 games) |
| --- | --- | --- |
| B1 (the §1 criterion) | −0.0015 [−0.0043, +0.0018] | −0.0042 [−0.0078, −0.0007] |
| BLEND_B2 | −0.0018 [−0.0040, +0.0005] | −0.0033 [−0.0062, −0.0002] |
| BLEND_MARKET | −0.0022 [−0.0054, +0.0011] | −0.0049 [−0.0087, −0.0011] |

- **Calibration of BLEND on E2:** intercept +0.029 [−0.127, +0.180] and slope 1.17 [0.94, 1.41]. Both include their calibrated values.
- **The E2 fit** (3,213 training games):
  - b_m is 0.49 [0.13, 0.85] and b_x 0.71 [0.30, 1.13];
  - b_u's interval includes 0 (ADR 0026);
  - these intervals come from the fit's own standard errors.
- **Gaps above 8 points against B1:** 17 on E1 and 49 on E2, reviewed by hand in `reports/gaps/blend-gap-review.md`. The review found one swapped SBR opener, and no bug.

## Consequences

- **The blend's historical evidence is one season.** 2022-23's 342 games come once after the freeze (ADR 0025).
- **On E2, the blend beats the opener's recalibrated market, the B2 blend and the control,** each with an interval clear of 0. Its gain is not only recalibration.
- **On E1, against the close, its interval includes 0.**
- **The blend inherits B3's calibration drift** between seasons.

## Revisit when

- b_x changes sign or size sharply between folds.
- Or the market-only control matches the blend.

Any change after the freeze is a new policy version, judged only on games after its own freeze date (plan §5).
