# Yardstick: explore, yardstick-20261030-dafedeb

The market-relative yardstick (ADR 0053) on 20122013 to 20142015, each season
fitted on the earlier ones from 2011-12, or from a feature's first season with its
inputs (2012-13 for finishing, ADR 0022), and scored only after it. Intervals are 95%
weekly block bootstrap.

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
| delta_s | 3,165 (2,215) | -0.203 [-0.348, -0.072] | -0.331 [-0.522, -0.167] | -0.307 [-1.598, +0.999] | +0.172 [+0.137, +0.211] |
| delta_g | 3,165 (2,215) | -0.029 [-0.102, +0.048] | +0.059 [-0.039, +0.156] | +0.612 [-1.044, +2.219] | +0.103 [+0.069, +0.136] |
| h_s | 3,165 (2,215) | +0.105 [+0.035, +0.161] | +0.159 [+0.115, +0.203] | -0.205 [-0.643, +0.214] | +0.094 [-0.098, +0.275] |
| delta_g_hat | 3,165 (2,215) | -0.072 [-0.328, +0.176] | -0.121 [-0.411, +0.146] | +0.961 [+0.031, +1.931] | +0.098 [+0.081, +0.115] |
| five_diff | 3,165 (2,215) | -0.257 [-0.516, -0.017] | -0.432 [-0.754, -0.135] | -0.491 [-2.317, +1.232] | +0.158 [+0.131, +0.188] |
| special_diff | 3,165 (2,215) | -0.058 [-0.130, +0.013] | -0.094 [-0.166, -0.025] | +0.723 [-0.193, +1.726] | +0.292 [+0.201, +0.386] |
| finishing_diff | 2,445 (1,717) | -0.019 [-0.045, +0.006] | -0.005 [-0.035, +0.027] | +0.163 [-0.217, +0.544] | -0.007 [-0.052, +0.037] |
| goalie_diff | 3,165 (2,215) | -0.022 [-0.095, +0.056] | +0.067 [-0.033, +0.165] | +0.603 [-1.055, +2.157] | +0.104 [+0.069, +0.139] |
| B2 inputs | 3,165 (2,215) | -0.138 [-0.359, +0.061] | -0.110 [-0.365, +0.128] | +0.235 [-1.932, +2.321] |  |
| B3 split | 2,445 (1,717) | -0.464 [-0.842, -0.134] | -0.554 [-1.061, -0.121] | +3.323 [-0.126, +6.690] |  |
| all | 2,445 (1,717) | -0.453 [-0.845, -0.121] | -0.454 [-0.985, -0.010] | +3.620 [-0.139, +7.273] |  |

The baseline's mean squared error, on 3,165 games, is 11.903 thousandths.

## Move skill per season (in thousandths)

| Candidate | 20122013 | 20132014 | 20142015 |
| --- | --- | --- | --- |
| delta_s | -0.073 [-0.338, +0.167] | -0.068 [-0.287, +0.144] | -0.414 [-0.644, -0.203] |
| delta_g | -0.224 [-0.356, -0.077] | +0.024 [-0.097, +0.156] | +0.033 [-0.095, +0.147] |
| h_s | +0.388 [+0.081, +0.624] | +0.045 [+0.014, +0.077] | -0.003 [-0.015, +0.009] |
| delta_g_hat | -0.671 [-1.131, -0.204] | +0.694 [+0.252, +1.157] | -0.489 [-0.896, -0.132] |
| five_diff | -0.349 [-0.724, -0.003] | +0.172 [-0.253, +0.597] | -0.636 [-1.053, -0.234] |
| special_diff | -0.049 [-0.175, +0.077] | +0.003 [-0.111, +0.117] | -0.123 [-0.241, -0.013] |
| finishing_diff | not scored | -0.018 [-0.045, +0.012] | -0.021 [-0.063, +0.018] |
| goalie_diff | -0.218 [-0.349, -0.071] | +0.041 [-0.085, +0.180] | +0.031 [-0.087, +0.139] |
| B2 inputs | +0.082 [-0.527, +0.603] | -0.023 [-0.269, +0.225] | -0.383 [-0.715, -0.048] |
| B3 split | not scored | -0.160 [-0.573, +0.246] | -0.770 [-1.345, -0.230] |
| all | not scored | -0.209 [-0.606, +0.188] | -0.698 [-1.299, -0.144] |
