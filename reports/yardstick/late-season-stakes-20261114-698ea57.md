# Late-season stakes (late-season-stakes-20261114-698ea57)

#422 (#417), each definition fixed on the issue before any figure was read (the comment
of 2026-11-14). The fit season is 2012-13 and the explore half 2013-14 and 2014-15.
Intervals are 95% weekly block bootstrap. Not fresh: earlier studies read these seasons,
so every p-value is development evidence only.

**In-sample settings:** the yardstick's features were tuned on these seasons, through
2018-04-09 10:00:00+00:00, and are read at their data's time under the training seasons'
exception (ADR 0053, ADR 0011). None of the yardstick's figures is out of sample.

## Step 1: the standings

- **The check:** 14,430 team-dates on 481 dates;
  games played and points differ from the lake's on 0.
- **Team-seasons flagged:** clinched 48,
  eliminated 41; our clinch bound
  47.
- **Flags lost** (a clinch that disappears later in the season): 0
  (must be 0).
- **Our clinch bound before the API's flag:** 0 team-dates
  (must be 0). The API's clinch leads our bound's by a median of
  0.0 days (at most 4;
  the run stops beyond 14).
- **Against the playoffs:** clinched teams that missed them,
  0; eliminated teams that played in them,
  0 (both must be 0).

## Step 2: the lineups

Team-games after the team's 65th: 1,020, of them
309 flagged. The surprise is in skater-minutes per
team-game; positive means more of the projection's minutes missing than it expected.

| Figure | Flagged less unflagged | p | Flagged team-games |
| --- | --- | --- | --- |
| **Clinched or eliminated (the test)** | +4.28 [+0.68, +6.45] | 0.0240 | 309 |
| Clinched | +6.12 [-0.25, +9.69] | 0.0540 | 148 |
| Eliminated | +2.59 [+0.44, +4.43] | 0.0220 | 161 |
| Starting goalie (share, not minutes) | +0.075 [-0.027, +0.135] | 0.1090 | 309 |
| Games 50 to 65, flagged later less the rest | -0.80 [-2.68, +1.31] | 0.4180 | 896 |

Mean surprise: flagged +2.02, unflagged -2.26.

## Step 3: the yardstick

Move skill in thousandths, the candidate less B3's split alone (negative anticipates the
move). Explore games without standings for a side at the prediction time: 7 (counted as unflagged).

| Candidate | All games | Rested | On games with the flag | Games with the flag | Result beyond the close |
| --- | --- | --- | --- | --- | --- |
| clinched | +0.047 [-0.094, +0.216] | -0.011 [-0.114, +0.109] | +2.914 [+1.009, +4.425] | 101 | +0.261 [-0.751, +1.242] |
| eliminated | +0.039 [-0.198, +0.369] | -0.094 [-0.231, +0.058] | +5.798 [+2.988, +8.503] | 113 | +0.478 [-0.439, +1.450] |

Each column's move slope on its own (log-odds per unit):

- `clinched_diff`: -0.0448 [-0.0627, -0.0208]
- `eliminated_diff`: -0.0047 [-0.0274, +0.0206]

**The standings two days before** (shown beside): an opener posted the evening before
(ADR 0006) can predate the night's results that flip a flag, and the rested games don't
control for that, since another team's game can flip it. These read the standings of
two days before, public before any such opener.

| Candidate | All games | On games with the flag | Games with the flag |
| --- | --- | --- | --- |
| clinched (two days before) | +0.053 [-0.103, +0.246] | +3.800 [+2.204, +5.868] | 93 |
| eliminated (two days before) | -0.003 [-0.188, +0.248] | +4.674 [+2.445, +6.805] | 109 |

## The decision

Holm's step-down at 0.05 over the three tests; a test holds when it passes with its
expected sign.

| Test | Estimate | Expected sign | p | Threshold | Holds |
| --- | --- | --- | --- | --- | --- |
| lineups | +4.28 [+0.68, +6.45] | + | 0.0240 | 0.0167 | no |
| clinched | +0.047 [-0.094, +0.216] | - | 0.6150 | 0.0250 | no |
| eliminated | +0.039 [-0.198, +0.369] | - | 0.8930 | 0.0500 | no |

**Kept for the owner:** none.
