# 0018. Projected ice time: a decayed average per player, pulled toward his role, scaled to the league's minutes

- Status: Accepted
- Date: 2026-10-02

## Context

B3 weights each skater's ratings by his expected minutes in each state (docs/plan.md §5): T·(μ_s + Σ_home (t_i/T)·o_i − Σ_away (t_j/T)·d_j), with the minutes adding up to 5 skaters × T. #99 (ADR 0017) gives each candidate's probability of dressing. #100 (phase 3 task 6) needs each candidate's minutes at 5v5, on the power play and on the penalty kill, his team's power-play unit 1, and a rule for the newcomers the lineup model expects. Everything comes from earlier games only (hard rule 9). The phase 3 plan says the lineup model tunes nothing, so the choices are fixed here before any fit is scored.

The counts behind it come from the training seasons 2011-12 to 2017-18, from the stints of the 8,024 games with a complete shift chart. No model was fitted.
- **Skater-minutes per team-game**, about the same every season (the range over the seven seasons' averages, as each season's figures appear in the first run's report):
  - forwards: 142.3 to 145.1 at 5v5, 17.5 to 18.4 on the power play, 9.7 to 10.6 on the penalty kill;
  - defensemen: 95.5 to 97.1 at 5v5, 6.6 to 8.6 on the power play, 9.9 to 10.8 on the penalty kill.
- **How noisy one player's minutes are against how much players differ.** For players with at least 20 games in a season, pooled over the seven seasons, the game-to-game variance divided by the variance between players' averages is:
  - forwards: 1.2 at 5v5, 1.0 on the power play, 1.0 on the penalty kill;
  - defensemen: 1.9 at 5v5, 0.9 on the power play, 1.7 on the penalty kill.

  A player's own average is worth that many games of the role average, so after a few games his own minutes dominate.
- **Newcomers** (dressed skaters who were not candidates) play less than the average dresser. At 5v5 a newcomer forward plays 9.9 minutes against 12.0, and a newcomer defenseman 13.5 against 15.9. In a team's first game of a season they also play more on the power play (about 1.5 minutes against 0.6) and on the penalty kill.

## Options

The owner chose among these on 2026-10-02:
1. **Memory: a half-life of 10 of his games** (chosen), 5 games or 20 games. Ten matches the lineup model's window. A move to a new line shows half its effect within 10 games, and one odd game counts about 7%.
2. **Pull toward his role's average: estimated each season from the season before** (chosen), or fixed at 2 games.
3. **The team's total minutes in each state: the league's average per role from the season before** (chosen), or each team's own decayed totals. Team-specific power-play time comes from the penalty-rate task (#104), which B3 can rescale with.
4. **Newcomers' minutes: a replacement skater of the missing role** (chosen, as plan §10 asks), or spread over the candidates.

## Decision

**Minutes if he dresses.** For each candidate (ADR 0017) and each state s of 5v5, power play and penalty kill:
- **His games:** every earlier game of his for the team's line of team codes that has stints, public before the prediction time. A game without a complete shift chart has no stints and is left out. States other than these three (4v4, 3v3 overtime, an empty net) are not projected.
- **Weights:** 0.5^(k/10), where k = 0 for his latest such game, k = 1 for the one before, and so on: a half-life of 10 of his games.
- **His minutes:** m = (Σ w·x + c·μ) / (Σ w + c).
  - x is his minutes in the state in each game.
  - μ is the average minutes of a dressed skater of his role in the state.
  - c is the pull, in games. It is the game-to-game variance of one player's minutes divided by the variance between players' averages, over the players of his role with at least 20 games.
  - μ and c come from the season before, per role and state.
- A candidate with no game with stints for the team gets μ.

**Expected minutes.** e = p × m × k, with p his probability of dressing (ADR 0017). For each team-game, role r and state s:
- **The total** L is the league's average skater-minutes of role r in state s per team-game, the season before.
- **Replacement skaters:** the expected count of role r is max(0, slots − Σ p over the role's candidates), with 12 slots for forwards and 6 for defensemen. Each gets the average minutes of a newcomer of role r in state s, the season before: one average for a team's first game of a season, one for its other games. Their minutes are R.
- **The scale:** k = (L − R) / Σ p·m over the role's candidates, or 0 when either part is 0 or less. Then the replacements take all of L, as in a new team's first game, which has no candidates. So the candidates and the replacements always add up to L.

**Power-play unit 1.** The five candidates with the most expected power-play minutes; a tie goes to the lower player id.

**What is stored** (both from `nhl lineups`):
- `lineups` gets each skater's expected minutes `exp_5v5`, `exp_pp` and `exp_pk`, and `pp_unit`.
- A new table `lineup_replacements` gets each team-game's replacement skaters per role: their expected count and minutes in each state.
- All of the season's fitted pieces (μ, c, L and the newcomer minutes) come from the season before, so the `train_cutoff` stays before the season's first as-of time.

**Scored** per season, by `nhl lineups`'s report, against "last game's minutes":
- **5v5 minutes MAE.** Over the candidates who dressed, in games with stints: the gap between his scaled minutes if he dresses (m × k) and his actual 5v5 minutes. The reference is his 5v5 minutes in his latest earlier game for the team with stints.
- **Power-play unit accuracy.** Over team-games with stints and power-play time: the share of the actual top five by power-play minutes that the projection named. The reference is the team's top five in its latest earlier game with stints and power-play time.
- **Intervals:** weekly block bootstrap, with the differences paired by team-game.
- The development and held-out seasons show only their counts until gate 2.

## Backtest evidence

None yet. B3's walk-forward (#106) and gate 2 (#107) will score the minutes through B3 against B2. The first run, `lineup-<yyyymmdd>-<sha>`, will be quoted on #100's PR.

## Consequences

- **B3 (#106)** reads `exp_5v5`, `exp_pp` and `exp_pk` as its t_i, and `lineup_replacements` for the replacement-level skaters.
- **The power play** is the league's average, the same for every team, until #104 projects each team's power-play opportunities.
- **Games without a complete shift chart** (117 of 8,141 in the training seasons, 1.4%) add nothing to a player's average.
- **The 12 and 6 slots** ignore the nights a team dresses 11 forwards and 7 defensemen, or 13 and 5.
- **Corrections:** minutes come from shift charts, which post-game corrections can change (ADR 0004, measured by #30).
- **Nothing here is tuned.** The memory is the owner's choice, and the pull and totals are measured from the season before.

## Revisit when

- **#104** projects team-specific power-play time, which should replace the league average.
- **Or gate 2 or the MAE report** shows the minutes moving B3, or the reference beating the projection. Changing the memory, the pull or the totals then needs a new ADR.
