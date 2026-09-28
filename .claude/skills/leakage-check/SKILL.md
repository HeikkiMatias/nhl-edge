---
name: leakage-check
description: Run the leakage-auditor subagent on the current diff to find look-ahead leakage. Use after
  any change to features, ratings, lineups, the blend or backtest code, and before merging a modeling PR.
context: fork
agent: leakage-auditor
background: false
---
Audit the current change for look-ahead leakage.

1. Collect the change: `git diff main...HEAD` for committed work on this branch, plus `git diff HEAD` and
   `git status --short` for uncommitted and untracked files. If $ARGUMENTS names files or a PR, audit
   those instead.
2. For every changed feature, join, fitted component or aggregation, apply your checklist: inputs public at
   the odds snapshot time, fitted components trained only before the fold start and refit per fold.
3. Check that every new module in src/nhl_edge/features/ has its own tests/leakage/test_<module>.py.
4. Run `uv run pytest tests/leakage -q`.
5. Report each finding as file:line, what leaks, and the fix. End with PASS or FAIL.
