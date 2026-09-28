# Model card

Current production model, how it scored, and its known weaknesses. It is updated only through the `run-backtest` skill, and the accepted metrics live in `reports/backtest/accepted.json`.

## Current model

None. Phase 0 (setup): no data ingested and no models fitted.

## Metrics

Every metric is reported with a 95% weekly block bootstrap interval, pooled over test seasons and per season.

| Metric | B0 | B1 | B2 | B3 | Blend |
| --- | --- | --- | --- | --- | --- |
| Log loss | | | | | |
| Paired log-loss difference against B1 | | | | | |
| Calibration intercept | | | | | |
| Calibration slope | | | | | |
| E3 CLV under the frozen policy | | | | | |

## Artifact versions

| Component | Version | train_cutoff |
| --- | --- | --- |

## Known weaknesses

- Not yet assessed.

## Run history

See `reports/backtest/runs.csv`.
