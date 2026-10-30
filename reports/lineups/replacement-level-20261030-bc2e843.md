# Replacement skaters' rating, replacement-level-20261030-bc2e843

The evidence for ADR 0051 (#134), from `scripts/replacement_level.py`: B3's 5v5 xG difference with its replacement skaters rated at each level, scored on 2012-13 to 2017-18 (ADR 0011). Each level comes from earlier seasons only.

## How many minutes

The replacements' share of each season's projected 5v5 skater-minutes, and of the minutes in a team's first game of the season.

| Season | Every game | First games |
| --- | ---: | ---: |
| 20122013 | 2.1% | 27.0% |
| 20132014 | 2.0% | 24.1% |
| 20142015 | 1.8% | 25.2% |
| 20152016 | 1.8% | 25.6% |
| 20162017 | 1.8% | 27.4% |
| 20172018 | 1.8% | 27.1% |

## Who the newcomers are

The skaters who dressed without being candidates (ADR 0017), whom the replacement rows stand for, and the share of them with an NHL rating before their game: most had played in the NHL before.

| Season | In-season newcomers | Rated before | Newcomers in first games | Rated before |
| --- | ---: | ---: | ---: | ---: |
| 20122013 | 434 | 76% | 130 | 82% |
| 20132014 | 677 | 82% | 140 | 81% |
| 20142015 | 638 | 84% | 154 | 84% |
| 20152016 | 663 | 81% | 142 | 84% |
| 20162017 | 644 | 83% | 157 | 77% |
| 20172018 | 597 | 83% | 165 | 85% |

## The levels

Each season's level per role, 5v5 offense plus defense in xG per hour; 0 is RAPM's reference skater. The newcomer level has one value for a team's first game of a season (opener) and one for its other games (in-season).

| Season | Fringe F | Fringe D | Prior F | Prior D | Newcomer F, in-season | Newcomer D, in-season | Newcomer F, opener | Newcomer D, opener |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20122013 | -0.003 | +0.003 | +0.000 | +0.000 | -0.001 | -0.004 | +0.000 | +0.000 |
| 20132014 | -0.027 | -0.044 | -0.014 | -0.018 | -0.014 | -0.016 | -0.002 | -0.015 |
| 20142015 | -0.027 | -0.053 | -0.011 | -0.025 | -0.014 | -0.027 | -0.002 | -0.018 |
| 20152016 | -0.026 | -0.050 | -0.009 | -0.027 | -0.014 | -0.029 | +0.002 | -0.017 |
| 20162017 | -0.026 | -0.048 | -0.010 | -0.027 | -0.014 | -0.031 | +0.002 | -0.018 |
| 20172018 | -0.028 | -0.051 | -0.012 | -0.031 | -0.016 | -0.035 | +0.000 | -0.019 |

## Predictions

Each option's minus zero's, negative when it predicts better: the home team's full-game win log loss through a win model on the 5v5 xG difference, and the squared error of the game's realized 5v5 xG difference through a line, both fitted on the earlier seasons' games before the fold starts. Intervals are 95% weekly block bootstrap, except the first games', which resample whole seasons, since each season's fall in one week.

**Every game** (6,911 games)

| Option | Win log loss | 5v5 xG difference |
| --- | --- | --- |
| fringe | +0.00006 [-0.00002, +0.00019] | -0.00002 [-0.00020, +0.00014] |
| prior | +0.00003 [-0.00001, +0.00009] | -0.00001 [-0.00009, +0.00007] |
| newcomer | +0.00001 [-0.00001, +0.00003] | -0.00000 [-0.00005, +0.00005] |
| fitted | +0.00021 [-0.00029, +0.00069] | +0.00136 [+0.00045, +0.00237] |

**In-season games** (6,805 games)

| Option | Win log loss | 5v5 xG difference |
| --- | --- | --- |
| fringe | +0.00000 [-0.00003, +0.00003] | -0.00001 [-0.00008, +0.00005] |
| prior | -0.00000 [-0.00002, +0.00001] | -0.00000 [-0.00004, +0.00003] |
| newcomer | -0.00000 [-0.00002, +0.00001] | -0.00000 [-0.00004, +0.00003] |
| fitted | +0.00012 [-0.00037, +0.00058] | +0.00101 [+0.00024, +0.00186] |

**In-season, replacements 3% or more of a side's minutes** (580 games)

| Option | Win log loss | 5v5 xG difference |
| --- | --- | --- |
| fringe | -0.00005 [-0.00032, +0.00019] | +0.00003 [-0.00048, +0.00057] |
| prior | -0.00005 [-0.00018, +0.00007] | +0.00007 [-0.00018, +0.00033] |
| newcomer | -0.00005 [-0.00020, +0.00008] | +0.00004 [-0.00023, +0.00032] |
| fitted | +0.00058 [-0.00281, +0.00400] | +0.00642 [-0.00011, +0.01307] |

**Either team's first game of the season** (106 games)

| Option | Win log loss | 5v5 xG difference |
| --- | --- | --- |
| fringe | +0.00388 [-0.00117, +0.01029] | -0.00065 [-0.01026, +0.00932] |
| prior | +0.00188 [-0.00066, +0.00520] | -0.00054 [-0.00522, +0.00419] |
| newcomer | +0.00061 [-0.00021, +0.00170] | +0.00025 [-0.00132, +0.00228] |
| fitted | +0.00553 [-0.00592, +0.02168] | +0.02441 [-0.00497, +0.05552] |

Only the 5v5 part of Δĝ is scored. B3 rates the replacements 0 on the power play and the penalty kill too, where they hold similar shares of the minutes.
