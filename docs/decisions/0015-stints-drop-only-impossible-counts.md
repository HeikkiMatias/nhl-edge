# 0015. Stints leave out only impossible on-ice counts

- Status: Proposed
- Date: 2026-10-01

## Context

RAPM (#101) fits player ratings on stints: the stretches of a game in which the players on the ice do not change, cut from the shift chart (#97). Plan §9 says to "drop stints where on-ice counts contradict the strength state", meaning the play-by-play's `situationCode`. ADR 0009 later decided that a complete shift chart is the better record where the two disagree: `situationCode` loses track of a penalty in some games of 2019-20 and 2020-21 and stays a skater off for the rest of the game (#28). So the plan's rule would now throw away the stretches ADR 0009 trusts. That has to be settled before the stint table is built. The number 0014 is kept for gate 1's ADR (#79).

The first sizing was a prototype on 2018-19 to 2021-22 in the phase 3 session of 2026-10-01: data coverage only, no results, ratings or model figures. The table below is from the built `stints` table on the open seasons, 2010-11 to 2021-22. 2025-26 and the live season were not looked at.

## Options

1. **Only impossible counts.** Leave a stint out only when the chart itself is impossible: a team with fewer than 3 or more than 6 skaters, or two goalies. Keeps ADR 0009's reading of the chart. It departs from plan §9's wording.
2. **Any that disagree, as plan §9 reads.** Also leave a stint out when the chart's skater count differs from `situationCode` at a shot inside it. In the prototype it left out 1.80% of 2019-20's playing time but 4.89% of its xG, mostly in the drifted games. It can only catch stints that contain a shot, so it removes shooting more than ice time, which tilts the ratings toward players whose shifts had fewer shots.

## Decision

Option 1, chosen by the owner on 2026-10-01. A complete chart accounts for every player's time on ice, so where it is possible it is kept, as ADR 0009 keeps it for shots and strength time.

The rule, per stint, by the shift chart:
- **Skaters:** a team with fewer than 3 or more than 6 skaters (6 is a pulled goalie) gives `drop_reason` "skaters". The range is ADR 0009's `CHART_SKATERS`, not tuned.
- **Goalies:** a team with two goalies on the ice gives "goalies", and that team's goalie is null.
- **Everything else is kept,** including stretches whose count differs from `situationCode`, a goalie in with six skaters, and a net empty by the chart.
- Left-out stints stay in `stints` with their `drop_reason`, so the audit can report what was left out and why. RAPM reads only the rows without one.

Measured on the built table (`nhl stints`, open seasons):

| Season | Stints left out (skaters, goalies) | Games | Seconds | Share of stint time | Share of xG |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2010-11 | 9 (9, 0) | 4 | 84 | 0.002% | no xG |
| 2011-12 | 7 (7, 0) | 6 | 76 | 0.002% | 0.002% |
| 2012-13 | 6 (6, 0) | 3 | 116 | 0.004% | 0.001% |
| 2013-14 | 7 (7, 0) | 6 | 71 | 0.002% | 0.002% |
| 2014-15 | 13 (8, 5) | 9 | 162 | 0.004% | 0.002% |
| 2015-16 | 20 (11, 9) | 12 | 219 | 0.005% | 0.005% |
| 2016-17 | 17 (9, 8) | 9 | 217 | 0.005% | 0.004% |
| 2017-18 | 21 (2, 19) | 9 | 186 | 0.004% | 0.006% |
| 2018-19 | 24 (7, 17) | 13 | 282 | 0.006% | 0.004% |
| 2019-20 | 169 (169, 0) | 94 | 1,738 | 0.045% | 0.028% |
| 2020-21 | 154 (154, 0) | 76 | 1,479 | 0.048% | 0.022% |
| 2021-22 | 21 (18, 3) | 12 | 246 | 0.005% | 0.009% |

The rule removes at most 0.05% of a season's stint time. What RAPM loses is mostly games without a complete chart (7 to 31 a season): the stints it keeps hold 96.9% to 99.4% of each season's playing time and 97.0% to 99.4% of its xG (the audit report's stints section).

## Backtest evidence

None yet. Nothing reads stints until RAPM (#101), and the decision shows in a backtest first through B3 (#106) at gate 2 (#107).

## Consequences

- `features/stints.py` builds `stints` from complete charts only, with `drop_reason`; `lake/schemas.py` checks that a stint is left out exactly when its counts are impossible.
- The audit report's stints section gives each season's stints left out per reason and, for the open seasons, the share of playing time and xG kept.
- ADR 0009's consequence on RAPM and plan §9's risk row now point here.
- A chart error that stays possible, such as a player left on the ice too long, stays in RAPM's rows. A complete chart limits it: every player's shifts add up to his time on ice within a minute.

## Revisit when

- **A season's kept share falls** visibly below the others' in the audit, in a season that may inform design.
- **Or `shift_coverage` shows a complete chart that disagrees with `situationCode` for long stretches** in a season without drift, ADR 0009's own reopening condition.
