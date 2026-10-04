# 0027. The market blend: B3 and the market in one logistic fit, per whole-season fold, with a B2 twin and a market-only control

- Status: Accepted (by the owner, 2026-10-04, in the phase 4 plan)
- Date: 2026-10-04

## Context

B3 is level with the market, not ahead of it. B3 minus B1 is +0.0033 [-0.0013, +0.0077] on E1 and -0.0000 [-0.0047, +0.0043] on E2 (ADR 0024). Phase 4's question is whether B3 adds information when it is combined with the market. docs/plan.md §5 gives the blend:

logit p = a + b_m·logit p_mkt + (b_x + b_u·u)·logit p_model

It is fitted separately for E1 and E2, and only on out-of-sample predictions from earlier folds (hard rule 6). B2 and B3 have out-of-sample predictions from 2018-19 on, the first fold after the tuning cutoff (ADR 0011). B3 is under-confident on the development seasons (calibration slope 1.34) and close to calibrated on hockey validation (0.94), and 2021-22's market was itself under-confident (#13, findings 1 and 6, and gate 2's finding 1). Everything here is fixed before any blend fit is scored. No choice here rests on the one-time 2025-26 test.

## Options

1. **Folds:**
   - whole seasons, so each season's blend learns from every out-of-sample season before it (chosen);
   - month-by-month refits, which also score 2018-19 from its second month, on noisy early fits.
2. **Calibration:**
   - the blend's own weights (chosen);
   - a separate recalibration of B3 per fold first. A recalibration fitted on earlier folds is a straight-line rescale of B3's log-odds, which the blend's a and b_x fit anyway.
3. **Fit:**
   - plain maximum likelihood (chosen). It has four weights on about 3,200 games, with nothing to tune.
   - a penalized fit, whose strength would have to be tuned on seen seasons.

## Decision

