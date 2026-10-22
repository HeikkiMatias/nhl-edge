# 0042. Challenger `goalie_spread_blend`: goalie doubt measured by its effect on B3

- Status: Proposed
- Date: 2026-10-22
- Part of: the review agenda (#250). The challenger is #257, scored under ADR 0038 against `conservative_blend` (ADR 0041).

## Context

u's goalie part is 1 − the likeliest starter's p_start (ADR 0026). It scores two different cases alike:
- a 50/50 choice between two goalies of equal effect, which barely moves B3;
- a 50/50 choice between a strong starter and a weak backup, which leaves B3 genuinely unsure.

B3 already predicts by mixing over goalie scenarios: each pair of candidate starters, weighted by their start probabilities (`b3.scenarios`). The spread of its log-odds across those scenarios measures the doubt in the prediction's own units.

The doubt is still there at v2's decision. Of v2's 46 predicted games from 2026-10-15 to 10-21:
- 11 had both starters confirmed;
- 13 had only Likely calls (goalie doubt under 0.05);
- 22 had more doubt.

These counts come from the ledger's `goalie_doubt`; no outcome was read.

Skater doubt can't be measured the same way cheaply. B3 reads each skater's expected minutes (p_available × minutes), not lineup scenarios.

## Options

1. **The spread as an interaction on B3's disagreement, inside `conservative_blend`** (chosen). It adds one term to a fitted challenger. The cost is a prior for the new term, set here as a judgement.
2. **The spread in place of u's goalie part, in the frozen blend's form.** This brings back the u interaction and the SBR-close horizon that ADR 0041 set aside.
3. **A fixed, unfitted shrink of B3's disagreement by the spread.** There is nothing to fit, but its size would be a guess.
4. **The spread as a measurement only.** This is cheap, but it can't show whether the spread improves a prediction.

## Decision

Option 1, as challenger `goalie_spread_blend`, version 1, registered on the day its code merges.

- **The spread s:** for each slate game at the decision, the probability-weighted standard deviation of B3's log-odds over its goalie scenarios.
  - s = √(Σ w_k (η_k − η̄)²), where η̄ = Σ w_k η_k and the scenario weights w_k sum to 1.
  - s is 0 when both starters are confirmed. It is near 0 for a Likely call, or when the candidates have similar effects.
  - **The inputs:** the day's run bundle alone. That is its goalie rows, with v2's starters in place (ADR 0037), and the saved B3 fit, read through B3's own scenarios.
- **The model:** logit p = a + b_m·logit p_mkt + (b_d + b_s·s/0.1)·(logit p_B3 − logit p_mkt).
  - a, b_m and b_d keep ADR 0041's priors.
  - **b_s ~ N(0, 0.5²):** for each 0.1 of spread (about 2.5 points of probability near 50%), B3's weight moves by about 0.5 at one prior SD, in either direction.
- **The fit:** maximum a posteriori, as for `conservative_blend`, on the same training rows: earlier weeks' v2 decisions, cut at the week's fold start (ADR 0041). It needs at least 200 games, or the week is unfitted. Games without a stored spread are left out and counted.
- **The spreads of training games:** each v2 decision day's spreads are computed once from its complete bundle and written once (`challengers/inputs/goalie_spread/<date>.parquet`).
  - This includes days before the registration, since spreads are inputs, not evidence.
  - A reader keeps the days before the fold start, and `check()` recomputes them from their bundles.
- **The inputs-only reading,** before registration, reported on #257, with no outcome read:
  - s's distribution on v2's decision days;
  - its rank correlation with u's goalie part;
  - the ten games where the two disagree most.

  It changes nothing fixed here.
- **Primary measure** (ADR 0038 rule 4): the paired log loss of `goalie_spread_blend` minus `conservative_blend`, on the games both predicted.
  - **Reported beside it:** the same on the games with s > 0, and what every challenger gets: the differences from the frozen blend and B1, the shadow bets' CLV, and the disagreement's slope.
  - Each gets a weekly block bootstrap interval (hard rule 7), with counts only under 4 weeks, and no verdict before 2027-04-12.

## Backtest evidence

None, by design (ADR 0038). **For context only:**
- **The frozen blend's u weight:** b_u was +0.222 [−0.065, +0.509] on E1 in 2021-22 (95% from the fit's standard errors, `reports/backtest/summary.json`; ADR 0026). No blend without u was run, so u's own effect was never isolated.
- **History's goalie doubt:** it averaged 0.30 on history's model p_start, with no confirmations (`summary.json`). At v2's decisions so far it averages 0.10.

## Consequences

- **Code:**
  - `game/b3.py`: each goalie scenario's log-odds, beside the mix B3 already computes;
  - `live/challengers.py`: the spread, the registry entry, and `conservative_blend`'s fit with the fourth term;
  - the write-once spreads, written by the runner for every complete v2 bundle without them, with their reader and a test in `tests/leakage/`.
- **The live report:** this challenger is also compared with `conservative_blend`, its named baseline.
- **Policy:** none, as for every challenger.
- **Evidence is slow:**
  - it starts when `conservative_blend` does, about the week of 2026-11-16;
  - the term matters only on games with s > 0, about half of them so far.

## Revisit when

- **Too little spread to learn from:** fewer than 100 training games by 2026-12-31 have s ≥ 0.05, for example because starters come to be confirmed before 12:45 ET as a rule.
- **A mistaken specification:** that makes a new version with a new registration, never a change to this one.
