# Yardstick: confirm, yardstick-20261101-e331828

The market-relative yardstick (ADR 0053) on 20152016 to 20172018, each season
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
| B3 split | 3,731 (2,613) | -1.294 [-1.752, -0.902] | -1.429 [-1.828, -1.089] | -0.469 [-1.638, +0.708] |  |
| delta_s | 3,731 (2,613) | -0.683 [-0.898, -0.490] | -0.752 [-0.946, -0.569] | -0.122 [-0.943, +0.713] | +0.116 [+0.078, +0.156] |

The baseline's mean squared error, on 3,731 games, is 16.334 thousandths.

## Move skill per season (in thousandths)

| Candidate | 20152016 | 20162017 | 20172018 |
| --- | --- | --- | --- |
| B3 split | -1.372 [-2.567, -0.491] | -1.783 [-2.381, -1.258] | -0.744 [-1.056, -0.437] |
| delta_s | -0.738 [-1.241, -0.333] | -1.031 [-1.405, -0.716] | -0.293 [-0.531, -0.072] |

## The claim: early-information

Its tests are one family: Holm's step-down at 0.05 over their two-sided p-values
(ADR 0048). A test holds when it passes and its estimate is negative.

| Test | Estimate (thousandths) | p-value | Holm threshold | Holds |
| --- | --- | --- | --- | --- |
| B3 split: move_rested | -1.429 [-1.828, -1.089] | 0.0000 | 0.0250 | yes |
| delta_s: move_rested | -0.752 [-0.946, -0.569] | 0.0000 | 0.0500 | yes |
