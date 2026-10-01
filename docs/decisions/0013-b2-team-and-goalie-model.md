# 0013. B2: a regularized logistic model on team, goalie and schedule terms, mixing over starters

- Status: Accepted
- Date: 2026-10-01

## Context

B2 is phase 2's model and gate 1's challenger to B1 (docs/plan.md §5, ADR 0002):

P(home win) = σ(β0 + h_s + β1·ΔS + β2·ΔG + β3·ΔR)

Its inputs are built and frozen: team strength ΔS (#74), the goalie-start probabilities (#76, ADR 0012), the goalie effects (#75) and the schedule terms and season home edge (#77). This ADR fixes how B2 combines them before any B2 fit is scored, and leaves only its L2 strength to the tuning protocol (ADR 0011).

Two choices went to the owner:
- whether h_s enters as is or with its own weight;
- which goalies the training games use.

## Options

For the home edge:
1. **As is,** a fixed term as in the plan's formula, so β0 absorbs only what h_s misses.
2. **With its own weight,** which lets the data discount it.

For the training games' goalies:
1. **The starters who played them,** whose boxscores were public long before the fold. B2 learns what a known goalie is worth, and prediction averages that over the likely starters.
2. **The same probability-weighted average as at prediction.** Fitting and prediction then see the same kind of input, but the goalie weight is learned from a blurred input.

## Decision

Option 1 for both, approved by the owner on 2026-10-01.

**Inputs per game,** each row read only once known: its `observed_utc` strictly before the experiment's prediction time, as `known_at` reads.
- ΔS from `team_strength`;
- ΔG, the home goalie's `goals_saved` minus the away goalie's (`goalie_effects`);
- h_s from `schedule_terms`, a fixed term, 0 at a neutral site;
- ΔR from `schedule_terms`: a back-to-back flag for each team, the rest difference (home minus away), the travel difference per 1,000 km, and each team's time-zone change in absolute hours;
- the empty-seat share, 1 minus `capacity_share`, at non-neutral games.

**Fit,** per fold: a logistic regression with an L2 penalty on the inputs' weights, not on β0, with the inputs standardized on the training games. It is trained on every game from 2011-12 whose result and boxscore were public before the fold starts, and whose feature rows were known by then (the tuned seasons' rows from ADR 0011's cutoff). Training games use the goalies who started them. A starter with no `goalie_effects` row known by then, because he was not among the candidates for example, counts as average (0). `train_cutoff` is the last moment any row the fit read became known, and at least B2's tuning cutoff. A fold starting before the latest tuning cutoff, B2's own or a feature table's, is refused.

**Prediction:** the probability averaged over every pair of candidate starters, each pair weighted by the product of the two goalie-start probabilities. A team without candidates counts its goalie as average. A game's own starters are never read (hard rule 9).

**L2 strength:** tuned per ADR 0011 on 2012-13 to 2017-18, with B2 itself as the scored model, over 0.1, 1, 10, 100 and 1,000. A stronger penalty counts as steadier. As ADR 0011 intends, the tuning reuses the frozen settings of ΔS, ΔG and h_s, which were chosen on the same seasons (the owner's decision on #88).

**Reported** by `nhl backtest` on E1 and E2 beside B0 and B1:
- its log loss, and the paired difference against B1 (hard rule 3), pooled and per season;
- its calibration intercept and slope;
- the goalie-start Brier score on the scored games;
- every game where it differs from B1 by more than 8 points, for manual review (hard rule 8).

All intervals are weekly block bootstrap (hard rule 7).

## Backtest evidence

None yet. #78's run of `nhl backtest` on the development seasons gives B2 against B1 for gate 1 (#79).

## Consequences

- **Hard rule 9 governs predictions.** A training game's starter only teaches B2 what a known goalie is worth. Its boxscore was public before the fold, and no game's own lineup feeds its own prediction. Codex read the rule as covering training games too (P0 on #90). The owner kept this design on 2026-10-01, since switching would also change B2 after its development-season results were seen.
- B2's goalie weight is learned from known starters, while its predictions carry the start uncertainty through the mixture. A confirmed starter (the live polls, #42) would replace the mixture by a single pair, with no refit.
- h_s enters at full weight, so a season's home edge cannot be discounted by the fit. The empty-seat input and β0 can still correct it.
- The training seasons' features are in-sample for their tuned settings (ADR 0011). B2 may train on them, but no B2 result on 2011-12 to 2017-18 counts as out-of-sample.

## Revisit when

- **Gate 1 shows B2 miscalibrated** in a way one of these choices explains: for example, a calibration slope well below 1 that the goalie mixture accounts for.
- **Or the player layer (B3)** replaces ΔS and ΔG, so B2's specification is the baseline it must beat (hard rule 3).
