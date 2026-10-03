# 0021. Penalty rates and expected power plays: players' unoffset penalties, two lineup views averaged, at the league's level

- Status: Accepted
- Date: 2026-10-03

## Context

B3's power-play and shorthanded expected goals need each team's expected power plays (docs/plan.md §5: "expected opportunities times average power play length times the PP unit's xG rate, reduced by the opposing PK unit. Expected opportunities come from the penalty-drawn and penalty-taken rates of both projected lineups"). Until now every team gets the league's average power-play time (ADR 0018). #104 (phase 3 task 10) adds:
- each skater's penalties taken and drawn per hour;
- each team's expected power plays, power-play minutes and shorthanded xG.

The phase 3 plan says penalty rates tune nothing: the memory is RAPM's frozen one (#103, ADR 0011), and the pull toward the role's average is estimated from the data each season.

The counts behind it come from the training seasons 2011-12 to 2017-18; no model was fitted.
- **Penalties per game:** about 8.2. Of the minors, double minors and majors, 21% are offset by a penalty of the same length to the other team at the same moment, which gives no power play. That includes 95% of majors, mostly fights.
- **Power plays per team-game,** counting only unoffset penalties: 3.04 to 3.33 by season. They move by up to 8% from one season to the next (3.26 in 2013-14, 3.04 in 2014-15).
- **Power-play length:** 1.76 to 1.81 minutes of power play (5v4, 5v3 and 4v3, both nets manned) per opportunity, steady across seasons.
- **Penalties no skater takes or draws:**
  - no skater named as taker: 2.5% (too many men, bench minors);
  - no skater named as drawer: 7% (delay of game over the glass, too many men).
- **How much players differ (2016-17, players with 8 hours or more):**
  - forwards take 0.78 per hour and draw 0.89, with a spread between players' true rates of about 0.38 per hour;
  - defensemen take 0.70 and draw 0.46, with spreads of 0.32 and 0.22.

  On a method-of-moments estimate, the pull toward the role's average is worth 6 to 10 hours of ice time, so a regular's own record dominates within a season.

## Options

The owner chose among these on 2026-10-03:
1. **Which penalties count: only unoffset ones** (chosen), or every minor and major.
2. **How both lineups combine: the average of two views** (chosen), the opponent's expected penalties taken and the team's expected penalties drawn, or their product relative to the league.
3. **Power-play length: the league's average from earlier games** (chosen), or each team's own.
4. **Where players' rates live: their own table, `penalty_rates`, with player_ratings' layout** (chosen), or inside `player_ratings`. Every lake write replaces whole date partitions, so sharing the table would make RAPM and #104 merge each other's rows on every rewrite.

## Decision

**Counted penalties.** Penalties of 2, 4 or 5 minutes: minors, double minors, bench minors, majors and match penalties. Misconducts and penalty shots don't count. At each moment of a game (period and second) and each length, a team's n penalties face the other team's m; each of the n counts (n − min(n, m)) / n. A penalty counts as **taken** by its committer and **drawn** by its drawer when that player is a skater in the game's boxscore. Penalties with no skater taker or drawer count only toward the league's level.

**A skater's rates** for each of taken and drawn, before a game:
- **His games:** every earlier game of his with stints, public before team strength's as-of time, on any team. Exposure is his minutes at 5v5, on the power play and on the penalty kill, the states projected minutes cover (ADR 0018). Games without a complete shift chart add nothing.
- **Weights:** 0.5^(d / 360), d league game days back: RAPM's frozen memory (#103).
- **His rate:** r = (C + c·ρ) / (H + c).
  - C is his weighted count and H his weighted hours.
  - ρ is his role's rate, all skaters of the role with the same weights and games.
  - c is the pull, in hours. Among the season before's skaters of the role with 20 games or more, μ = ΣC / ΣH and v = (Σ H·(C/H − μ)² − n·μ) / ΣH, the spread of true rates once Poisson noise is taken out; c = μ / v, per role and for taken and drawn. If v is 0 or less, everyone sits at ρ.
  - A candidate without games gets ρ. His sd is the gamma posterior's, √(C + c·ρ) / (H + c).

