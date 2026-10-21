# 0017. Lineup availability: candidates from the last ten games, a logistic fit per season, shifted to the skaters expected to dress

- Status: Accepted
- Date: 2026-10-02

## Context

B3 builds each team's expected goals from its projected lineup (docs/plan.md §5), so before a game it needs the probability that each skater dresses (#99, phase 3 task 5). The backtest may read only earlier boxscores (hard rule 9), never a game's own lineup, and plan §10 asks for a fitted availability per player, not a fixed rule. The live roster pull of phase 5 has no point-in-time history, so the backtest cannot use it.

A specification tuned on the development seasons (2018-19 and 2021-22) would shape gate 2's evidence, and the phase 3 plan says the lineup model tunes nothing. So it is fixed here, before any fit is scored.

The counts behind it come from the training seasons 2010-11 to 2017-18, with no model fitted:
- 22.6 candidates per team-game, and 18 skaters dress in all but 20 of 18,711 team-games.
- Of the skaters who dressed in the team's last game, 93.8% dress again; 17.8% of the other candidates do.
- Of those who left their last game early, 42% dress again, against 94%.
- About 0.27 dressed skaters per team-game were not candidates. In a team's first game of a season it is about 5 of 18, and last season's regulars dress only 62.5% then.

## Options

1. **A logistic regression per candidate** on a few inputs from earlier boxscores, refit per season, with each team-game's probabilities shifted to add up to the skaters expected to dress. It cannot see injuries, signings or trades until they show in a boxscore.
2. **A rule: he dresses if he dressed last game.** Nothing to fit, but each answer is certain, so B3 could not mix over availability doubts (plan §10), and it misses 6% of regulars and 18% of the others.
3. **A choice model of 18 among the candidates**, like ADR 0012's one among the goalies. Its exact likelihood runs over every set of 18, which is costly, for little over the shift in option 1.
4. **A tuned model,** with a grid over the window and the caps (ADR 0011). The phase 3 plan leaves tuning to RAPM's three settings.

## Decision

Option 1, approved by the owner on 2026-10-02.

**Candidates.** The skaters (forwards and defense in the boxscore) who dressed for the team in its last 10 games public before the prediction time, followed through its line of team codes (PHX, ARI, UTA). A player whose latest public game was for another team drops out. A team with no earlier game has none.

**Inputs per candidate**, each between 0 and 1:
- whether he dressed for the team's last game, and his share of those 10 games;
- team games since he last dressed, capped at 10 (0 when he dressed last game);
- an early exit: he dressed last game and played under half his average ice time in his other games among the 10. Without such games or an ice time it is 0. The half is fixed, not tuned;
- his run of straight team games dressed up to the last one, capped at 10;
- whether the boxscore listed him among the defense in his latest game for the team;
- the team's first game of a season (its last game was in an earlier season), times "dressed last game" and times "share of the 10". A flag that is the same for every candidate cancels in the shift below, so it acts only through these products and the total.

**The model.** A logistic regression with an intercept and a light ridge (1e-3, not on the intercept) that keeps the fit finite and is not tuned. It is fitted per season on every earlier season's candidates whose team-game boxscore was public before the season's first as-of time. The window, the caps, the half and the inputs are fixed here.

**The total.** Each team-game's probabilities are shifted by one common amount on the log-odds scale, so that they add up to 18 minus the expected newcomers. These are the earlier seasons' average number of dressed skaters per team-game who were not candidates, from the same team-games as the fit: one average for a team's first game of a season and one for its other games. 2011-12's fit has no earlier first games (2010-11's have no history), so its first games use the other average. With no more candidates than the total, each gets 1.

**Goalies.** Their `p_start` is copied from `goalie_starts` (ADR 0012), with its own `train_cutoff` and `artifact_version`.

**When.** A game is rated as of the time team strength uses: 10:00 US Eastern on its date, or an hour before the start if that is earlier. `lineups` stores each candidate's `p_available`, with `observed_utc` at that time and `train_cutoff` at the last boxscore its model read.

**Scored** per season, by the report `nhl lineups` writes:
- the Brier score over each team-game's skaters: the sum of (p − dressed)² over the candidates, plus 1 for each dressed skater who was not a candidate;
- the share of dressed skaters who were not candidates.

The Brier score has a weekly block bootstrap interval, beside "dressed last game" (probability 1 or 0) as a reference, paired by team-game. The training seasons, 2019-20 and 2020-21 among them, show their figures. The development and held-out seasons show only their counts until gate 2.

## Backtest evidence

None yet. B3's walk-forward (#106) and gate 2 (#107) will score the lineups through B3 against B2. The model's own first run is `lineup-<yyyymmdd>-<sha>`, quoted on #99's PR.

## Consequences

- Ice time and power-play units (#100) weight each candidate's minutes by `p_available`. The gap to 18 is the expected newcomers, which get a replacement-level skater of their role there.
- A newcomer (a call-up, a trade arrival or a signing) has no row until he dresses for the team.
- Summer trades and signings stay unseen until a boxscore shows them, so a first game's candidates include players who have left. The lower total and the products absorb that on average, not player by player.
- The early exit reads `toi_s`, which a post-game correction can change (ADR 0004). The backtest sees the corrected times while live sees the first ones; #30 measures how often they differ.
- The training seasons' probabilities come from models fitted only on earlier seasons, so they are out-of-sample. No choice here was made on a fitted score.

## Revisit when

- **A point-in-time roster or injury source** covers enough games to replace or correct the model, such as the daily roster pull of phase 5.
- **Or the ice-time projection (#100) or gate 2 shows availability errors moving B3.** Changing the inputs, the window or the total then needs a new ADR.
