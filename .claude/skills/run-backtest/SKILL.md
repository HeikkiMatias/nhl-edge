---
name: run-backtest
description: Run the walk-forward backtest for B0 to B3 and the blend, compare with the last accepted
  model, and update the model card. Use after any model or feature change.
allowed-tools: Bash(uv run nhl backtest *) Bash(uv run pytest *)
---
1. Run `uv run pytest tests/leakage -q`. Stop if it fails.
2. Run `uv run nhl backtest --seasons $ARGUMENTS --out reports/backtest/` (default: development
   seasons). Never include 2025-26 or live games unless I say so explicitly.
3. From reports/backtest/summary.json, report with 95% weekly block bootstrap intervals: paired
   log-loss difference of B2 and B3 against B1 and of B3 against B2, pooled and per season;
   calibration intercept and slope; E3 CLV under the frozen policy; count of gaps above 8 points.
4. Compare with reports/backtest/accepted.json and flag any season that got worse.
5. Propose updating accepted.json only if the intervals support it. Never overwrite it without my
   confirmation.
6. Update docs/model-card.md and log the run (seasons, artifact versions) in reports/backtest/runs.csv.
