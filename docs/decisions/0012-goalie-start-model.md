# 0012. Goalie-start model: candidates from the last ten games, a conditional logit refit per season

- Status: Accepted
- Date: 2026-10-01

## Context

B2 adds a goalie effect (#75), but before a game the starter is often unconfirmed. The plan (docs/plan.md §5) has B2 mix over goalie scenarios weighted by the probability that each goalie starts. The backtest may read only earlier boxscores (hard rule 9), never a game's own lineup. So it needs a model that gives those probabilities from earlier games alone (#76). The live goalie polls (#42) can be compared with it later.

A specification tuned on the development seasons (2018-19 and 2021-22) would shape gate 1's evidence. So it is fixed here, before any fit is scored, and has nothing to tune.

## Options

1. **A conditional logit over each team's candidate goalies,** with a few inputs from earlier boxscores. It refits per season, and each team-game's probabilities add up to 1. It cannot see injuries or call-ups announced before the game.
2. **A rule:** each goalie's share of the team's last ten starts. It has nothing to fit, but it ignores back-to-backs and rest.
3. **A tuned model** with a grid over the window and the caps (ADR 0011). That would be more to tune and log, for a component whose gains show up in B2 only through the goalie effect.

## Decision

Option 1, approved by the owner on 2026-10-01.

**Candidates.** The goalies who dressed for the team in its last 10 games public before the prediction time, followed through its line of team codes (PHX, ARI, UTA). A team with no earlier game has none.

**Inputs per candidate**, each between 0 and 1:
- his share of the team's starts in those 10 games, and in its games this season (0 in a season's first game);
- whether he started the team's last game, and his run of straight starts, capped at 10;
- whether he started the team's game yesterday. The team playing yesterday is the same for every candidate, so on its own it cannot move the choice among them, and it enters only through this product;
- days since his last start for the team, capped at 14;
- whether he dressed for the team's last game.

**The model.** A conditional logit: one choice among a team's candidates per game, so the probabilities add up to 1. It has a light ridge (1e-3) that keeps the fit finite and is not tuned.
- It is fitted per season on every earlier season's team-games whose starter was public before the season's first as-of time.
- Team-games whose starter was not a candidate, such as a debut or a trade, are left out of the fit.
- The window, the caps and the inputs are fixed here, not tuned.

**When.** A game is rated as of the time team strength uses: 10:00 US Eastern on its date, or an hour before the start if that is earlier. That is before E1 and E2 predict. `goalie_starts` stores each candidate's `p_start`, with `observed_utc` at that time and `train_cutoff` at the last starter its model read.

**Scored** per season, by the report `nhl goalie-start` writes:
- the Brier score over each team-game's goalies, with a starter who was not a candidate counted at probability 0;
- top-pick accuracy;
- the share of starters who were not candidates.

The Brier score and top-pick accuracy have weekly block bootstrap intervals. Beside them is option 2, the share of the last ten starts, as a reference. The development and held-out seasons show only their counts until their phase.

## Backtest evidence

None yet for B2. B2's walk-forward (#78) and gate 1 (#79) will score the mixture against B1.

The model's own first run is `goalie-start-20261001-<sha>`, quoted on #76's PR. On the training seasons 2011-12 to 2017-18 it beat the reference in every season:
- Brier score lower by 0.068 to 0.116, each interval excluding 0;
- top-pick accuracy 70.5% to 73.6%, higher by 4.9 to 8.4 points;
- 0.6% to 0.9% of starters not among the candidates.

## Consequences

- B2 (#78) mixes over `goalie_starts`, and the goalie effect (#75) is tuned with these probabilities, not with the actual starter.
- A starter outside the candidates gets no weight. B2 must handle that case, about 1% of team-games, as an unknown goalie.
- The training seasons' probabilities come from models fitted only on earlier seasons, so they are out-of-sample. No choice here was made on them.

## Revisit when

- **The live polls (#42) or confirmed-starter feeds** cover enough games to replace the model for confirmed starters, or to show it miscalibrated.
- **Or B2's goalie effect proves sensitive to the mixture,** so that a better start model would move gate 1. Changing the inputs or the window then needs a new ADR.
