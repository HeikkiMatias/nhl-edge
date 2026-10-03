# 0023. B3: expected goals from the projected lineups in B2's model, projected skaters in training, B2's L2, three gate 2 subsets

- Status: Accepted
- Date: 2026-10-03

## Context

B3 is phase 3's model and gate 2's challenger to B2 (docs/plan.md §5, §6; #12). It keeps B2's logistic model (ADR 0013) and replaces B2's team and goalie terms, ΔS and ΔG, with one input: Δĝ, the home team's expected goals less the away team's, built from the projected lineups. h_s stays a fixed term, and ΔR and the empty-seat share stay as inputs.

Its inputs are built and frozen:
- RAPM ratings and league rates (#101 to #103, ADR 0019, 0020, 0011);
- projected lineups and minutes (#99, #100, ADR 0017, 0018);
- expected power plays and shorthanded xG (#104, ADR 0021);
- finishing and goalie conversion (#105, ADR 0022).

This ADR fixes how B3 combines them before any B3 fit is scored. The phase 3 plan named two choices for the owner, and gate 2 needs its subsets fixed before any B3 result is seen.

## Options

The owner chose among these on 2026-10-03:
1. **The training games' lineups:**
   - **projected skaters and the starters who played** (chosen), the goalies treated as in B2 (ADR 0013);
   - all as at prediction (the goalie mixture in training too);
   - the actual lineups and starters, for which ratings exist only for projected candidates.
2. **The L2 strength: B2's frozen 100** (chosen), or B3's own tuning under ADR 0011.
3. **Gate 2's subsets: three, from boxscores** (chosen): trades, injuries and lineup changes, scored separately and together. The other option was one combined subset by a minutes-weighted change.

## Decision

**The model:** P(home win) = σ(β0 + h_s + β1·Δĝ + β·ΔR + β·empty seats), for the full game, overtime and shootout included (hard rule 2). ΔR and the empty-seat share are B2's inputs (ADR 0013).

**A team's expected goals.** For team A against team B, from each game's rows:
- **5v5:** (T / 60)·(μ5 + Σ_A w·o − Σ_B w·d).
  - T is the game's 5v5 minutes, the projected skater-minutes at 5v5 over five, averaged over the two teams.
  - μ5 is RAPM's 5v5 rate for the season, the intercept plus the season term of the game date's fit (`rapm_terms`).
  - o and d are each candidate's 5v5 offense and defense (`player_ratings`).
  - w = 5·exp_5v5 over the team's total exp_5v5, the candidate's average share of the five skaters on the ice.
  - Replacement skaters (`lineup_replacements`) count in the minutes, rated 0, the reference skater.
- **Power play:** (P_A / 60)·(μPP + Σ_A u·pp − Σ_B v·pk).
  - P_A is A's expected power-play minutes (`expected_power_plays`).
  - μPP is RAPM's power-play rate for the season.
  - u = 5·exp_pp over A's total exp_pp, and v = 4·exp_pk over B's total exp_pk: five skaters on the power play and four killing (5v4, the base).
- **Shorthanded:** A's expected shorthanded xG (`expected_power_plays`).
- **Goals:** ĝ_A = (5v5 + power play + shorthanded) × M, with M = κ·φ_A·γ (`goal_multipliers`) for the opposing goalie: the league's finishing, A's φ and the goalie's γ. An opponent without candidate goalies has one row at γ = 1.

Δĝ = ĝ_home − ĝ_away for a pair of goalies, each team's goals with the other's goalie in net. The home term of RAPM, like its score, zone and arena terms, only removes bias and is left out: home ice lives in h_s (plan §5).

**The fit,** per fold, as B2's:
- a logistic regression with an L2 penalty of 100 (B2's frozen strength, ADR 0011) on the standardized inputs' weights, not on β0;
- training on every game from 2011-12 whose result and boxscore were public before the fold starts, and whose rows were known by then;
- the tuned seasons' rows are known only from the tuning cutoff (2018-04-09 10:00 UTC), so a fold starting before the latest cutoff is refused.

A training game's Δĝ uses the projected skaters, as at prediction, and the goalies who started it, as in B2 (ADR 0013): each team's goals with the opposing starter's M, or κ·φ (γ = 1) when the starter was not a candidate. `train_cutoff` is the last moment any row the fit read became known.

**A prediction** averages the probability over every pair of candidate starters, each pair weighted by the product of the two goalie-start probabilities (`goalie_starts`), as B2 does. A team without candidates counts its goalie as average. A game's own lineup and starters are never read (hard rule 9).

**Gate 2's subsets,** fixed now from boxscores and the projection before any B3 result. A game is in a subset if either team qualifies:
- **Trade:** a projected skater (`p_available` at least 0.5) who dressed for another team in any of his last 10 games before this one.
- **Injury:** a regular missing from the projection (not a candidate, or `p_available` below 0.5). A regular is among the team's 9 forwards or 4 defensemen with the most ice time over its last 10 games.
- **Lineup change:** 3 or more of the skaters who dressed in the team's previous game missing from the projection, by the same test.

Each subset and their union are scored. Each reads only boxscores public before the game's as-of time and the game's projection.

**Reported** by `nhl backtest` on E1 and E2 beside B0, B1 and B2:
- B3's log loss and its paired differences against B2 (hard rule 3) and against B1, pooled and per season, and on each gate 2 subset;
- its calibration intercept and slope;
- every game where it differs from B1 by more than 8 points, for manual review (hard rule 8);
- the lineup quality on the scored games: the projection's 5v5 minutes error and power-play unit accuracy (ADR 0018), and the goalie-start Brier score (ADR 0012).

All intervals are weekly block bootstrap (hard rule 7). A hockey-only mode, `nhl backtest --hockey-only`, scores B2 and B3 at the as-of time on outcomes alone, for seasons without prices. Held-out seasons stay refused until gate 2.

## Backtest evidence

None yet. #106's run of `nhl backtest` on the development seasons gives B3 against B2 for gate 2 (#107).

## Consequences

- **B3 against B2 isolates the player layer:** the same model, goalies, L2, ΔR and h_s, with Δĝ in place of ΔS and ΔG.
- **Hard rule 9 governs predictions.** The training games' starters only teach what a known goalie is worth, as for B2, which Codex flagged as a P0 on #90 and the owner kept. No game's own lineup feeds its own prediction, and the skaters are projected in training too.
- **Nothing new is tuned:** B3 reuses B2's L2, and its inputs' settings were frozen by their tasks. The training seasons' rows are in-sample for those settings, so no B3 result on 2011-12 to 2017-18 counts as out-of-sample.
- **New code:** `game/b3.py`, `backtest/subsets.py`, their leakage tests, B3 and the hockey-only mode in `nhl backtest`, and the report's B3 sections.

## Revisit when

- **Gate 2 shows B3 no better than B2** on the development seasons or the subsets: the layer's inputs, not this combination, are then the first suspects.
- **Or B3 is miscalibrated** in a way the training lineups explain: projected skaters in training against actual ones.
