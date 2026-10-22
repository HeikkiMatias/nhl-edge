# 0041. Challenger `conservative_blend`: the market plus B3's shrunk disagreement, fitted at the decision horizon

- Status: Accepted (the owner, 2026-10-22)
- Date: 2026-10-22
- Part of: the review agenda (#250); the challenger is #256, scored under ADR 0038

## Context

The frozen blend (ADR 0027, `reports/live/blend-live-20261007-89ec631.json`) is logit p = a + b_m·logit p_mkt + (b_x + b_u·u)·logit p_B3. Its weights are b_m 0.710, b_x 0.492 (SE 0.168) and b_u 0.203 (SE 0.112). Three of its features are the least supported:
- **The u interaction:** b_u's interval includes 0, yet two standard deviations of u raise B3's weight from 0.49 to 0.77.
- **u is outside its range under v2:** a confirmed starter takes goalie doubt to 0, about 3.5 SDs below its training mean (ADR 0037), so the weights are applied outside the range they were fitted on.
- **The horizon:** it learned at SBR's close, and is applied at Pinnacle's 12:45 ET price (ADR 0030).

Every history season is spent (ADR 0038), so whether these features cost anything can be learned only on future decision days, against a pre-registered alternative.

## Options

1. **The frozen form without u, refitted unpenalized on this season's decision days.** The cost is noise: from the frozen fit's SE of 0.17 on 4,875 games, B3's weight would have an SE near 0.5 on 500 games, so the weights would swing from week to week.
2. **The market plus B3's disagreement, with market-centred priors, refitted weekly on decision days** (chosen). The cost is that its priors are a judgement, fixed here before any reading.
3. **The same, with a = 0 and b_m = 1 fixed, fitting B3's weight alone.** This is simpler, but it rules out the recalibration of the market that history shows: B1's slope was 1.08 to 1.12 at SBR.
4. **The frozen weights with b_u set to 0, no refit.** This is cheap, but it keeps the SBR-close horizon.

## Decision

Option 2, as challenger `conservative_blend`, version 1, registered on the day its code merges.

- **The model:** logit p = a + b_m·logit p_mkt + b_d·(logit p_B3 − logit p_mkt).
  - **p_mkt** is the day's p_b0: Pinnacle's 12:45 ET price, de-vigged multiplicatively (`market/devig.py`, hard rule 3).
  - **p_B3** is the day's p_b3.
  - Both come from the day's run bundle, replayed.
- **The fit:** maximum a posteriori logistic regression with independent normal priors, fixed here:
  - a ~ N(0, 0.05²). B1's intercepts at SBR were −0.03.
  - b_m ~ N(1, 0.15²). B1's slopes were 1.08 to 1.12.
  - b_d ~ N(0, 0.5²): shrunk toward the market. The history's B3 weight, 0.49 to 0.71, lies one prior SD away: allowed, not assumed.
- **Training rows (point in time, hard rule 1):** every earlier v2 ledger row with a prediction (`paper_ledger`, `policy_version` v2) whose result was public before the day's input cutoff. A row comes from a decision day in an earlier ET week (Monday to Sunday), so the fit changes once a week and never sees its own week.
  - The result is the full game, with overtime and the shootout (hard rule 2).
  - v1's days are left out, because their B3 read no confirmed starters.
- **Unfitted:** with fewer than 200 training games, the output carries no probability, under the status `unfitted`. At about 46 predicted v2 games a week (2026-10-14 to 10-20), the first fitted week should be the one from 2026-11-16.
- **Lake reads:** `paper_ledger`, through a new point-in-time reader that keeps rows published before the cutoff, with its leakage test; and `games`, through the existing results reader.
- **Primary measure** (ADR 0038 rule 4): the paired log loss of `conservative_blend` minus the frozen blend, on the games both predicted.
  - **Reported beside it:** the challenger minus B1; its shadow bets' CLV under the frozen rules (#252); and its disagreement's slope on the move to the close (#251).
  - Each gets a weekly block bootstrap interval (hard rule 7), with counts only under 4 weeks, and no verdict before 2027-04-12.

A u interaction comes back only as its own challenger, and only if it beats this one on future games.

## Backtest evidence

None, by design: ADR 0038 spends no history season on a challenger. Its evidence is its outputs from registration on, scored nightly.

**For context only,** from `backtest-20261005-b7639f5` (2021-22, weekly block bootstrap):
- the frozen blend minus B1 was −0.0015 [−0.0043, +0.0018] on E1 and −0.0042 [−0.0078, −0.0007] on E2;
- the blend minus the market-only control was −0.0022 [−0.0054, +0.0011] on E1.

So the blend's gain over the market at SBR is small and uncertain, which is the case for a more conservative challenger.

## Consequences

- **Code:**
  - `live/challengers.py`: the registry entry and the fit;
  - a `paper_ledger` point-in-time reader, with a test in `tests/leakage/`;
  - a status for unfitted rows;
  - each week's fitted weights, saved with the output.
- **Policy:** none. The frozen policy and ADR 0032's verdicts are untouched, and nothing reaches a policy without a new version, its ADR, its freeze and the owner.
- **The report:** the live report's Challengers section lists it, unfitted weeks included. Two challengers are registered, `market_only` and this one.
- **Evidence is slow:** about 21 fitted weeks before the review on 2027-04-12.

## Revisit when

- **Too little to fit:** fewer than 200 v2 games are available by 2026-11-30, for example after a pause in the season. The minimum would then need a new version.
- **A mistaken specification:** a prior or input proves mistaken. That makes a new version with a new registration, never a change to this one.
