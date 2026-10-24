# 0046. Shadow `kalshi_prices`: select on Kalshi's own prices, market-only against hockey-adjusted fair value, with one execution and one budget

- Status: Proposed
- Date: 2026-10-24
- Part of: the external review of 2026-10-23 (#317); the shadow is #320

## Context

- **The paper policy** selects on Pinnacle's price, and its bets go to Kalshi only as orders (ADR 0040, ADR 0045). Nothing measures the bets the policy never makes, where Kalshi's own price sits below a sharp fair value.
- **The review's question:**
  1. Does Kalshi offer prices below a sharp fair value, after fees, depth and the fills a resting bid really gets?
  2. Does the hockey model make those fills worth more than the market alone?
- **What we have:** every game's Kalshi markets, watched densely and privately (#319):
  - `kalshi_tops` each minute;
  - `kalshi_depth` and `kalshi_trades` every 5 minutes.

  Pinnacle is snapshotted at each odds slot, and every fill can be valued against a reference observed after it (#318).
- **Why it is fixed now:** its rules have to be fixed before the first reading. Kalshi's history isn't ours to use (#280), so the evidence can only come from games after this ADR.

## Options

1. **No shadow; wait for the real fills.** The cost is that real fills only exist on the paper policy's own sides, at about 1.5 € a bet, so they can't answer either question.
2. **A taker-only shadow.** It is simple and needs no fill model, but it leaves out the maker fee's headroom (ADR 0045), where most of a resting bid's value lies.
3. **Taker and maker, two arms, one budget per arm, no ladder (chosen).** It answers both questions on the same opportunities, with the execution the real orders use.
4. **The same, with a ladder of bids below the maker bid.** More hooks, but each rung multiplies the fill model's optimism, and the budget's split between rungs is one more choice fixed before any evidence.

## Decision

Option 3, as shadow `kalshi_prices`, version 1. It changes no paper bet, real bet or policy.

1. **Opportunities:** both outcomes (each team's Yes market) of every game on the day's slate, at each odds slot from the midday decision to the start (midday, pre7, pre8, pre10).
   - Each needs a Pinnacle pair fresh by ADR 0033's 5 minutes, and a Kalshi depth snapshot at most 5 minutes old at the slot.
   - Never after the start. Kalshi settles the full game (hard rule 2).
2. **Fair value, two arms on the same opportunities:**
   - **market:** Pinnacle's pair at the slot, de-vigged multiplicatively (`market/devig.py`, hard rule 3).
   - **hockey:** `conservative_blend` version 1 (ADR 0041), with its fit for the week, at the slot's market probability and the decision's p_B3 from the day's ledger.
     - **Before the fit:** in a week it is unfitted (under 200 training games), the arm has no opportunities, and the paired comparison counts only fitted weeks.
     - **A later goalie:** the decision's B3 assumed the decision's starters. From the first slot at which Daily Faceoff names another starter for either team, the game leaves the hockey arm and stays in the market arm. This is ADR 0045's rule, read as the real re-valuation reads it.
     - **No ledger row:** a game without a decision row, such as one published late, is market-only.
3. **Hurdles and stakes** (ADR 0028):
   - **hockey:** 2.5% plus a point per SD of the decision's u, and quarter Kelly divided by (1 + u⁺);
   - **market:** the same 2.5% and quarter Kelly, with u⁺ = 0. The arms then differ only in their fair value.
4. **Execution, identical in both arms:**
   - **Taker:** at the slot, the outcome's Yes asks in the depth snapshot, cheapest first, with the taker fee rounded up as ADR 0040 rounds it.
     - It buys the most contracts the stake allows whose effective cost still clears the hurdle: p × C ÷ cost − 1 ≥ hurdle (hard rule 4).
     - It fills at once at those levels, an upper bound on what a real order would take.
   - **Maker:** then a post-only bid for the stake left, at the highest cent that clears the hurdle at the maker fee, kept a cent below the best ask (post-only never crosses).
     - It rests until 5 minutes before the start, and is re-valued at each later slot as ADR 0045 re-values a real one: lowered or cancelled, never raised. In the hockey arm, a starter change cancels it.
     - **The fill model:** it fills when the trades logged after its posting, at or below its price, exceed the depth ahead of it at its price at posting, or when a logged ask reaches its price. Partial fills count. This is an upper bound on its fills.
   - **No ladder:** one maker bid per opportunity. A ladder would be its own version, registered later, so its reading never mixes with this one's.
5. **One budget per arm:** a notional 100 € bankroll with the frozen policy's caps.
   - **Per game:** at most 1.5% across every hook (taker and maker, every slot), and only one outcome per game: the first to clear.
   - **Per day:** at most 5%.
   - **Resting bids:** a resting maker bid counts as filled until it is cancelled.
   - **Order:** hooks are allocated slot by slot; within a slot by expected return at the executable price, highest first (hard rule 4); and taker before maker.
6. **Measures,** per arm and for taker and maker apart, each with its weekly block bootstrap interval, counts only under 4 weeks (hard rule 7):
   - **net CLV per fill,** at the matched cost with fees, against #318's reference. A fill at or after its game's proxy gets no CLV and is counted apart, never valued against an earlier quote.
   - **the expected profit captured,** in euros, at the reference, and the fills;
   - **the paired difference,** hockey minus market, per opportunity, on the weeks both arms ran;
   - **adverse selection:** the share of maker fills after a named starter changed, or after Pinnacle's fair price moved against the side since posting (as #308 measures).
7. **Point in time:**
   - Every input at a slot is observed at or before it. Fills use only trades logged after the bid's posting and before its expiry, and every reference comes after its fill.
   - The shadow runs nightly on the night's logs, with its leakage tests.
8. **Private:** it reads Kalshi's books and trades (#280), so every figure goes to R2 under `real/shadow` (a private prefix) or to the owner in a session. The log prints counts only, and nothing goes in the repository, its issues or its committed reports.
9. **No verdict before 4 weeks.** A real use is a new policy version, with its ADR, the owner's decision and the scale-up standard (#322).

## Backtest evidence

None, by design: Kalshi's history isn't ours to use (#280), so no season can be replayed. Its evidence is its own nightly readings from the first night after this ADR, and the first reading with intervals comes after 4 weeks of #319's logs.

## Consequences

- **Code:**
  - `live/kalshi_shadow.py`: opportunities, both arms, execution, budget, fills and measures;
  - a nightly `nhl real shadow --r2` step, writing private JSON to R2;
  - tests in `tests/unit/` and `tests/leakage/`;
  - `real/shadow` added to the private prefixes.
- **Depends on:** the Kalshi watch running (#319). It is dispatched by the timer, so it waits for the timer's redeploy. On a day the watch missed there are no opportunities, and that day is counted, not filled in.
- **Optimism:** the taker fills and the maker fill model are both upper bounds, so a negative reading is informative and a positive one needs the real fills (#318) beside it.
- **Credits:** none. It reads only stored snapshots and the watch's logs.

## Revisit when

- **The fill model is too generous:** the real maker parts' fills (#318) fall well short of what the fill model gives the same bids. That makes a new version with a stricter queue.
- **The inputs change:** `conservative_blend` gets a new version, or Kalshi changes its fees, post-only or the fields the watch logs.
- **A ladder or a third arm is wanted:** each is its own version, never a change to this one.
