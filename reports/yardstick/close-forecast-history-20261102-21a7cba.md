# The close forecaster on the training seasons: close-forecast-history-20261102-21a7cba

#369, for ADR 0054. SBR's opener and close, de-vigged multiplicatively, on the
yardstick's rows (ADR 0053). Move skill is in thousandths of squared log-odds against
no move; negative means the forecast beats it. Intervals are 95% weekly block
bootstrap. The features' settings were tuned on these seasons (ADR 0011): development
evidence only.

## 1. The frozen fit

On 6,896 games from 2012-13 to 2017-18, train_cutoff 2018-04-09 10:00:00+00:00.

| Weight | Value |
| --- | --- |
| a | +0.0219 |
| b_o | -0.1287 |
| five_diff | +0.3074 |
| special_diff | +0.1496 |
| finishing_diff | +0.0813 |
| goalie_diff | +0.0781 |

In sample, its move skill is -1.029 [-1.363, -0.732], against a no-move
error of 14.162.

## 2. Out of sample

Each season fitted on the earlier ones from 2012-13.

| Seasons | Games | Move skill | Rested games | Against the opener's drift |
| --- | --- | --- | --- | --- |
| 2013-14 to 2017-18 | 6,176 | -0.510 [-0.825, -0.207] | -0.633 [-0.937, -0.328] | -0.965 [-1.256, -0.701] |

The forecast moves average 0.66 probability points. 21% of games
have one of 1 point or more, and 3% of 2 points or more.

## 3. A simpler form: k of the way toward B3's whole difference

| Seasons | Games | k by season | Its move skill | Split's on the same games | Simpler less split |
| --- | --- | --- | --- | --- | --- |
| 2014-15 to 2017-18 | 4,949 | 0.072, 0.080, 0.102, 0.124 | -0.518 [-0.751, -0.312] | -0.969 [-1.303, -0.663] | +0.451 [+0.276, +0.629] |

## 4. Taking at the opener

The opener's median margin is 3.8%. Each taken side is valued at the
de-vigged close.

| Expected return above | Sides taken | CLV | Without the moves above 8 points (diagnosis) |
| --- | --- | --- | --- |
| 0% | 238 | +0.50 [-0.72, +1.85]% | 229: -1.68 [-2.25, -1.05]% |
| 1% | 70 | +4.50 [+2.14, +7.29]% | 63: -1.49 [-2.71, -0.16]% |

51 games moved more than 8 points from the opener to the close.
Setting them aside reads the close, so it is a diagnosis, not a rule.
