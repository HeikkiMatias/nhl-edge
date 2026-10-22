# 0040. Real bets placed automatically on Kalshi through its API

- Status: Accepted (the owner, 2026-10-22). On 2026-10-18 the owner chose the venue, the automation, the order type, the currency and the practice on the demo exchange.
- Date: 2026-10-18
- Amends: ADR 0039 item 4 (the stake's currency and rounding on an exchange) and item 6 (the venue and placement). The start rule, the stops, the bankroll and what is bet are unchanged.

## Context

**The owner's decision (2026-10-18):**
- Kalshi is the venue for real bets, placed automatically through its API, with no manual steps.
- Pinnacle stays the manual option.
- The law and tax checks are done (#280 step 1).

**What placement must respect:**
- ADR 0039's real stakes start from 2026-11-16, and its item 6 asks automated placement for "its own review before its first real bet".
- **The contract:** Kalshi's contract pays $1 if a team wins the game, overtime and the league's tiebreak included, so it settles as the moneyline does (hard rule 2).
- **The price:** in dollars, on a 1¢ grid for NHL markets, plus a taker fee of 0.07 × C × P × (1 − P) on C contracts at P. A maker pays a quarter of that, or nothing on markets without maker fees.
- **The account:** in USD. An order takes contracts in steps of 0.01.
- **Timing:** a market stays open during and after its game.

## Options

1. **Manual placement from the alert,** with the decimal price converted for the owner: dropped by the owner, whose reason for Kalshi is the API.
2. **Fill now:** a limit order at the ask, cancelled if not filled at once. It never rests, so it misses fills a resting order would get.
3. **A limit buy at the limit price L, resting until shortly before the start (the owner's choice):**
   - it fills at once from any ask at or below L, and what is left rests;
   - #281's private measurement on 2026-11-09 shows whether the resting part's fills are worth it. It can amend only the execution, and only by the rule below: resting or fill-now. That would be a new ADR, with the real record split at its date. Never p, the hurdle, the selection, the staking or the limit's formula, which the live games must not tune.
   - **The rule, fixed now:** the CLV per filled resting bid against Pinnacle's de-vigged closing proxy, with its interval from the weekly block bootstrap (hard rule 7).
     - **The evidence:** on 11-09, #281's private measurement on the order books #282 logs. There, a bid resting at L counts as filled, with the maker fee, at the first snapshot logged after the decision whose best Yes ask is at or below L. From the start, at each weekly report, the real orders' resting fills, the maker fills (`is_taker` false).
     - **The decision:** if the interval's upper bound is below 0, the resting part goes and orders become immediate-or-cancel at L. Otherwise the order stays as it is.
     - **The floor:** fewer than 4 weeks of resting fills decide nothing.
4. **Post-only at the bid:** the maker fee only, but no fill while the price doesn't come down, and the most adverse selection.

## Decision: option 3

1. **The order:**
   - **Its terms:** a limit buy of Yes on the bet's team (`side: bid`), `good_till_canceled` with `expiration_time` 5 minutes before the listed start, and `cancel_order_on_pause` set.
   - **Its expiry** keeps every fill pre-game, since the market stays open during the game.
   - **Never after the start:** an order is never sent within a minute of its expiry.
   - **A moved game (the leakage audit):** an order goes only while the game's start, as the NHL's schedule lists it then, is still the alert's.
     - Each later run reads the schedule again. A game moved earlier moves the order's deadline with it, and the order is cancelled once that deadline has passed.
     - The earliest start any run has seen holds, written once with the order, so one unread schedule loses no move. A run without the schedule writes no final record and no entry.
     - Without the schedule, no order goes.
   - **A fill at or after the start**, the earlier of the alert's and the schedule's, which the expiry rules out, is kept apart, flagged, and never the bet's (hard rule 2).
2. **The limit L (hard rule 4):**
   - L is the highest whole cent at which the order, sized as in item 3, still clears the bet's hurdle at its effective price: p × C / cost − 1 ≥ hurdle, where cost = C × L + the taker fee rounded up to $0.0001.
   - The taker fee is the worst case, and the cost of one contract rises with its price. So every fill at or below L, maker or taker, clears the hurdle and the stake rule.
   - L is computed from the alert's own numbers only. No Kalshi price is an input: before an order, placement uses only the market's status from Kalshi, and the balance as a check.
3. **The size (amends item 4):**
   - **The stake:** the stake rule's euro stake at L's effective price, from the alert's stake table (`real_ledger.stake_tables`, unchanged), in USD at the owner's deposit rate.
   - **The contracts:** that amount divided by one contract's cost, rounded down to 0.01 contracts.
   - **Kept within the rule:** the count is cut while the cost in euros, plus a half cent, exceeds what the table allows at the order's own effective price. So the record's rounded stake stays within the rule.
   - **No order:** a stake that buys no 0.01 contracts is skipped and logged.
4. **The currency (the owner's choice):**
   - The real bankroll stays 100 € and the record stays in euros, at the deposit rate (USD received per euro), fixed for the season, so exchange-rate drift is ignored.
   - It is set with the venue in `live/real.py`: `Venue("kalshi", 0.0, 0.01, Exchange("live", <rate>))`.
5. **Never twice:**
   - each order's `client_order_id` is derived from its environment, mode, date, game and policy, and Kalshi refuses a second order with it (409);
   - each order is written to R2 before any check and before it is sent, and Kalshi's order id after;
   - a rerun sends nothing for an order already placed, and finds on Kalshi one an interrupted run had sent.
6. **Limits in the code,** before each day's orders, beside ADR 0039's stops, which decide the mode as before:
   - each order's most cost within the policy's max_bet (1.5%) of the real bankroll;
   - the day's orders, an earlier run's included, within max_day (5%);
   - the balance covering them;
   - the market active;
   - **the code's own limits** (#294), whatever the day's alert row in R2 says. The exchange must be one the code names: the demo exchange, or the live venue's, set only by the venue commit, at that venue's rate. The order's cost is recomputed from its contracts and limit and may be no more than its row states. That cost, and the day's together (earlier runs' orders counted from their own records, never from the alert rows), must stay within max_bet and max_day of twice the starting bankroll (`placement.CEILING_BANKROLL`, 200 €), at the code's rate. Without these, anyone able to rewrite R2 could size the orders. An order over the ceiling is refused, not shrunk, so a bankroll nearing 200 € needs the ceiling raised in a commit.
7. **The switch (the kill switch):** the repository variable `KALSHI_TRADING`.
   - **off,** or unset: nothing is placed, and each run cancels our resting orders. `kalshi-cancel.yml` cancels them at once.
   - **demo:** every alerted bet goes to Kalshi's demo exchange, with mock funds.
   - **live:** the real days' bets go to the live exchange, which also needs the venue commit.
8. **Practice (the owner's choice):**
   - the practice alert stays in decimal until the start;
   - the practice days' orders go to the demo exchange, recorded apart (`real/demo/`), never in the practice or real record.
   - The demo run before the start, the live key check (`nhl kalshi check --env live`) and Codex's reviews are the review item 6 asks for.
9. **The record:** once an order's deadline has passed and a run has read the schedule, its fills before the start become the bet's entry (`real/live/entries/`), with the fields the dashboard's form writes.
   - **Mechanical:** the pipeline writes the entry, so no result can shape it.
   - **Corrections:** a correction (`nhl real correct`) only brings it in line with Kalshi's own statement, with its reason, as at any venue (ADR 0039). #287's nightly check compares the two.
   - **Its fields:**
   - **placed:**
     - matched price = contracts ÷ total cost, fees included;
     - stake = total cost ÷ rate;
     - bet id = Kalshi's order id;
     - placed time = the first fill;
   - **not placed:** "not matched", when nothing filled; "missed it", when no order went.

   Settlement, the bankroll and the stops read the entries as before. The orders and fills are kept in R2 and the lake's `real_orders`, privately.
10. **Settlement on Kalshi's rules (#287):**
    - **A postponed game played within 14 days** settles on its result, since Kalshi doesn't void it.
    - **A cancelled game, or one played later:** Kalshi pays its "last fair price", so the bet is held until its payout is known.
    - **The nightly check:** the record is checked against Kalshi's own settlements each night.
11. **Logs and data:**
    - Run logs are public, so a run prints counts only. Prices, sizes, order ids and the balance go to R2 and to the owner's Telegram.
    - The API keys live only in GitHub secrets: the demo key in repository secrets, the live key in the `kalshi-live` environment, restricted to `main` (#294).
    - Kalshi's data is used only for the owner's own orders, under its Developer Agreement (3.1).

## Backtest evidence

None, and none is possible: Kalshi's history isn't ours to use (#280, the Developer Agreement). The limit keeps every fill within the frozen policy's rule. Whether fills come at a worse time than the decision is what #281's private measurement on 2026-11-09 shows.

## Consequences

- **The owner's setup:**
  - API keys for the demo account, in repository secrets, and for the live account, in the `kalshi-live` environment (#294);
  - `KALSHI_TRADING=demo` for practice;
  - before the first real bet: the live key check, the deposit and its rate (the venue commit), then `KALSHI_TRADING=live`.
- **Resting orders are adversely selected:** a resting order fills mostly when the price falls toward L, so the bets that fill late may be worse than the decision's. The record shows each bet's fill time and price.
- **The stake is sized at L:** a fill at a better price costs less than the rule would allow there, so the real stakes run a little under the paper ones.
- **The dashboard's form** stays for practice entries only. #287 shows the automatic orders there.

## Revisit when

- #281's private measurement (2026-11-09), under the rule in Options 3: an execution-only amendment, in a new ADR, with its own leakage audit.
- Kalshi changes its fees, its contract or its API.
- A fill comes at or after a start, or the nightly check finds the record and Kalshi apart.
- A stop rule fires, or the formal review on 2027-04-12.
