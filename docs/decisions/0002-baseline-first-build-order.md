# 0002. Build the team-and-goalie baseline before the player layer

- Status: Accepted
- Date: 2026-09-28

## Context

The goal is a moneyline model that adds information beyond the recalibrated market (B1). The player layer (B3) is the main hypothesis, but it is also the most expensive part to build: stints, RAPM, priors and lineup projection. Closing moneylines are close to efficient, so the plan needs an early, cheap signal about whether any edge exists (docs/plan.md sections 6 and 9).

## Options

1. **Player layer first.** Build RAPM and lineup projection straight away and compare them with the market. Months of work pass before the first verdict, and without a simpler model to compare against, a gain cannot be attributed to the player information.
2. **Both in parallel.** This is faster in calendar time. The two models would share untested components such as xG and the win-probability link, and splitting the work makes gate 1 later and less clear.
3. **Team-and-goalie baseline first.** B2 is built in phase 2 and tested against B1 at gate 1. The player layer follows in phase 3 and must beat B2 at gate 2.

## Decision

Option 3. B2 predicts the full-game home win probability from team strength, goalie effect, rest and travel through a regularized logistic model. B3 later replaces the team strength and goalie terms inside the same logistic model, so the two models differ only in how strength is measured.

Gate 1 is a checkpoint, not a stop. The player layer is built either way, because it is the hypothesis being tested, but a baseline that adds nothing lowers expectations for the rest.

## Backtest evidence

None yet. Gate 1 produces the first evidence: the B2 against B1 paired log-loss difference with its interval, pooled and per season.

## Consequences

- xG, the goalie effect, rest and travel, the season home term and the logistic link are built and validated once in phase 2, then reused by B3.
- Every player-layer change is judged against B2, overall and on games after trades, injuries and lineup changes.
- The one-time 2025-26 hockey-only test is spent at gate 2, on B3 against B2.
- If B2 and B3 both fail against B1, v2 moves to totals, props and timing edges instead of more moneyline modeling.

## Revisit when

Gate 1 or gate 2 gives a result that makes the order irrelevant. For example, B3 is abandoned, or a later model needs a different baseline to compare against.
