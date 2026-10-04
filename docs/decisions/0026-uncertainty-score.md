# 0026. The uncertainty score u: goalie doubt, availability doubt and rookie minutes, equally weighted

- Status: Proposed
- Date: 2026-10-04

## Context

docs/plan.md §5 handles uncertainty in three places:
- the scenario mixtures, which B2 and B3 already use;
- the market blend, where the model's weight depends on an uncertainty score u;
- the policy, where a higher u raises the hurdle and lowers the stake.

§5 names u's sources, an unconfirmed goalie, availability doubts and the share of rookie ice time, but not how to measure or combine them. The development seasons have been seen, so u is fixed before any blend or policy result, with nothing tuned on them.

## Options

1. **Three parts with equal weights,** each standardized on the fold's training games. It's simple and untuned, but it may weight one source more than its information deserves.
2. **A weight per part,** fitted with the blend. That means three interaction weights fitted on about 3,200 games, a noisy fit.
3. **One source only,** such as goalie doubt. It drops two of the three sources §5 names.

## Decision

Option 1, the phase 4 plan approved by the owner on 2026-10-04. `game/uncertainty.py` computes three parts per game from rows known before its prediction time:
- **Goalie doubt:** 1 minus the likeliest candidate starter's `p_start`, averaged over the two teams. A team without candidates counts 1.
- **Availability doubt:** Σ p·(1−p) over a team's skater candidates (`p_available`), averaged over the two teams.
- **Rookie minutes:** the share of both teams' projected 5v5 minutes going to skaters with fewer than 82 earlier NHL regular-season games, or to replacement slots. The earlier games are NHL lines of earlier seasons once public, plus this season's boxscores.

u is the mean of the standardized parts (`Scale`, fitted on the fold's training games), and the policy reads u in its training standard deviations (`u_sd`).

## Backtest evidence

Run `backtest-20261004-6f74840` in reports/backtest/runs.csv, on the 2021-22 fold, which learns from 2018-19 to 2020-21:
- **The blend that reads u,** BLEND minus B1, paired log loss with a 95% weekly block bootstrap interval:

  | | Result |
  | --- | --- |
  | E2 | −0.0042 [−0.0078, −0.0007] |
  | E1 | −0.0015 [−0.0043, +0.0018] |

  No blend without u was run, so this does not isolate u's own effect.
- **u's weight b_u** is +0.225 [−0.061, +0.511] on E2 and +0.222 [−0.065, +0.509] on E1. These are 95% intervals from the fit's own standard errors, not a bootstrap.
  - Both include 0. u adds nothing measurable to the blend on these seasons.
  - Both lean the wrong way: the model is trusted more as doubt grows.
- **The parts' spread** is under `uncertainty` in summary.json. The E2 means, 2018-19 to 2021-22, are:
  - goalie doubt: 0.29 to 0.31;
  - availability doubt: 1.53 to 1.77;
  - rookie minutes: 0.17 to 0.19.

## Consequences

- **Each game's parts carry their inputs' cutoffs,** and so does `Scale`, so the blend can refuse a fold that read later rows. `tests/leakage/test_uncertainty.py` covers u.
- **History's goalie doubt is the goalie-start model's alone.** Live, confirmed starters lower u, so the blend trusts the model more live than in training. The model card records this.
- **The policy's hurdle and stake still read u** (ADR 0028), even though the blend gives it no clear weight.

## Revisit when

- b_u's interval excludes 0 on the wrong side (the model trusted more as doubt grows).
- Or live 2026-27 shows u far from its historical range once confirmations arrive.

A u revised on live evidence is a policy change. It is judged only on games after its new freeze date, and the CLV count restarts.
