# 0022. Finishing φ and goalie conversion γ: shooters above the league, shared by their own xG rates, and the goalie effect per shot

- Status: Accepted
- Date: 2026-10-03

## Context

B3 turns expected goals into goals with two multipliers, both near 1 (docs/plan.md §5): the projected shooters' finishing φ and the opposing goalie's conversion γ. #105 (phase 3 task 11) builds both. The phase 3 plan says finishing tunes nothing: it estimates its pull toward 1 from the data, and γ reuses the goalie effect's frozen settings (#75, ADR 0011). The goalie enters B3 only through γ.

The counts behind it come from the training seasons 2011-12 to 2017-18, unblocked shots with xG (ADR 0010: no penalty shots or empty nets); no model was fitted.
- **League finishing,** goals ÷ xG: 0.98 to 1.03 by season. xG per unblocked shot: 0.061 to 0.063.
- **By role:** forwards take 85% of the xG and finish at 0.99, defensemen at 1.04.
- **How much shooters differ beyond noise** (method of moments, goals against xG):
  - forwards with 20 games or more: a pull of 38 to 203 expected goals, measured one season at a time;
  - forwards over their 2011-12 to 2017-18 careers with 20 xG or more: about 48;
  - defensemen: no spread beyond noise in six of the seven seasons.
- **Individual xG per hour** at 5v5, on the power play and on the penalty kill (2016-17, 20 games or more): forwards 0.45 to 1.04 (10th to 90th percentile), defensemen 0.11 to 0.31.
- **The goalie effect** in `goalie_effects`: −0.0028 to +0.0036 goals saved per unblocked shot (5th to 95th percentile), so γ would be about 0.94 to 1.05.

## Options

The owner chose among these on 2026-10-03:
1. **φ's baseline: finishing above the league's** (chosen), or raw goals ÷ xG. Above the league, a league-wide drift in finishing doesn't show up as a shooter's.
2. **φ's pull: from every season before the one rated** (chosen), or from the season before only, which swings from about 40 to 200 expected goals with noise.
3. **A team's φ weights each shooter by his expected share of its xG: from his own decayed xG rate** (chosen), or from his minutes and his role's average rate.
4. **Tables: `finishing` per candidate and `goal_multipliers` per game, attacking team and opposing candidate goalie** (chosen), or three separate tables.

## Decision

**Shots.** Unblocked shots with xG (ADR 0010), at every strength, whose shooter is a skater in the game's boxscore. Each skater-game with stints (lineup.minutes.player_minutes) gives his minutes at 5v5, on the power play and on the penalty kill, his xG and his goals. Games without a complete shift chart add nothing.

**Memory.** Every sum is decayed by 0.5^(d / 360), d league game days back from the latest date read: RAPM's frozen memory (#103), as for penalty rates (ADR 0021). A skater's history covers his earlier games on any team public before team strength's as-of time.

**A skater's φ.** φ = (G + c) / (κ·X + c):
- G his weighted goals, X his weighted xG;
- κ the league's goals ÷ xG over all skaters' shots with the same weights, so φ = 1 is league-average finishing;
- c the pull, in expected goals, per role. At each season's start, over the skaters of the role with 20 games or more across every earlier season, each counted once with his totals, and each season's shots set against that season's own goals ÷ xG (E, his expected goals): μ = ΣG / ΣE and v = (Σ E·(G/E − μ)² − n·μ·q) / ΣE, the spread of true finishing beyond noise; c = μ·q / v, or infinite when v is 0 or less, which puts every player of the role at 1. q is a goal count's variance over its mean: a goal is a shot's yes or no, so q = 1 − Σp² / Σp over the shots' expected goals p, about 0.9 rather than Poisson's 1.
- His sd is the gamma posterior's, √(G + c) / (κ·X + c). A candidate without shots, and every candidate of 2011-12, the first season with xG and so with no earlier season to measure c on, is at 1.

**A skater's xG rate,** for his share: (X + c_x·ρ) / (H + c_x), H his weighted hours, ρ his role's xG per hour with the same weights, and c_x the pull in hours, measured the same way on every earlier season, with xG for goals, hours for expected goals and q = Σ xG² / Σ xG, since xG is a sum of the shots' probabilities and so far less noisy than a count (about 0.1); infinite in 2011-12, so shares that season follow minutes and role.

**A team's φ.** Each candidate's share is e·r over the team's Σ e·r, with e his expected minutes (`exp_5v5 + exp_pp + exp_pk`, his probability of dressing in) and r his xG rate; the replacement skaters (ADR 0018) take their minutes at their role's rate and φ = 1. The team's φ is the shares' weighted average of φ.

**A goalie's γ.** For each candidate goalie in `goalie_effects`: γ = 1 − effect / x̄, with x̄ the league's xG per unblocked shot over the shots public before the as-of time, in the game's season and the one before (team strength's league window); γ = 1 before any shot with xG is public, when the effect is 0 anyway. The effect keeps its frozen settings, so γ adds nothing to tune.

**What is stored** (`nhl finishing`):
- **`finishing`:** each candidate's φ (mean, prior 1, sd), his weighted goals and expected goals, his xG rate with its prior and pull, his share of the team's xG, hours, the pulls, known_utc, the half-life and the stamps.
- **`goal_multipliers`:** per game, attacking team and opposing candidate goalie: the team's φ, the goalie's γ, κ·φ·γ (B3's factor on the team's xG, κ taken as 1 before any is public), x̄ and κ, known_utc and the stamps.
- **The cutoff:** the latest of RAPM's tuning cutoff (the memory), the goalie effect's (its settings, ADR 0011, 2018-04-09 10:00 UTC for both), the season's pulls', the game's lineups' (whose expected minutes set the shares) and, for `goal_multipliers`, the goalie effect's own. observed_utc is the later of as_of_utc and the cutoff.

**Scored** per season by the command's report, on each team-game's goals from shots with xG: the squared error of its actual xG × κ (the reference), against that times the team's φ, times the opposing goalie's γ mixed over his team's candidates by their pregame start probabilities (`goalie_starts`, as B3 will mix them), and times both. The game's own xG is read only to score, and its starter never. The training seasons are in-sample for RAPM's memory and the goalie effect's settings, so this compares the multipliers and is not out-of-sample evidence. The development and held-out seasons show only their counts until gate 2.

## Backtest evidence

None yet. B3's walk-forward (#106) and gate 2 (#107) score φ and γ through B3 against B2. The first run, `finishing-<yyyymmdd>-<sha>`, will be quoted on #105's PR.

## Consequences

- **B3 (#106)** multiplies each team's xG by `goal_multipliers`' factor for each opposing goalie scenario, weighted as B2 weights them.
- **2011-12** has φ = 1 and shares by minutes and role, since no earlier season has xG to measure the pulls on.
- **Defensemen** will mostly sit at φ = 1, as their finishing showed no spread beyond noise; their 4% edge over xG is the xG model's, the same for every team with the same share of defense shots, and the league κ covers it on average.
- **γ and ΔG** come from the same goalie effect: B3 takes it only through γ (no double counting, plan §5).
- **New code:** `ratings/finishing.py`, its leakage test, both tables' schemas, the command and its report; docs/plan.md's table list.
- **Nothing is tuned.** The memory is RAPM's, the goalie settings are the goalie effect's, and the pulls and league figures are measured on earlier data.

## Revisit when

- **The report shows φ or γ adding error** over xG × κ across the training seasons. B3 would then leave that multiplier at 1.
- **Or gate 2 shows B3's goal multipliers adding nothing.**
- **Or a season's league finishing moves** far enough that the decayed κ lags it by more than about 2%.
