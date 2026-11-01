# Yardstick: explore, yardstick-20261101-c7bfa52

The market-relative yardstick (ADR 0053) on 20132014 to 20142015, each season
fitted on the earlier ones from 2012-13, the same for every candidate (ADR 0053, as
amended: 2011-12 is the features' burn-in season). Intervals are 95% weekly block
bootstrap.

- **Move skill** (in thousandths): the squared error of the forecast of SBR's move from
  the opener to the close, in log-odds, less that of the opener's own drift. Negative
  means the feature anticipates the move.
- **Rested:** the games where neither team played the day before, so neither team's own
  last game falls between an opener of the evening before and 10:00 ET (ADR 0006).
- **Beyond the close** (in thousandths): the log loss of the close and the feature, less
  the close alone. Negative means the feature adds to the closing price.
- The features' frozen settings were tuned on these seasons' results (ADR 0011): they
  are in-sample in their settings by design.

| Candidate | Games (rested) | Move skill | Rested | Beyond the close | Slope of the move |
| --- | --- | --- | --- | --- | --- |
| delta_s | 2,445 (1,717) | -0.256 [-0.397, -0.140] | -0.314 [-0.499, -0.159] | +0.074 [-1.884, +2.039] | +0.160 [+0.122, +0.201] |
| delta_g | 2,445 (1,717) | -0.034 [-0.127, +0.060] | +0.054 [-0.069, +0.179] | +1.401 [-0.305, +3.159] | +0.054 [+0.018, +0.091] |
| h_s | 2,445 (1,717) | +0.016 [-0.018, +0.050] | +0.082 [+0.042, +0.124] | -0.043 [-0.589, +0.574] | +0.118 [-0.115, +0.339] |
| delta_g_hat | 2,445 (1,717) | -0.340 [-0.607, -0.099] | -0.425 [-0.781, -0.120] | +1.202 [+0.109, +2.268] | +0.080 [+0.062, +0.098] |
| five_diff | 2,445 (1,717) | -0.472 [-0.756, -0.245] | -0.592 [-0.950, -0.302] | -0.279 [-2.225, +1.665] | +0.139 [+0.112, +0.170] |
| special_diff | 2,445 (1,717) | -0.070 [-0.147, +0.003] | -0.083 [-0.159, -0.010] | +2.094 [+0.092, +4.316] | +0.299 [+0.191, +0.410] |
| finishing_diff | 2,445 (1,717) | -0.019 [-0.045, +0.006] | -0.005 [-0.035, +0.027] | +0.163 [-0.217, +0.544] | -0.007 [-0.052, +0.037] |
| goalie_diff | 2,445 (1,717) | -0.018 [-0.112, +0.075] | +0.069 [-0.054, +0.196] | +1.262 [-0.358, +2.955] | +0.053 [+0.015, +0.091] |
| B2 inputs | 2,445 (1,717) | -0.311 [-0.498, -0.142] | -0.199 [-0.454, +0.040] | +1.367 [-1.482, +4.169] |  |
| B3 split | 2,445 (1,717) | -0.464 [-0.842, -0.134] | -0.554 [-1.061, -0.121] | +3.323 [-0.126, +6.690] |  |
| all | 2,445 (1,717) | -0.453 [-0.845, -0.121] | -0.454 [-0.985, -0.010] | +3.620 [-0.139, +7.273] |  |

The baseline's mean squared error, on 2,445 games, is 12.588 thousandths.

## Move skill per season (in thousandths)

| Candidate | 20132014 | 20142015 |
| --- | --- | --- |
| delta_s | -0.097 [-0.217, +0.025] | -0.416 [-0.628, -0.217] |
| delta_g | -0.099 [-0.246, +0.058] | +0.032 [-0.092, +0.143] |
| h_s | +0.023 [-0.020, +0.073] | +0.008 [-0.039, +0.056] |
| delta_g_hat | -0.109 [-0.387, +0.170] | -0.573 [-1.016, -0.197] |
| five_diff | -0.225 [-0.479, +0.027] | -0.720 [-1.122, -0.336] |
| special_diff | -0.002 [-0.070, +0.066] | -0.137 [-0.261, -0.020] |
| finishing_diff | -0.018 [-0.045, +0.012] | -0.021 [-0.063, +0.018] |
| goalie_diff | -0.065 [-0.218, +0.099] | +0.029 [-0.083, +0.130] |
| B2 inputs | -0.250 [-0.423, -0.081] | -0.372 [-0.673, -0.074] |
| B3 split | -0.160 [-0.573, +0.246] | -0.770 [-1.345, -0.230] |
| all | -0.209 [-0.606, +0.188] | -0.698 [-1.299, -0.144] |