**A lineup's index** for taken and drawn: I = Σ e·r / Σ e·ρ over the team's projected skaters. e is a candidate's expected minutes (`exp_5v5 + exp_pp + exp_pk`, which include his probability of dressing), and the replacement skaters count at their role's rate. A league-average lineup is 1.

**Expected power plays** for team A against team B: O_A = L × (I_taken(B) + I_drawn(A)) / 2. L is the league's unoffset penalties per team-game over the games public before the as-of time, in the game's season and the one before, team strength's league window. The level comes from the league's recent games, so a season-to-season drift shows as the season's games accumulate, sooner than through the players' two-season memory, while the lineups set each team's distance from it.

**Power-play minutes:** P_A = O_A × ℓ, with ℓ the league's power-play minutes per unoffset penalty, same window. A's penalty-kill minutes are P_B.

**Shorthanded xG:** A's expected shorthanded xG = s × P_B, s the league's xG per penalty-kill minute for the shorthanded team, its own goalie in, same window.

**What is stored** (`nhl power-plays`):
- **`penalty_rates`:** each candidate's `pen_taken` and `pen_drawn` per game, with player_ratings' columns except aging: mean, prior (ρ), sd, hours, known_utc, half_life_days, pull_hours, as_of_utc, train_cutoff, artifact_version, observed_utc.
- **`expected_power_plays`:** per team-game, the two indexes, O, P, A's penalty-kill minutes, shorthanded xG, L, ℓ, s, the latest game read (known_utc) and the timestamps.
- **The cutoff:** both tables carry RAPM's tuning cutoff (2018-04-09 10:00 UTC), since the memory was tuned (ADR 0011), or the season's pulls' cutoff if later; `expected_power_plays` also the game's lineups' cutoff, whose expected minutes it reads. observed_utc is the later of as_of_utc and the cutoff.

**Scored** per season by the command's report: the squared error of each team-game's power-play minutes against its actual minutes. B2's team-level estimate is the reference: team strength's expected power-play minutes with its frozen settings. Each season shows the paired difference with its weekly block bootstrap interval. The training seasons are in-sample for both RAPM's memory and B2's settings (ADR 0011), so this compares the two and is not out-of-sample evidence. The development and held-out seasons show only their counts until gate 2.

## Backtest evidence

None yet. B3's walk-forward (#106) and gate 2 (#107) score the expected power plays through B3 against B2. The first run, `power-plays-<yyyymmdd>-<sha>`, will be quoted on #104's PR.

## Consequences

- **B3 (#106)** reads `expected_power_plays` for P and the shorthanded xG, scaling the skaters' `exp_pp` and `exp_pk` to the team's own power-play time in place of the league's (ADR 0018). Players' rates in `penalty_rates` let B3 redo the indexes for another lineup.
- **Team-level penalties** (too many men, bench minors, goalies' penalties) sit in L, the same share for every team.
- **Offsetting** matches penalties of the same length only. A major against a minor at the same moment, or a double minor against a minor, is not netted, which is rare.
- **Penalties at 4v4, 3v3 or with an empty net** count toward a player's rate, while his exposure covers only the three projected states. That raises everyone's rate a little and cancels in the index.
- **New code:** `ratings/penalty_rates.py`, its leakage test, both tables' schemas, the command and its report. docs/plan.md's table list gets both tables and drops "penalties to come" from player_ratings.
- **Nothing is tuned.** The memory is RAPM's frozen one, and the pulls, rates and league figures are measured from earlier data.

## Revisit when

- **The report shows B2's team-level minutes matching or beating these** across the training seasons. B3 would then take power-play time from team strength's figures instead.
- **Or gate 2 shows B3's special-teams term adding nothing.**
- **Or a season's power-play length moves** by more than about 5%, so a league average no longer serves every team.
