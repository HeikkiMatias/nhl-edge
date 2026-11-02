# 0055. Two-sided demo quoting on Kalshi, from GitHub runners

- Status: Proposed
- Date: 2026-11-02
- Part of: #375, #358 step 5 brought forward for the demo exchange only. From the owner on 2026-11-02: "do that now and use github runners for now", with paper two-sided quoting the priority.

## Context

**The paper simulator runs** (#358 step 1, `live/kalshi_quotes.py`, `nhl real quotes`, nightly). It keeps each day from 2026-10-31 privately. The first two days had watch logs, and their quotes posted and filled on Kalshi's real books. Its figures are private (#280), and it gives counts only under 4 weeks.

**What the simulator can't show is the mechanics.** It doesn't show whether quotes can actually be posted, moved, pulled and cancelled in time, from where the jobs run, within Kalshi's rate limits, and recorded so they replay.

**The demo exchange tests exactly that, with mock funds.** Its books and fills come from other demo users, not the real market. So it says nothing about adverse selection or profit: those stay the simulator's, on the real logs.

**The jobs run on GitHub-hosted runners for now** (the owner's choice). Kalshi refuses trading calls from some US states (#347: two demo orders on 2026-10-26, from Washington), so a call can be refused on some days. On demo that costs nothing. The Kalshi runner takes over once #347 is up.

## Options

1. **A demo quoting job with the simulator's rules, one fair value and one half-spread.** It is one account on one variant, so its orders never overlap. The cost is a job that steps every minute through the game day.
2. **Several fair values or half-spreads at once.** One account's bids on the same market at several prices would mix their fills, and the demo gives no economics to compare anyway.
3. **Wait for #347's runner.** Cancels would be reliable, but nothing is learned until the owner's machine is up.

## Decision (proposed): option 1, as `demo_quotes` version 1

**The fair value (the owner's pick):** the model, the owner's choice in #358. It is the morning view's blend from its publication, then the decision's from its own.
- **The alternatives:** the model anchored to Kalshi's mid, or the close forecast (ADR 0054) from its first day.

**The rules,** the simulator's (`live/kalshi_quotes.py`) at one half-spread:
- **The quotes:** a post-only bid of 10 contracts on each team's Yes market, at the fair value less 1¢, rounded down to the cent and at least a cent below the best ask.
- **Re-quoting:** a bid is amended when its price would move by a cent or more, and replaced after a fill.
- **Limits:** a net cap of 30 contracts a team, and a day cap of $100 of filled and resting cost, in mock funds.
- **Pulls:**
  - a game's bids are cancelled on a named starter changing in Daily Faceoff's stored polls, or a 3¢ move in a team's mid since posting;
  - the game then waits for its fair value to update, as in the simulator;
  - every bid is cancelled 10 minutes before the start.

**When and where it runs:**
- **The job:** a second job in `kalshi-watch.yml`, so the timer's existing dispatches start it at 07:05, 11:05, 15:05 and 19:05 ET with no change to the timer. It runs on GitHub-hosted runners.
- **The steps:** each minute, from the morning view's publication (about 07:20 ET) until the last game's start less 10 minutes.
  - It reads each market's top of book from Kalshi's public REST.
  - It reads the fair value and the Daily Faceoff polls from what R2 holds by then, and only after their publication (hard rule 1).
- **The windows:** each run stops a minute before the next window and cancels its resting bids at its end. The next run posts again at its first step.

**Safety:**
- **Demo only:** the job refuses to run unless `KALSHI_TRADING` is `demo`.
- **Kept apart from the practice orders:** its orders carry a `q-` client_order_id prefix and their own record, which the practice orders' reconcile ignores. `kalshi-cancel.yml` still cancels everything.
- **Refusals:** a refused call is recorded by its status. A refused cancel is retried at the next step, and two in a row send a Telegram alert.

**The record:**
- **The responses:** every one is kept raw (`live/kalshi_trade.py`), and each day's quotes and fills are kept privately under `real/demo/quotes/<date>/`.
- **The nightly summary:** private, and counts only in the logs (#280). It covers, per day:
  - the quotes posted, amended, pulled and refused;
  - the fills and their contracts;
  - the time from a fair value's update to the re-quote;
  - any bid still resting at a start, which should be none;
  - the simulator's quotes and fills for the same day and variant beside them.
- **No verdict:** it is a mechanics check.

**What it doesn't change:** the paper simulator, the decision, the policy and the practice orders. Real two-sided quotes need their own ADR, reading date and the owner's decision (#358 step 4).

## Backtest evidence

None, by design: the job tests mechanics on mock funds. Its evidence is a demo week reviewed with the owner. The simulator's kept days (2026-10-31 and 11-01) show that these rules post and fill on the real logs.

## Consequences

- **The code:**
  - `live/demo_quotes.py`: the step, the orders' record and the summary;
  - `nhl real quote --demo`;
  - the job in `kalshi-watch.yml`, with the demo secrets;
  - the summary in the nightly;
  - tests with a synthetic exchange, since the repository holds no Kalshi response (#280).
- **The leakage tests:** the fair value and the polls are read only after their publication.
- **Requests:** about two markets' tops a game each minute, inside Kalshi's limits.
- **Moving to the Kalshi runner:** once #347 is up, the job moves there like the other trading jobs.

## Revisit when

- #347's runner is up.
- The owner picks another fair value or half-spread, as a new version.
- #358's ADR (step 4) proposes real quotes.
