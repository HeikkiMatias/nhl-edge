# 0049. Challenger `close_model`: Pinnacle's move to the close, predicted at the decision

- Status: Proposed (drafted 2026-10-28 for the owner)
- Date: 2026-10-28
- Part of: the review agenda (#250); the challenger is #260, scored under ADR 0038

## Context

**The policy's problem is the price, not the direction.** On history its bets anticipated the market, with a fair move of +2.83% [+2.04%, +3.62%] per bet. Yet they lost to the close, at a CLV of −1.23% [−2.01%, −0.46%] (ADR 0039). A bet's CLV depends on where the price goes after the decision.

**If what is known at the decision anticipates that move,** a model of the move tests it directly, without going through outcomes. A season's moves have far tighter intervals than its results.

**The data exists:** every predicted v2 game has its decision pair, the day's morning and decision snapshots of every logged book in its run bundle, and a closing proxy after it (ADR 0033, #251).

## Options

1. **A small, shrunk linear model of the move,** on three signed inputs known at the decision, refitted weekly on earlier games. It is cheap and pre-registered, with no input chosen after a reading. The cost: a move is small, and its signal may be too weak to show in a season.
2. **The same with more inputs** (time to the start, goalie and lineup calls, u's parts). It is richer, but each input added to a few hundred games mostly adds noise. They can come later, as their own version.
3. **No model of the close; keep #251's diagnostic only.** Free, but it never tests whether a closing forecast could improve selection.

## Decision (proposed): option 1, as challenger `close_model`, version 1, registered on the day its code merges

- **The target:** y = logit p_close − logit p_dec, in the home side's log-odds.
  - **p_dec** is Pinnacle's decision pair de-vigged multiplicatively (the ledger's p_b0, hard rule 3).
  - **p_close** is its closing proxy taken after the decision's own snapshot (`live/movement.py`), de-vigged the same way.
  - The close is the target only, never an input to its own game.
- **The inputs,** all from the day's run bundle, as the decision saw them:
  - d_B3 = logit p_B3 − logit p_dec, B3's disagreement;
  - m = logit p_dec − logit p_morning, Pinnacle's move since its 07:05 ET pair, or 0 without one;
  - g = the median of the other logged books' de-vigged log-odds less Pinnacle's at the decision snapshot, or 0 when no other book quotes.
- **The model and the fit:** ŷ = a + b₁·d_B3 + b₂·m + b₃·g, by maximum a posteriori linear regression, with priors fixed here: a ~ N(0, 0.02²), each b ~ N(0, 0.5²), and the residual variance estimated from the training rows. The prediction is p̂_close = sigmoid(logit p_dec + ŷ).
- **Training rows (point in time, hard rule 1):** every earlier v2 predicted game with a valid closing proxy. A row comes from an earlier ET week, and both the row and its proxy were public before the week's fold start: its Monday at 12:45 ET, as ADR 0041 sets. The fit changes once a week and never sees its own week.
- **Unfitted:** under 200 training games, the output carries no prediction, under the status `unfitted`.
- **Lake reads:** `paper_ledger` through ADR 0041's point-in-time reader, and the earlier games' closing proxies through the challengers' odds reader, observed before the cutoff, each with its leakage test.
- **Primary measure** (ADR 0038 rule 4): per game, (y − ŷ)² − y², the squared error of the challenger's forecast of the move less that of "no move". Its weekly block bootstrap interval is taken on the games with a close, and negative favours the challenger.
  - **Reported beside it:**
    - p̂_close's log loss on the result, against B1 and the frozen blend;
    - shadow bets: the frozen selection on p_blend, kept only where p̂_close · o − 1 ≥ 0 at the executable price, valued at Pinnacle's proxy (CLV).
  - Each has counts only under 4 weeks, and there is no verdict before 2027-04-12.

## Backtest evidence

None, by design: ADR 0038 spends no history season on a challenger. History also has no decision-time Pinnacle prices (#323). The evidence is its outputs from registration on.

## Consequences

- **The code:**
  - `live/challengers.py`: the registry entry, the inputs from the bundle's odds, and the fit;
  - a leakage test for each input and for the training rows;
  - the report's challenger section, with the squared-error measure.
- **Multiplicity:** it is a fifth candidate. Under ADR 0048 as proposed, the smallest p-value at the review must then be at most 0.01.
- **The first fitted week** comes once about 200 v2 games have a close, at about 40 a week: around mid-November.

## Revisit when

- Near-start snapshots are added (#254), which changes what the close is.
- A fitted week's coefficients sit at their priors' edge, which suggests the priors bind.
- The review on 2027-04-12.
