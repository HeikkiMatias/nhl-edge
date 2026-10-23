# 0045. Kalshi execution v2: a taker part, a post-only maker part, and re-valuation

- Status: Proposed (drafted for the owner, 2026-10-23, #321)
- Date: 2026-10-23
- Amends: ADR 0040 items 1, 2 and 9 (the order, its limit and the record). What is bet, its p, its hurdle, its stake rule, the limits in the code, the kill switch and the 11-09 rule are unchanged.

## Context

- **ADR 0040 today:**
  - one order per alerted bet, a limit buy at L, resting until 5 minutes before the start;
  - L is the highest cent that clears the hurdle at the taker fee, the worst case, so every fill clears it, maker or taker;
  - the order is valued once, at the midday decision.
- **The review of 2026-10-23 (#317) and the owner's "many hooks":**
  - **A higher maker limit:** a post-only order always pays the maker fee, a quarter of the taker's, so it can rest higher and still clear the hurdle. At p = 0.55 and a 2.5% hurdle, L_t = 0.51 and L_m = 0.53.
  - **Adverse selection:** a resting bid fills most after news has moved against it, and nothing re-values it between the midday decision and the start.
- **The owner's decisions of 2026-10-23 (#321):**
  - the two limits and the re-valuation apply to the real bets from 2026-11-16, after practice on the demo exchange;
  - Kalshi is the real-time source of when to look again (#319); the Odds API gives Pinnacle's fresh price, within the free tier's 500 credits a month.
- **What doesn't change:** a ladder, and bets on sides the paper policy didn't bet, wait for #320's reading and a new policy version.

## Options

1. **Keep ADR 0040.** It is simplest, but it leaves the maker-fee headroom unused, and its bids rest on the midday valuation.
2. **Two parts, no re-valuation.** More fills at prices that still clear the hurdle, but the same blind resting.
3. **Two parts, plus re-valuation that only lowers or cancels (chosen).** A resting part follows fresh information downward, never upward.
4. **Re-price both ways,** raising the bid when the fair price moves toward it. That chases the price, gives up queue place, and buys most when sellers know more.
5. **Re-run the whole model at each trigger.** B2 and B3 refit at midday in most of a 40-minute job. The decision's B3 is point in time at the decision, and a trigger must act in minutes.

## Decision

Option 3.

1. **The parts, sent at the midday placement:**
   - **Taker part:** an immediate-or-cancel buy at L_t, today's L, sized at L_t's effective price from the alert's stake table. Whatever doesn't fill at once is cancelled by Kalshi.
   - **Maker part:** then a post-only bid at L_m, resting until 5 minutes before the start, with ADR 0040's expiry, pause and moved-start rules. L_m is the highest cent at which p × C ÷ cost − 1 clears the hurdle with the maker fee (rounded up, as ADR 0040 rounds the taker fee). Post-only can't take, so the maker fee is the only one it pays.
   - **Size:** the maker part's contracts keep the two parts together within the stake the alert's table allows at L_m's effective price.
   - **Never twice:** each part has its own client order id, derived as ADR 0040 item 5 derives one, with the part's name.
2. **Re-valuation of a resting maker part:**
   - **When:** at each odds slot. Also, from the decision to the start, on either of these, checked every 15 minutes:
     - a move of 3¢ or more in the market's best Yes bid or ask since its last valuation, from #319's `kalshi_tops`;
     - a change in Daily Faceoff's named starter for either team since the decision.
   - **The fresh price:** each triggered check makes one Odds API h2h call (1 credit, every game), at most 4 a day. Past the cap, triggers wait for the next slot.
   - **The value p′:** the frozen blend recomputed with Pinnacle's fresh de-vigged probability (`market/devig.py`) in place of the decision's. Every other input stays the decision's: B3, u and the hurdle. A quote not fresh by ADR 0033's 5 minutes is no re-valuation.
   - **The rule:**
     - if no cent clears the hurdle at p′, the part is cancelled;
     - if L_m′ is below the resting limit, the part is lowered to L_m′, through Kalshi's amend or a cancel and replace;
     - otherwise it rests as it is, keeping its place in the queue. Never raised.
   - **Starter changes:** a named starter change since the decision cancels the maker part outright, since the decision's B3 assumed the earlier starter.
   - **Kalshi's move is a trigger only,** never an input to p′ (#280, the owner's direction).
3. **The record:**
   - each fill is tagged taker or maker from Kalshi's `is_taker`;
   - each re-valuation is logged with its time, trigger, p′, action and the quote's snapshot;
   - the real record is split at this ADR's date, so ADR 0040's days stay its own.
4. **ADR 0040's 11-09 rule** now judges the maker part's resting fills. If it removes the resting part, only the taker part stays.

## Backtest evidence

None, and none is possible: Kalshi's history isn't ours to use (#280). The evidence is prospective:
- the real fills' CLV for taker and maker apart, against a reference after the fill (#318), from 2026-11-16;
- #320's shadow, which runs the same execution on the market-only and hockey-adjusted fair values.

Every fill still clears the frozen policy's hurdle at its effective price (hard rule 4).

## Consequences

- **Code:**
  - `live/exchange.py`: the maker-fee limit;
  - `live/placement.py`: two parts, their ids, sizes and records;
  - a re-valuation command, run at each slot and by a 15-minute `kalshi-revalue.yml` in the `kalshi-live` environment, dispatched by the timer;
  - an R2 counter for the day's triggered Odds API calls.
  - **Tests:** re-valuation reads only quotes and polls observed before it; Kalshi's prices never reach p′; the limit is never raised; the code's ceilings still hold.
- **Credits:** the slots take about 300 a month, and the triggers at most about 120 more, within the free 500. #254's near-start snapshots would compete for the rest.
- **Before 2026-11-16:**
  - the build and Codex's reviews;
  - a week of practice on the demo exchange (`KALSHI_TRADING=demo`);
  - the owner's review of its records, as ADR 0039 item 6 asked of automated placement before its first real bet.
- **Fewer resting fills after bad news, and more fills at the maker limit:** both show in #318's figures, taker and maker apart.

## Revisit when

- the 11-09 measurement or #318's maker CLV interval says the resting part costs more than it brings (ADR 0040's rule);
- #320's reading supports a ladder or bets on other sides, through a new policy version;
- the free tier's credits run short, or Kalshi changes its fees, post-only, amend or rate limits;
- a fill comes at or after a start, or the code's ceilings refuse a part.
