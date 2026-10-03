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
- **The run** (`team-strength-20261001-38b38ae`, quoted on #86 and committed under `reports/tuning/`) scored 2012-13 to 2017-18.
  - **Chosen:** a half-life of 80 games and a pull worth 40 games, log loss 0.6780 [0.6742, 0.6816]. It was also the leader.
  - **Ties:** every setting with a pull and a half-life of 20 games or more tied with it. Settings with no pull, and every half-life of 10, did not, at +0.0015 to +0.0026.
  - **An earlier run:** `team-strength-20261001-9dc689a`, before Codex's fixes on #86, chose the same setting.
  - **The grid's edge:** the leader sits on the grid's steadiest corner, so a longer memory might do a little better. The owner chose to keep the grid as fixed and freeze this result, since the gaps are within noise and widening the grid would take a second look at the same seasons.

**The goalie effect's application (#75, #88):**
- **Grid:** memory as a half-life of 20, 40, 80 or 160 of the goalie's own games, and a pull toward zero worth 500, 1,000, 2,000 or 4,000 unblocked shots. That is 16 candidates.
- **Steadiness order:** the longer half-life first, then the larger pull.
- **Feature:** the ΔG expected under the goalie-start probabilities (#76, ADR 0012), never the actual starter.
- **The run** (`goalie-effect-20261001-dc79a24`, committed under `reports/tuning/`) scored 2012-13 to 2017-18.
  - **Leader:** a half-life of 40 games and a pull worth 4,000 shots, log loss 0.6873 [0.6848, 0.6897].
  - **Ties:** 13 of the 16. All settings fell within 0.6873 to 0.6877, against 0.6887 for the same win model with no feature.
  - **Chosen:** the steadiest tie, a half-life of 160 games and a pull worth 4,000 shots, on the grid's steadiest corner. Its tie held by a margin of 2e-6: +0.000367 [-0.000002, +0.000725]. Its neighbor, 160 games and 2,000 shots, missed. The owner chose to follow the rule as set in advance.
- **Reusing frozen settings:** the expected shots in ΔG use team strength's frozen settings, which were chosen on the same seasons. Codex raised it as a P0 on #88. The owner kept it as this ADR intends (Consequences). Scored with league-average shots instead, which no outcome tuned, the rule chose the same setting.

**The season home edge's application (#77, #89):**
- **Grid:** a pull toward the three seasons before worth 50, 100, 200, 400, 800 or 1,600 games (a season has about 1,300).
- **Steadiness order:** the larger pull.
- **Feature:** h_s, 0 at a neutral site.
- **The run** (`schedule-terms-20261001-89564c5`) scored 2012-13 to 2017-18. All six tied, from 0.6886 to 0.6890.
- **Chosen:** the leader, which was also the steadiest, a pull worth 1,600 games, on the grid's edge. This is the "revisit when" case below: the data cannot tell the settings apart, so steadiness alone decided. The owner chose to freeze it and keep the grid. With this pull, a full season of home games still moves the estimate about halfway.

**B2's application (#78, #90, ADR 0013):**
- **Grid:** an L2 penalty of 0.1, 1, 10, 100 or 1,000 on the standardized inputs' weights.
- **Steadiness order:** the stronger penalty.
- **Scored model:** B2 itself, each season predicted by a fit on the earlier ones. The features are the frozen ones, with every table cut to 2017-18 and earlier.
- **The run** (`b2-20261001-fe11def`) scored 2012-13 to 2017-18.
  - **Chosen:** an L2 of 100, log loss 0.6780 [0.6741, 0.6821]. It was the leader and the steadiest of its ties.
  - **Not tied:** 1,000, at +0.0028 [+0.0013, +0.0043]. So the choice sits inside the grid.

**RAPM's application (#103, ADR 0019, ADR 0020):**
- **Grid,** fixed before the first run as the owner chose on 2026-10-03. That is 36 candidates:
  - a pull toward the prior mean worth 10, 20, 40 or 80 hours of ice time;
  - a memory, as a half-life of 90, 180 or 360 league game days (half a season, one or two);
  - an aging weight of 0, 0.5 or 1.
- **Steadiness order:** the larger pull first, then the longer memory, then the fuller aging.
- **Aging,** as the owner chose: at each season's start, every player's earlier evidence moves by the aging weight times his age curve's expected change for his age (ADR 0020). RAPM's running sums allow this exactly: X'Wy gains X'WX times the shift.
- **Feature:** each game's projected 5v5 expected-goal difference. It is each candidate's 5v5 offense plus defense (xG per hour) times his expected 5v5 minutes (ADR 0018), summed for the home team less the away team. Replacement skaters count as the reference skater, 0.
- **Scored:** 2012-13 to 2017-18, with ratings refit every game day from 2011-12 as `nhl rapm` does. Only the 5v5 model is fitted, without the posterior spreads, which the feature does not read.
- **Reuse:** the power-play model reuses the chosen settings, frozen (phase 3 plan).
- **A timing run** before the grid scored two of its points, the provisional setting without aging and with full aging, to size the run (about 4 minutes per candidate).
- **The run** (`rapm-20261003-3545379`, committed under `reports/tuning/`) scored 2012-13 to 2017-18.
  - **Leader:** a half-life of 360 days, a pull of 20 hours and no aging, log loss 0.6746 [0.6705, 0.6786]. Team strength's feature scored 0.6780 on the same seasons.
  - **Ties:** 22 of the 36. Every pull and every memory has a tied candidate, all within 0.6746 to 0.6763.
  - **Aging:** no candidate with full aging tied. At each pull and memory, no aging scored best and half aging next.
  - **The rule's choice:** the steadiest tie, 360 days, 80 hours and half aging, at +0.0007 [−0.0004, +0.0018]. It sits on the grid's edge for pull and memory, and it took half aging only because the order ranks fuller aging as steadier.
  - **Frozen, by the owner's decision on 2026-10-03:** the rule's pull and memory without aging, 360 days, 80 hours and aging 0, at +0.0004 [−0.0007, +0.0015], itself a tie. The owner kept the order for pull and memory, as for the other components on their grids' edges, and dropped aging, since it never helped.

## Backtest evidence

The tuning runs' losses record each choice and are not walk-forward evidence. The training seasons are in-sample by design, and a later run reuses earlier settings chosen on the same seasons. The development seasons, scored once every choice was frozen, are the evidence.

The tuning runs are above, each on its component's PR. B2, built from all four frozen settings, was scored on the development seasons by `backtest-20261001-388bb85` (#90). B2 minus B1 is +0.0112 [+0.0044, +0.0185] on E1 and +0.0078 [+0.0013, +0.0146] on E2, with a calibration slope of 1.35 [1.09, 1.62]. Gate 1 (#79) judges it.

## Consequences

- The training seasons' tuned features are in-sample for the choice, as 2012-13 to 2017-18 xG is for the rebound term (ADR 0010). They may train later models, but no result may present them as out-of-sample. Every fold from 2018-19 on uses settings chosen entirely before it.
- **Every tuned output records the run's cutoff,** the last result the run read: `train_cutoff` in `team_strength`, 2018-04-09 10:00 UTC. A tuned output counts as known no earlier than it, so `observed_utc` is the later of the output's own time and the cutoff. A fold that starts before the cutoff then cannot read the tuned seasons' outputs at all. Gate 1's folds start after it.
- One choice per component, made once. A component tuned later does not reopen an earlier one's settings. It reuses them, frozen, even though they were chosen on the same seasons. The owner confirmed this on #88, when Codex raised it as a P0, and #78 applies it to B2.
- B2's L2 strength (#78) uses the same protocol, with B2 itself as the scored model.
- Every tuned table records the same cutoff, 2018-04-09 10:00 UTC: `team_strength`, `goalie_effects`, `schedule_terms`, and RAPM's `player_ratings` and `rapm_terms`. B2's backtest refuses a fold that starts before the latest of them (ADR 0013), and B3's (#106) should do the same for the ratings.

## Revisit when

- **A component's leader and its ties span the whole grid,** so the data cannot tell the settings apart. Then the steadiness order alone decides, and the grid or the objective needs a second look.
  - **It happened for the home edge,** where all six settings tied.
  - **It nearly did for RAPM,** where 22 of 36 tied across every pull and memory, and only aging separated.
  - **It nearly did for the goalie effect,** where 13 of 16 tied.
  - **Team strength's leader sits on its grid's edge,** as do the goalie effect's and the home edge's choices.
  - In each case the owner kept the grid. A grid can grow under a new ADR if gate 1 or the player layer shows the setting matters.
- **Or gate 1 shows a frozen setting failing badly on the development seasons.** Retuning there would spend them, so it needs the owner and a new ADR.
