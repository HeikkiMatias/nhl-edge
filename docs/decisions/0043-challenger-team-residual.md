# 0043. Challenger `team_residual_blend`: a shrunk, decaying team effect on what B3 leaves unexplained

- Status: Proposed
- Date: 2026-10-22
- Part of: the review agenda (#250). The challenger is #258, scored under ADR 0038 against `conservative_blend` (ADR 0041).

## Context

B3 builds a team's strength from its projected players (ADR 0023). It has no term for a team that keeps doing better or worse than its players predict, through coaching, deployment, tactics, or attributes the ratings miss. The plan names this a phase 6 add-on, fitted last and only on what B3 leaves unexplained (plan §5), so that teams are never counted twice.

ADR 0036 tested B2's team strength ΔS as a B3 input. It double counts the players, and it didn't pass. Its option 2, team form against B3, was deferred, because it needs B3's own out-of-sample residuals within the season.

Live, the ledgers now hold exactly those. Each predicted game's `p_b3` was published before the game, by a B3 fitted on earlier seasons only, and its full-game result follows.

## Options

1. **Each team's effect on B3's log-odds, fitted weekly on this season's v2 residuals with a fixed prior and half-life, inside `conservative_blend`** (chosen). The cost is that the prior and the half-life are judgements, fixed here.
2. **The same effect added to B3 alone, scored against B3.** That isolates the hockey model. But it doesn't ask whether the term adds anything beyond the market, which is what a policy needs.
3. **A separate fitted weight on the team effect inside the blend.** This adds a term the 200-game fit can barely estimate, beside the effect's own shrinkage.
4. **Seeding the effects with last season's residuals.** Rosters, coaches and systems change over the summer. Every team starts the season at 0.

## Decision

Option 1, as challenger `team_residual_blend`, version 1, registered on the day its code merges.

- **The team effects θ:** each ET week's, at its fold start (Monday 12:45 ET, ADR 0041).
  - They are found by maximum a posteriori from logit P(home win) = logit p_B3 + θ_home − θ_away.
  - **The games:** `conservative_blend`'s training rows for that week (ADR 0041). These are v2's predicted decisions of earlier weeks, with their results public before the fold start.
  - Each game is weighted 0.5^(age / 42 days), its age counted from its date to the fold start.
  - **The prior:** independent θ_t ~ N(0, 0.2²), and every team starts the season at 0.
- **The model:** `conservative_blend` (ADR 0041), with B3 corrected by the effects:
  - logit p_B3′ = logit p_B3 + θ_home − θ_away;
  - logit p = a + b_m·logit p_mkt + b_d·(logit p_B3′ − logit p_mkt);
  - the same priors, the same training rows and the 200-game minimum, or the week is unfitted.
- **Point in time (hard rule 1):** each row uses the effects of its own week's fold start.
  - For a training row, that is the week it was decided in, so no game's own result ever corrects its own B3.
  - The slate uses the effects of the day's week.
- **Lake reads:** `paper_ledger` and `games`, through their existing readers. There is no new table.
- **Saved with each output:** the week's effects, by team, beside the fit.
- **Primary measure** (ADR 0038 rule 4): the paired log loss of `team_residual_blend` minus `conservative_blend`, on the games both predicted.
  - **Reported beside it:** what every challenger gets: the differences from the frozen blend and B1, the shadow bets' CLV and the disagreement's slope.
  - Each gets a weekly block bootstrap interval (hard rule 7), with counts only under 4 weeks, and no verdict before 2027-04-12.

**The cost of caution:** the term moves slowly by design. Take a team that is truly 0.2 log-odds better than B3 rates it, about 5 points of probability near 50%. With about 3.5 games a week, it would show θ of about +0.03 by mid-December, and at most about +0.05 later in the season.

## Backtest evidence

None, by design (ADR 0038). **For context only:** ADR 0036's team strength term, B3 with ΔS minus B3, was +0.00034 [−0.00138, +0.00198]. That is the pooled paired log loss with its 95% weekly block bootstrap interval over 2018-19, 2021-22, 2023-24 and 2024-25 (`reports/backtest/hockey-20261010-a6e67dd-210612.json`). So a team term built from B2's results added nothing measurable to B3.

## Consequences

- **Code:** `live/challengers.py` gains:
  - the weekly team effects, reusing `conservative_training`'s rows;
  - the corrected B3;
  - the registry entry, with `conservative_blend/1` as its baseline.
- **Tests:** a leakage test in `tests/leakage/`. A week's effects read only games before its fold start, and a training row uses its own week's effects.
- **Policy:** none, as for every challenger.
- **Evidence is slow:**
  - it starts when `conservative_blend` does, about the week of 2026-11-16;
  - the effects stay small until mid-season.

## Revisit when

- **The term is inert:** by 2026-12-31, no team's effect exceeds 0.02. The prior would then be too tight to test anything, which would take a new version.
- **A mistaken specification:** that makes a new version with a new registration, never a change to this one.
