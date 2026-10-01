# 0011. Tuning protocol: settings chosen on the training seasons by later games' results, then frozen

- Status: Accepted
- Date: 2026-10-01

## Context

Phase 2's components have settings that a fold's fit cannot estimate on its own (docs/plan.md §5, #11):
- team strength's memory and its pull toward the league average (#74);
- the goalie effect's (#75) and the schedule terms' (#77) equivalents;
- B2's L2 strength (#78).

A setting chosen while looking at the development seasons (2018-19 and 2021-22) would shape gate 1's evidence. The approved phase 2 plan therefore chooses every such setting by a walk-forward on the training seasons' outcomes, then freezes it. That plan asked for this ADR the first time the protocol is used, which is team strength (#74). Two choices went to the owner:
- what a setting is judged on;
- what happens when two settings score within noise of each other.

## Options

What a setting is judged on:
1. **Predicting winners.** Log loss of later games' full results, overtime and shootout included: the yardstick of gate 1 and of the bets. It is noisy.
2. **Predicting scoring chances.** Error in each team's later share of xG. A steadier signal, one step removed from winning.

Near-ties:
1. **The steadier setting.** It is fixed in advance per component, for example a longer memory and more pull toward the average.
2. **The best number,** however small the gap.

## Decision

Option 1 for both, chosen by the owner on 2026-10-01.

**Where.** The training seasons only (`TRAINING_SEASONS`, 2010-11 to 2017-18). A season is scored when an earlier one has the component's inputs to fit on. The flagged seasons (2019-20 and 2020-21), the development seasons and the held-out seasons are never scored while tuning.

**How a candidate is scored:**
1. The component computes its feature for every game from earlier games only, point in time, as it will in the backtest.
2. For each scored season, a small logistic model is fitted on the earlier seasons' games whose results were public before the season's first game: the home team's full-game win, overtime and shootout included (hard rule 2), against an intercept and the feature.
3. The fitted model predicts the season's games, and each game's log loss is kept.

**The grid** is small and fixed before the first run, in the component's code and ADR or PR.

**The choice:**
- The candidate with the lowest pooled log loss leads.
- Every candidate whose paired difference against the leader has a weekly block bootstrap interval holding zero ties with it (hard rule 7).
- Among the leader and its ties, the steadiest wins, by an order fixed in advance for the component.

**Frozen.** The chosen values become constants in the component's code, with the tuning run's version.
- The development seasons are scored for gate 1 without retuning, and live uses the same values.
- Retuning needs a new ADR.

**Logged.** Each run writes every candidate's pooled and per-season log loss, its paired difference against the leader with its interval, and the choice to `reports/tuning/<component>-<version>.md`. The component's PR quotes it.

**Team strength's application (#74):**
- **Grid:** memory as a half-life of 10, 20, 40 or 80 games, and a pull toward the league average worth 0, 10, 20 or 40 games. That is 16 candidates.
- **Steadiness order:** the longer half-life first, then the larger pull.
- **The run** (`team-strength-20261001-9dc689a`, quoted on #74's PR) scored 2012-13 to 2017-18.
  - **Chosen:** a half-life of 80 games and a pull worth 40 games, log loss 0.6781 [0.6743, 0.6818]. It was also the leader.
  - **Ties:** settings with any pull tied with it. Settings with no pull did not, at +0.0014 to +0.0025.
  - **The grid's edge:** the leader sits on the grid's steadiest corner, so a longer memory might do a little better. The owner chose to keep the grid as fixed and freeze this result, since the gaps are within noise and widening the grid would take a second look at the same seasons.

## Backtest evidence

None yet. Team strength's tuning run is on #74's PR, and gate 1 (#79) scores B2 on the development seasons with the frozen values.

## Consequences

- The training seasons' tuned features are in-sample for the choice, as 2012-13 to 2017-18 xG is for the rebound term (ADR 0010). They may train later models, but no result may present them as out-of-sample. Every fold from 2018-19 on uses settings chosen entirely before it.
- **Every tuned output records the run's cutoff,** the last result the run read: `train_cutoff` in `team_strength`, 2018-04-09 10:00 UTC. B2's walk-forward (#78) must not score a fold that starts before it as out-of-sample. Gate 1's folds start after it.
- One choice per component, made once. A component tuned later does not reopen an earlier one's settings.
- B2's L2 strength (#78) uses the same protocol, with B2 itself as the scored model.

## Revisit when

- **A component's leader and its ties span the whole grid,** so the data cannot tell the settings apart. Then the steadiness order alone decides, and the grid or the objective needs a second look. Team strength's leader sits on its grid's edge: its grid can grow, under a new ADR, if gate 1 or the player layer shows memory matters.
- **Or gate 1 shows a frozen setting failing badly on the development seasons.** Retuning there would spend them, so it needs the owner and a new ADR.
