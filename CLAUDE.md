# NHL Edge Model

Moneyline model that must add information beyond a recalibrated market.
Read docs/plan.md section 5 for the model specification, docs/model-card.md for current state,
and docs/decisions/ before changing a modeling choice.

## Commands
- `uv sync` install, `uv run nhl --help` CLI
- `uv run pytest -m "not slow"` fast tests, `uv run pytest` full suite
- `uv run ruff check . && uv run pyright` lint and types
- `uv run nhl backtest` walk-forward backtest on development seasons

## Hard rules
1. Point-in-time only. Every input has observed_utc before the prediction time, and every fitted
   component has train_cutoff before the fold start. The backtest refits everything per fold.
   Every new feature gets a test in tests/leakage/.
2. The moneyline settles on the full game including OT and shootout. Never compare a regulation
   probability with a moneyline price.
3. De-vig only with market/devig.py. Every model is compared with B1 (recalibrated market), and
   the player layer (B3) with the team-and-goalie model (B2).
4. Select bets on expected return at the executable price (p * odds - 1), never on probability gap.
5. The last fresh pre-game snapshot is a closing proxy. Store last_update with every quote.
6. The blend trains only on out-of-sample predictions from earlier folds.
7. Report metrics with intervals from a weekly block bootstrap. No pass or fail on point estimates.
8. Probability gaps above 8 points get a manual review. Never tune a model to make them disappear.
9. Backtest lineups use only earlier boxscores. Never use a game's own lineup to predict it.
10. Never modify data/raw/ or tests/golden/. Never call paid endpoints unless the prompt says so.

## Conventions
- Python 3.12, Polars in src/, type hints everywhere, one pandera schema per table in lake/schemas.py
- All times UTC; seasons as 20252026; team codes as NHL API triCode
- Artifact version string: <component>-<yyyymmdd>-<shortsha>, stored with train_cutoff on every output
- One modeling change per PR, with the backtest delta and its interval in the PR description

## Workflow
- Tasks are GitHub issues in milestones P0 to P6; the SessionStart hook lists the open ones in
  the earliest milestone. Take [priority] issues first, then the lowest number. A phase's summary
  issue is broken into task issues when the phase starts. docs/plan.md's status column is not kept.
- One branch and PR per issue (phase-N/<topic>), with "Closes #n" in the PR description. Work
  found along the way becomes a new issue, not extra scope. Resume unfinished work from its open PR.
- Start each phase in plan mode and paste the approved plan into the PR description
- After changing features or models: /leakage-check, then /run-backtest
- Record modeling decisions with /adr

## Review budget (Codex)
- Two rounds per PR: the automatic review, then one requested re-review after fixes. A third
  round only after a P0 fix.
- Fix before merge: P0 and any hard-rule violation, whatever its label. P1: fix if it is in this
  PR's diff and small, otherwise a follow-up issue. P2 and below never block.
- Hooks are best-effort guards against accidents. Findings that need a deliberate bypass are
  won't-fix.
- Reply to every finding: "Fixed in <sha>", "Follow-up #n" or "Won't fix: <reason>". Ready to merge
  when CI is green, no P0 is open and every finding has a reply. Use /review-triage.
