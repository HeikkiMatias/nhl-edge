# 0010. Simple xG: specification, fixed in advance, fitted per season

- Status: Accepted
- Date: 2026-09-30

## Context

B2's team strength (#74) rates teams by expected goals for and against, and its goalie effect (#75) by goals saved above expected (docs/plan.md §5). Both need an expected-goals model on unblocked shots (#73). The plan calls for a simple one in phase 2, trained chronologically, and leaves a custom model to phase 6.

A fitted component must not see the data it scores (hard rule 1), and a choice tuned on the development seasons (2018-19 and 2021-22) would shape gate 1's evidence. So the specification is fixed here, before any fit is scored.

Two choices went to the owner:
- **Rush shots.** They score more often, but seeing them needs the play before each shot, of any kind, and `shots` held only shots.
- **The training window.** All earlier seasons, or only the last five.

## Options

For rush:
1. **Add the play before each shot to `shots`**, and flag a rush from it. It needs four new columns and a replay of every season from the raw cache.
2. **Leave rush out** until phase 6's custom xG.

For the training window:
1. **Every earlier season**, with a season term that absorbs scoring drift.
2. **The last five seasons**, which follow changes in recording habits more closely, with less data for rare shot types.

## Decision

Rush option 1 and window option 1, both chosen by the owner on 2026-09-30.

**Shots modelled.** Every unblocked shot in `shots` (goals, shots on goal and missed shots in periods 1 to 4) with coordinates. Left out:
- penalty shots;
- shots at an empty net;
- shots without coordinates.

They get no xG.

**Inputs.** A logistic regression on:
- **Distance and angle** to the net at x = +89 (`shots` turns every shot toward it). Each is a cubic B-spline with fixed knots:
  - distance at 0, 10, 20, 30, 45, 60, 100 and 200 feet;
  - angle at 0, 20, 40, 60, 80, 100 and 180 degrees, where past 90 is behind the goal line.
- **Shot type:** wrist (the reference), snap, slap, backhand, tip-in, deflected and wrap-around. The rare types and unknown ones are grouped as "other".
- **Rebound:** the play before the shot is a shot on goal, missed shot or blocked shot by the shooting team, 3 seconds or less before.
- **Rush:** the play before the shot is in the neutral zone or the shooting team's defensive zone, 4 seconds or less before.
- **Strength state from the shooting team's side** (ADR 0009): 5v5 (the reference), 4v4, 3v3, advantage, shorthanded and unknown.
- **Season,** one term per training season. A season the model has not seen takes the latest training season's level.

The windows of 3 and 4 seconds are the common public definitions. None of the constants is tuned on results.

**Fitting.** L2-regularized with C = 1, which is negligible at hundreds of thousands of shots and only keeps a rare category's coefficient finite.
- **One model per season,** fitted on every earlier season's shots that were public before the season's first game.
- **2010-11,** the first season in the lake, has no model; every season from 2011-12 on is scored.
- **Each model records** its `train_cutoff` (the last training shot's `observed_utc`) and an artifact version `xg-<yyyymmdd>-<shortsha>`.

**The play before a shot.** `shots` gains four columns from the play logged just before each shot in the same period, ordered by game time and then play order:
- `prev_event_type`;
- `prev_seconds`;
- `prev_by_shooting_team`;
- `prev_zone`, from the shooting team's side.

The zone comes from the play's x and the shooting team's attack direction, past a blue line at x = ±25. Without coordinates it comes from `zoneCode`.

Blocked shots are the exception in the NHL's logging. On a sample of 160 games from 2010-11 to 2023-24:
- a blocked shot is logged under the team that took it;
- its `zoneCode` is mostly from the blocker's side;
- coordinates and `zoneCode` agreed for every other kind of play.

**The output.** A new lake table, `shot_xg`: one row per scored shot, with `xg`, `train_cutoff`, `artifact_version` and the shot's own `observed_utc`. The model predates every shot it scores, and the schema checks it.

**The report.** `nhl xg` fits and scores, writes `shot_xg`, and writes a calibration report to `reports/xg/<version>.md`:
- per season, goals against xG per 100 shots with a weekly block bootstrap interval (hard rule 7), and AUC;
- over the open seasons, the same by distance band, strength state, shot type, rebound and rush.

A held-out season shows only how many shots were scored.

## Backtest evidence

None. xG is an input, not a model scored against B1. Its calibration report is on #73's PR, and team strength (#74) is its first use in the walk-forward.

## Consequences

- `ingest/shots.py` fills the four `prev_` columns, and `nhl ingest --replay` rebuilds every season. They come from the game's own play-by-play, which ADR 0004 already dates to the morning after.
- `features/xg.py` holds the model, `audit/xg.py` the report. `tests/leakage/test_xg.py` shows that the scored season and later ones never move a shot's xG, nor does an earlier shot that became public only after the fold started.
- The live season is scored by the model fitted before its first game, and a rerun refits it on the same shots. Scoring new games each night is left to the first reader, team strength (#74).
- Scorekeepers record shot locations differently from rink to rink. This model does not correct for it. Phase 6's custom xG may, if the report or team strength shows it matters.
- MoneyPuck's shot-level xG (docs/plan.md §3) could serve as a sanity check, but it is not fetched. Its terms of use and a download would need the owner.

## Revisit when

- **The report shows a season, distance band, strength state or shot type mis-calibrated** in the open seasons, with an interval that excludes zero by more than 1 goal per 100 shots.
- **Or team strength (#74) or the goalie effect (#75) needs a term this model lacks,** such as rink effects or shooter talent. That belongs to phase 6's custom xG, under its own ADR.
