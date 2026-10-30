# 0051. Replacement skaters stay rated at RAPM's reference skater

- Status: Proposed
- Date: 2026-10-30

## Context

#134, found by gate 2's gap review (#107). B3 fills a short projected lineup with replacement skaters (`lineup_replacements`, ADR 0018). It counts them in the minutes and rates them 0, RAPM's reference skater (ADR 0023). The candidates' minutes-weighted average is about +0.01 xG per hour for forwards and −0.02 for defensemen at 5v5, so 0 sits close to a typical NHL skater, not a replacement-level one. #134's cases are an expansion team's first game and any short lineup.

The owner parked #134 for the next model version on 2026-10-09. The 2026-27 policy is frozen (`policy-20261005-8ec5cf3`), so #134 is decided at the formal review on 2027-04-12, with evidence from the training seasons only. This record gives that evidence.

Where the replacement minutes are, on 2012-13 to 2017-18:
- **Overall,** they hold 1.8% to 2.1% of the projected 5v5 skater-minutes.
- **In a team's first game of a season,** they hold 24% to 27%. ADR 0017's candidates come only from a team's own boxscores, so its summer arrivals are invisible until they dress for it.
- **The newcomers who fill those slots** are mostly known players: 76% to 85% of them had an NHL rating before their game, in openers and in-season alike. As known before their game:
  - **in-season:** about −0.015 xG per hour for forwards and −0.016 to −0.035 for defensemen;
  - **in openers:** about 0 for forwards and −0.015 to −0.019 for defensemen.

## Options

1. **Keep 0,** and record it as a known weakness.
2. **A fringe skater's level:** per role, the games-weighted season-end rating of the skaters who dressed 20 games or fewer in an earlier season. That is about −0.027 for forwards and −0.05 for defensemen.
3. **A newcomer's prior** (ADR 0020): per role, the season prior of earlier seasons' newcomers. That is about −0.01 for forwards and −0.025 for defensemen.
4. **The newcomer level:** per role, what earlier seasons' newcomers were rated as known before their game. It has one value for a team's first game of a season and one for its other games.

## Decision

**Proposed: keep 0 (option 1).**
- **No level helps.** Neither of #134's levels, nor the newcomer level fitted to who actually fills the slots, moves the win log loss or the 5v5 xG error, on any subset. Every interval holds 0.
- **The data ask for nothing lower.** The levels fitted to earlier seasons' results swing between seasons, and they make the xG error worse.
- **The minutes are too few to matter in-season**, at under 2% of the 5v5 skater-minutes.
- **In openers, where they matter, the newcomers rate about average.** Most of them had played in the NHL before and joined over the summer.

The expansion case fits that pattern: an expansion draft takes NHL regulars, so a newcomer level would put Vegas's first game near 0 anyway.

## Backtest evidence

`reports/lineups/replacement-level-20261030-bc2e843.md`, from `scripts/replacement_level.py`.
- **What it scores:** B3's 5v5 xG difference with the replacements rated at each level, on 2012-13 to 2017-18 (ADR 0011). Each level comes from earlier seasons only.
- **The figures below** are each option's minus zero's, with 95% weekly block bootstrap intervals. The openers' intervals resample whole seasons instead, since each season's openers fall in one week.
- **The check:** with the replacements at 0, the script's 5v5 difference is `b3.team_goals`'s.

| Games | Option | Win log loss | 5v5 xG difference, squared error |
| --- | --- | --- | --- |
| Every game (6,911) | fringe | +0.00006 [−0.00002, +0.00019] | −0.00002 [−0.00020, +0.00014] |
| | prior | +0.00003 [−0.00001, +0.00009] | −0.00001 [−0.00009, +0.00007] |
| | newcomer | +0.00001 [−0.00001, +0.00003] | −0.00000 [−0.00005, +0.00005] |
| In-season, replacements 3% or more of a side's minutes (580) | fringe | −0.00005 [−0.00032, +0.00019] | +0.00003 [−0.00048, +0.00057] |
| | prior | −0.00005 [−0.00018, +0.00007] | +0.00007 [−0.00018, +0.00033] |
| | newcomer | −0.00005 [−0.00020, +0.00008] | +0.00004 [−0.00023, +0.00032] |
| Either team's first game of the season (106) | fringe | +0.00388 [−0.00117, +0.01029] | −0.00065 [−0.01026, +0.00932] |
| | prior | +0.00188 [−0.00066, +0.00520] | −0.00054 [−0.00522, +0.00419] |
| | newcomer | +0.00061 [−0.00021, +0.00170] | +0.00025 [−0.00132, +0.00228] |

The levels fitted to earlier seasons' results make the 5v5 xG error worse on every game: +0.00136 [+0.00045, +0.00237].

Only the 5v5 part of Δĝ is scored. B3 rates the replacements 0 on the power play and the penalty kill too.

## Consequences

- **No code changes.** ADR 0018's replacement rows and ADR 0023's rating of 0 stand.
- **The known weakness is the openers.** A team's first game of a season carries about a quarter of its 5v5 minutes as replacements, and they stand for its summer arrivals, most of whom have played in the NHL before. That is ADR 0017's limit (summer trades and signings unseen until a boxscore), not a rating problem. Rating those minutes lower would push the openers the wrong way.
- **The script stays** to rerun the comparison when the lineup model or RAPM changes.

## Revisit when

- A point-in-time roster source lets the lineup model project summer arrivals from their earlier teams' boxscores (ADR 0017's own revisit). The replacements would then be call-ups, and a level below 0 could help.
- RAPM's reference skater moves, for example through ADR 0050's debut-season priors, and the candidates' average moves away from 0.
- An expansion team joins with a roster unlike an expansion draft's.