As the phase 4 plan, approved by the owner on 2026-10-04 (#13):
- **Training rows.** Each tested season's blend learns from the out-of-sample predictions of 2018-19 up to the season before it (`seasons.blend_training_seasons`), the flagged 2019-20 and 2020-21 included. The rows are each earlier fold's own scored games: B2's and B3's predictions, B0's de-vigged price under the default method (ADR 0008), u's parts (ADR 0026) and the full-game result.
  - Every input must be known before the tested season's fold starts, or the fold is refused.
  - 2018-19 has no earlier fold, so it has no blend. The blend's development test is 2021-22, trained on 3,221 games of E1.
- **Three fits per experiment and season,** on the same training games, by Newton-Raphson with no penalty:
  - **BLEND,** on B3: the model the policy bets with (ADR 0024).
  - **BLEND_B2,** on B2: B3's reference at the blend level (hard rule 3).
  - **BLEND_MARKET,** a + b_m·logit p_mkt alone. This is the control: the market recalibrated on the same games. It shows how much of any gain is only a recalibration of the market.
- **u** is standardized on the training games (`uncertainty.fit_scale`), so b_x is the model's weight at average doubt.
- **B3 alone** is still reported uncalibrated, with its own calibration.
- **Reported** by `nhl backtest` on E1 and E2, each with a weekly block bootstrap interval (hard rule 7):
  - the blend's log loss and its paired difference against B1 (the §1 criterion), pooled and per season;
  - against BLEND_B2 and against BLEND_MARKET;
  - its calibration;
  - each fit's weights with their model-based standard errors, and the u scale;
  - the attribution groups, which are different favourites from the market, same favourite, a season's first 28 days and the rest (§10; #13 findings 4, 6 and 11);
  - every game where it differs from B1 by more than 8 points, in `gaps_blend.csv`, for manual review (hard rule 8).

## Backtest evidence

The first run with the blend: a local `nhl backtest` on the development seasons at commit `f1286a0`, with figures unchanged since. #144's committed run logs it in `reports/backtest/runs.csv`. The blend is scored on 2021-22 only, since 2018-19 has no earlier fold. All intervals are 95% weekly block bootstrap.

| Paired log-loss difference, 2021-22 | E1 (close, 1,312 games) | E2 (opener, 1,306 games) |
| --- | --- | --- |
| BLEND minus B1 (the §1 criterion) | -0.0015 [-0.0043, +0.0018] | -0.0042 [-0.0078, -0.0007] |
| BLEND minus BLEND_B2 (hard rule 3) | -0.0018 [-0.0040, +0.0005] | -0.0033 [-0.0062, -0.0002] |
| BLEND minus BLEND_MARKET (the control) | -0.0022 [-0.0054, +0.0011] | -0.0049 [-0.0087, -0.0011] |
| BLEND_B2 minus B1 | +0.0003 [-0.0021, +0.0028] | -0.0010 [-0.0041, +0.0023] |
| BLEND_MARKET minus B1 | +0.0008 [+0.0000, +0.0016] | +0.0007 [-0.0001, +0.0014] |

- **Calibration of BLEND** (pooled 2021-22):
  - E1: intercept +0.021 [-0.138, +0.170], slope 1.16 [0.95, 1.39];
  - E2: intercept +0.029 [-0.127, +0.180], slope 1.17 [0.94, 1.41].

  Both include 0 and 1.
- **The fits** (2021-22 fold, 3,221 training games on E1 and 3,213 on E2; ± is the model-based standard error):
  - E1: a -0.071 ± 0.043, b_m 0.58 ± 0.18, b_x 0.59 ± 0.22, b_u +0.22 ± 0.15;
  - E2: a -0.076 ± 0.042, b_m 0.49 ± 0.18, b_x 0.71 ± 0.21, b_u +0.23 ± 0.15.

  The market-only control's b_m is 1.01 on E1 and 1.03 on E2.
- **b_u is positive, against what u was meant to do,** but its interval includes 0 on both experiments. This revisit trigger needs an interval clear of 0, so it doesn't fire.
- **Gaps above 8 points against B1** (hard rule 8): 17 on E1 and 49 on E2, listed in `gaps_blend.csv` for #144's review. B3 alone had 208 in 2021-22 on E1.
- **Attribution** (BLEND minus B1):

  | Group | E1 | E2 |
  | --- | --- | --- |
  | Different favourites | -0.0018 [-0.0166, +0.0128] (150 games) | -0.0102 [-0.0308, +0.0071] (139) |
  | First 28 days | -0.0043 [-0.0173, +0.0034] (181) | -0.0005 [-0.0119, +0.0067] (180) |
  | After 28 days | -0.0010 [-0.0041, +0.0025] | -0.0048 [-0.0089, -0.0003] |

- **What it says:**
  - On E2, the blend beats the opener's recalibrated market, the B2 blend and the control, all with intervals clear of 0, on one season. So its gain is not just a recalibration of the market.
  - On E1, against the close, the interval includes 0.
  - This is one development season of evidence. 2022-23's 342 games come once after the freeze (#145, ADR 0025).

## Consequences

- **New code:** `market/blend.py` (the fit), `backtest/blend.py` (the walk-forward and its report) and their leakage test. `nhl backtest` runs the blend after B0 to B3.
- **One season of development evidence.** The blend's test on history is 2021-22, plus 2022-23's 342 games once at the end (ADR 0025). Its intervals will be wide.
- **The B2 twin keeps hard rule 3 at the level that matters,** inside the combination with the market.
- **The blend inherits B3's calibration drift between seasons.** Weights learned on 2018-19 to 2020-21 rescale B3 the way those seasons needed. The choice of the blend's weights over a separate recalibration rests on arithmetic, not on any season's figures: a recalibration fitted on earlier folds is a straight-line rescale of B3's log-odds, which a and b_x fit anyway.

## Revisit when

- b_x's sign or size moves sharply between folds, or b_u comes out positive with an interval clear of 0 (the model trusted more as doubt grows).
- Or the market-only control matches the blend, so the gain is only recalibration.

Any change is a policy change: it is judged only on games after its new freeze date (plan §5).
