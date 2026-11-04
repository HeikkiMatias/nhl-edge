# close_forecast version 2 on the training seasons: close-forecast-2-history-20261104-863eb1a

ADR 0056. Version 1's split regression (ADR 0054) with #370's five schedule density
columns beside B3's four parts. SBR's opener and close, de-vigged multiplicatively, on
the yardstick's rows (ADR 0053). Skill is each forecast's squared error less no move's,
in thousandths of squared log-odds; negative means it beats no move, and a negative
difference favours version 2. Intervals are 95% weekly block bootstrap. The features'
settings were tuned on these seasons (ADR 0011), and #370 chose density on 2013-14 and
2014-15: development evidence only.

## 1. The frozen fit

On 6,896 games from 2012-13 to 2017-18, train_cutoff 2018-04-09 10:00:00+00:00.

| Weight | Value |
| --- | --- |
| a | +0.0180 |
| b_o | -0.1681 |
| five_diff | +0.3606 |
| special_diff | +0.2047 |
| finishing_diff | +0.1231 |
| goalie_diff | +0.0808 |
| dense3_diff | -0.0136 |
| dense6_diff | -0.0014 |
| mismatch | +0.0662 |
| home_return | -0.0006 |
| away_trip | +0.0020 |

## 2. Out of sample, beside version 1

Each season from 2013-14 fitted on the earlier ones from 2012-13, both versions on the
same games.

| Games | Count | Version 1 | Version 2 | Version 2 less version 1 |
| --- | --- | --- | --- | --- |
| Every game | 6,176 | -0.510 [-0.825, -0.207] | -1.789 [-2.207, -1.423] | -1.279 [-1.512, -1.061] |
| Rested games | 4,330 | -0.633 [-0.937, -0.328] | -1.043 [-1.406, -0.691] | -0.410 [-0.525, -0.297] |
| Back-to-back games | 1,846 | -0.221 [-0.859, +0.349] | -3.540 [-4.600, -2.710] | -3.318 [-4.036, -2.688] |

**The build goes ahead:** version 2 less version 1 is below 0 on the rested games, -0.410 [-0.525, -0.297] (ADR 0056's stop).

## 3. The forecast moves

| Version | Mean size, points | At 1 point or more | At 2 points or more |
| --- | --- | --- | --- |
| 1 | 0.66 | 21% | 3% |
| 2 | 0.94 | 38% | 10% |

## 4. Taking at the opener

The opener's median margin is 3.8%. A side is taken where its forecast
close times its opener price, less 1, is above the threshold (hard rule 4), and valued
at the de-vigged close. Setting aside the moves above 8 points reads the close: a
diagnosis, not a rule. SBR's opener may predate the previous night's result (ADR 0006),
so the rested games are the clean ones.

| Version | Games | Expected return above | Sides | CLV | Without the moves above 8 points (diagnosis) |
| --- | --- | --- | --- | --- | --- |
| 1 | Every game | 0% | 238 | +0.50 [-0.72, +1.85]% | 229: -1.68 [-2.25, -1.05]% |
| 1 | Every game | 1% | 70 | +4.50 [+2.14, +7.29]% | 63: -1.49 [-2.71, -0.16]% |
| 1 | Rested games | 0% | 152 | +0.40 [-1.13, +2.16]% | 147: -1.58 [-2.31, -0.77]% |
| 1 | Rested games | 1% | 43 | +3.57 [+1.46, +6.03]% | 39: -1.43 [-2.88, -0.08]% |
| 2 | Every game | 0% | 725 | +1.08 [+0.52, +1.75]% | 703: +0.08 [-0.26, +0.45]% |
| 2 | Every game | 1% | 293 | +3.13 [+1.83, +4.63]% | 277: +0.88 [+0.25, +1.48]% |
| 2 | Rested games | 0% | 270 | +0.74 [-0.31, +1.84]% | 259: -0.77 [-1.36, -0.16]% |
| 2 | Rested games | 1% | 82 | +4.60 [+1.88, +7.88]% | 74: -0.21 [-1.29, +0.90]% |
