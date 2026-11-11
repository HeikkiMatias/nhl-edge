# Goals and shot attempts beside xG (goals-attempts-20261111-e58aa1f)

#420 (#417), on the yardstick's explore half (2013-14 and 2014-15, fitted from 2012-13),
each definition fixed on the issue before any figure was read (the comment of
2026-11-11). Intervals are 95% weekly block bootstrap. Not fresh: #361, #366, #370 and
#418 read these seasons, and five_diff's own move skill is known, so every p-value is
development evidence only.

**In-sample settings:** the 5v5 xG part's settings (and the yardstick's other features')
were tuned on these seasons, through 2018-04-09 10:00:00+00:00, and are read at their
data's time under the training seasons' exception (ADR 0053, ADR 0011). So is the new
columns' half-life, ΔS's 80 games: their pulls were chosen on 2012-13 alone
(through 2013-04-29 10:00:00+00:00, before the first explore fold), but at that
in-sample half-life. None of these figures is out of sample.

## The pulls, chosen on 2012-13

Half-life 80 games (ΔS's). Loss: Σ (count - rate·minutes)² / minutes over
2012-13's team-games, for and against; the lowest wins, ties to more pull.

| Measure | pull 0 | pull 10 | pull 20 | pull 40 | pull 80 | pull 160 | pull 320 | Chosen |
|---|---|---|---|---|---|---|---|---|
| goals | 110.6 | 110.3 | 110.2 | 110.0 | **110.0** | 110.1 | 110.3 | 80 |
| attempts | **4,716.0** | 4,721.8 | 4,737.9 | 4,779.2 | 4,856.5 | 4,958.2 | 5,055.0 | 0 |
| team_xg | **21.5** | 21.5 | 21.5 | 21.6 | 21.8 | 22.0 | 22.2 | 0 |

**The check of the code:** at ΔS's settings (half-life 80, prior 40), the control
equals the lake's ΔS 5v5 part on 2,445 of 2,445
explore-half games (largest gap 8.88e-16).

## The candidates

Move skill and result beyond the close in thousandths: negative means the candidate
anticipates SBR's move beyond the opener's drift, or adds to the close.

| Candidate | Games | Move skill | On the rested games | Result beyond the close |
|---|---|---|---|---|
| xg part | 2,445 | -0.472 [-0.756, -0.245] | -0.592 [-0.950, -0.302] | -0.279 [-2.225, +1.665] |
| goals | 2,445 | -0.514 [-0.830, -0.267] | -0.702 [-1.125, -0.376] | +0.105 [-1.938, +2.095] |
| attempts | 2,445 | -0.483 [-0.770, -0.250] | -0.600 [-0.972, -0.304] | +0.345 [-1.808, +2.473] |
| both | 2,445 | -0.465 [-0.766, -0.219] | -0.644 [-1.051, -0.318] | +2.028 [-0.247, +4.405] |
| team xg | 2,445 | -0.371 [-0.675, -0.115] | -0.494 [-0.874, -0.186] | -0.247 [-2.165, +1.644] |

**Shown beside, never part of the rule:** the slope of the move on each new column
alone, the calibration slope of the result model's probabilities, and (descriptive)
the correlations of the columns on the explore half's games.

| Column | Slope of the move on it |
|---|---|
| five_goals_diff | +0.131 [+0.084, +0.176] |
| five_attempts_diff | +0.007 [+0.005, +0.008] |
| five_team_xg_diff | +0.157 [+0.122, +0.195] |

| Result model | Calibration slope |
|---|---|
| xg part | +0.897 [+0.725, +1.086] |
| the close alone | +0.953 [+0.768, +1.151] |
| goals | +0.895 [+0.725, +1.080] |
| attempts | +0.886 [+0.708, +1.077] |
| both | +0.856 [+0.690, +1.037] |
| team xg | +0.899 [+0.727, +1.090] |

| Correlation | five_diff | five_goals_diff | five_attempts_diff | five_team_xg_diff |
|---|---|---|---|---|
| five_diff | 1.00 | 0.65 | 0.78 | 0.92 |
| five_goals_diff | 0.65 | 1.00 | 0.63 | 0.65 |
| five_attempts_diff | 0.78 | 0.63 | 1.00 | 0.87 |
| five_team_xg_diff | 0.92 | 0.65 | 0.87 | 1.00 |

## The decision

Holm's step-down at 0.05 over the three paired tests against the 5v5 xG part. A test
holds, and its candidate is kept, when it passes with a negative estimate.

| Test | Paired move skill (thousandths) | p | Holm threshold | Holds | Kept |
|---|---|---|---|---|---|
| goals | -0.042 [-0.122, +0.025] | 0.2520 | 0.0167 | no | no |
| attempts | -0.011 [-0.035, +0.012] | 0.3360 | 0.0250 | no | no |
| both | +0.007 [-0.065, +0.073] | 0.8350 | 0.0500 | no | no |

**Kept:** none.

**Shown beside** (paired against the 5v5 xG part, thousandths). On a back-to-back the
rates know last night's game, which an opener of the evening before may not (ADR 0006).
The control, team xg, is outside the family.

| Pairing | Rested | Back-to-back | 20132014 | 20142015 | Result beyond the close |
|---|---|---|---|---|---|
| goals | -0.110 [-0.208, -0.033] | +0.119 [+0.021, +0.221] | -0.013 [-0.084, +0.059] | -0.071 [-0.218, +0.039] | +0.384 [-0.329, +1.133] |
| attempts | -0.008 [-0.038, +0.022] | -0.019 [-0.080, +0.043] | +0.007 [+0.004, +0.010] | -0.030 [-0.074, +0.015] | +0.624 [-0.320, +1.591] |
| both | -0.052 [-0.135, +0.018] | +0.146 [+0.033, +0.261] | +0.087 [+0.002, +0.169] | -0.074 [-0.187, +0.020] | +2.307 [+0.663, +3.952] |
| team xg | +0.098 [+0.027, +0.175] | +0.109 [-0.028, +0.243] | +0.200 [+0.046, +0.347] | +0.001 [-0.011, +0.013] | +0.032 [-0.072, +0.140] |

The control's paired move skill: +0.101 [+0.024, +0.176] (p 0.0110, outside the family).

## Limits

- **Redundancy:** the measures move together (the correlations above), so any gain is
  small.
- **Luck:** a goals rate can chase it, which is what its pull is for.
- **Overlap:** a goals rate overlaps B3's finishing multipliers (ADR 0022).
- **The market:** sharp bettors use such mixes, so the close likely holds them.
