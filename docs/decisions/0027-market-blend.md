# 0027. The market blend: B3 and the market in one logistic fit, per whole-season fold, with a B2 twin and a market-only control

- Status: Accepted (by the owner, 2026-10-04, in the phase 4 plan)
- Date: 2026-10-04

## Context

B3 is level with the market, not ahead of it. B3 minus B1 is +0.0033 [-0.0013, +0.0077] on E1 and -0.0000 [-0.0047, +0.0043] on E2 (ADR 0024). Phase 4's question is whether B3 adds information when it is combined with the market. docs/plan.md §5 gives the blend:

logit p = a + b_m·logit p_mkt + (b_x + b_u·u)·logit p_model

It is fitted separately for E1 and E2, and only on out-of-sample predictions from earlier folds (hard rule 6). B2 and B3 have out-of-sample predictions from 2018-19 on, the first fold after the tuning cutoff (ADR 0011). B3 is under-confident on the development seasons (calibration slope 1.34), its calibration moves between seasons (0.94 on hockey validation, 0.55 on 2025-26), and 2021-22's market was itself under-confident (#13, findings 1 and 6). Everything here is fixed before any blend fit is scored.

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

None yet. The first `nhl backtest` run with the blend on the development seasons provides it (#140).

## Consequences

- **New code:** `market/blend.py` (the fit), `backtest/blend.py` (the walk-forward and its report) and their leakage test. `nhl backtest` runs the blend after B0 to B3.
- **One season of development evidence.** The blend's test on history is 2021-22, plus 2022-23's 342 games once at the end (ADR 0025). Its intervals will be wide.
- **The B2 twin keeps hard rule 3 at the level that matters,** inside the combination with the market.
- **The blend inherits B3's calibration drift.** Weights learned on 2018-19 to 2020-21 rescale B3 the way those seasons needed.

## Revisit when

- b_x's sign or size moves sharply between folds, or b_u comes out positive with an interval clear of 0 (the model trusted more as doubt grows).
- Or the market-only control matches the blend, so the gain is only recalibration.

Any change is a policy change: it is judged only on games after its new freeze date (plan §5).
