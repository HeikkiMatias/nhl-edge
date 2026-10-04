# 0026. The uncertainty score u: goalie doubt, availability doubt and rookie minutes, equally weighted

- Status: Accepted (by the owner, 2026-10-04, in the phase 4 plan)
- Date: 2026-10-04

## Context

docs/plan.md §5 handles uncertainty in three places:
- the scenario mixtures, which B2 and B3 already use;
- the market blend, where the model's weight depends on an uncertainty score u;
- the selection policy, where a higher u raises the required expected return and lowers the stake.

§5 names u's sources: an unconfirmed goalie, availability doubts and the share of rookie ice time. It doesn't say how they're measured or combined. The inputs exist from phase 3: `goalie_starts.p_start`, `lineups.p_available` and `exp_5v5`, `lineup_replacements`, earlier boxscores and `player_league_seasons`. The development seasons have been seen, so u's specification has to be fixed before any blend or policy result is read, with nothing tuned on them.

## Options

1. **Three parts with equal weights,** each standardized on the fold's training games. It's simple and nothing is tuned, but it may weight one source more than its information deserves.
2. **Weights fitted with the blend,** a b_u per part. That's three parameters instead of one, fitted on about 3,200 games, which is a noisy fit for one more interaction.
3. **A single source,** such as goalie doubt alone. It's simplest, but it drops two of §5's three named sources.

## Decision

Option 1, as the phase 4 plan approved by the owner on 2026-10-04. `game/uncertainty.py` computes three parts per game from rows known before its prediction time:
- **Goalie doubt:** 1 minus the likeliest candidate starter's `p_start`, averaged over the two teams. A team without candidates counts 1.
- **Availability doubt:** the sum of p·(1−p) over a team's skater candidates (`p_available`), the expected number of lineup surprises, averaged over the two teams.
- **Rookie minutes:** the share of both teams' projected 5v5 minutes going to skaters with fewer than 82 earlier NHL regular-season games, or to replacement slots.
  - A skater's earlier games are his NHL lines of earlier seasons (`player_league_seasons`, public at each season's end) plus this season's boxscores.
  - Boxscores alone would start every career at 2010-11 and count some veterans as rookies.
  - 82 games is one full season, fixed here.

u is the average of the three parts, each standardized by its mean and standard deviation over the fold's training games (`Scale`). A part that never varies gets a spread of 1.

## Backtest evidence

These are inputs only. No result or model output is read. From a local `nhl backtest` run on the development seasons and the blend's training folds, E2, mean (standard deviation):

| Season | Games | Goalie doubt | Availability doubt | Rookie minutes |
| --- | --- | --- | --- | --- |
| 2018-19 | 1,267 | 0.294 (0.086) | 1.54 (0.47) | 0.166 (0.062) |
| 2019-20 | 1,080 | 0.306 (0.084) | 1.53 (0.47) | 0.167 (0.056) |
| 2020-21 | 866 | 0.308 (0.088) | 1.72 (0.49) | 0.193 (0.071) |
| 2021-22 | 1,306 | 0.295 (0.086) | 1.77 (0.53) | 0.192 (0.075) |

E1 matches to the third decimal, since the lineups and goalie starts become known at the as-of time, before both prediction times. Every game B3 predicts has u.

The blend's paired log loss against B1 with and without u comes with #140.

## Consequences

- **The backtest reports u's parts per season** under `uncertainty` in summary.json. It has a leakage test (`tests/leakage/test_uncertainty.py`).
- **The blend (#140)** fits one b_u on u, and the policy (#141) reads u in standard deviations above its training mean.
- **History's goalie doubt is the goalie-start model's alone,** since no historical confirmation exists. Live, a confirmed starter (#42's polls) lowers u, so the blend trusts the model more on live games than its training saw. The model card records this as a known weakness.
- **u describes the projection, not the model's error.** A wrong but confident projection scores low.

## Revisit when

- The blend's b_u has the wrong sign (the model trusted more as doubt grows) on a fold, with an interval that excludes 0.
- Or live 2026-27 shows u's distribution far from history's once confirmations arrive, enough that the policy's hurdle stops binding or binds on most games.
