# 0061. Market making on Kalshi as the primary track, the Pinnacle policy in the background

- Status: Proposed
- Date: 2026-11-20
- Part of: #438, and #358's step 4 widened. From the owner on 2026-11-20: "change the kalshi model to the primary model and just keep silently following pinnacle in the background. I think kalshi is where the value is."
- Would supersede, once accepted: ADR 0039's start rule and its weekly CLV stop, for the frozen policy. Its real bankroll of 100 € and its 20% stop carry over here.

## Context

**The frozen policy can't start real stakes.**
- Its CLV against Pinnacle's fair closing proxy is −0.0295 [−0.0386, −0.0228] per bet, at the weekly report of 2026-11-16 (51 bets, 5 weeks).
- So ADR 0039's condition (a) fails, and the policy takes prices that the close says are wrong.

**Market making is the owner's chosen alternative** (#358, the owner's decision of 2026-10-30).
- **The idea:** post-only bids on both teams' Yes markets at our model's fair value less a half-spread. A maker on Kalshi pays no taker's cost (#358).
- **What already runs:**
  - the paper simulator on the watch's logs (`live/kalshi_quotes.py`, from 2026-10-31);
  - the demo quoting job (ADR 0055);
  - the hold rules' record (#385) and the requote record (ADR 0059).
- **Its main risk is adverse selection:** a quote fills mostly where it is wrong.
- **Fills:** the simulator's fill rule is an upper bound, on the adverse fills too.
- **Privacy:** every Kalshi figure is private (#280).

**The owner's choices of 2026-11-20 (#438):**
- market making at the model's fair value is the primary track;
- it stays on paper until its own start rule holds;
- the Pinnacle policy runs on in the background.

## Options

1. **Market making primary, paper first, with a market-relative start rule** (the owner's choice). The start waits on evidence that the fills beat the close, which may take weeks or never come.
2. **Real quoting as soon as the venue is ready.** It starts sooner, but real money would go on a measure no interval supports yet. The owner declined it.
3. **Keep the Pinnacle policy primary.** It has its 5 weeks of evidence, and the evidence is against it.

## Decision: option 1 (proposed)

**The traded rule:** the simulator's model variant at a 1 cent half-spread (ADR 0055's, fixed before any figure).
- **Fair value:** the morning view's blend from its publication, then the decision's.
- **Quotes:** the simulator's pulls (a named starter changing; a 3 cent move), and its stop 10 minutes before the start.
- **The hold rule after a move:** the owner's pick from #385, due about 2026-11-24. Until then it is `wait`.
- **Requote:** per ADR 0059's reading.

**The record each rule reads:** the private record of that exact rule. That is `kalshi_quotes` version 1's model 1 cent variant under `wait`, or #385's record of the picked rule. Each was fixed before its first scored day, so it counts from that day.

**The primary measure (pre-registered):** the close value per contract filled.
- **Definition:** Pinnacle's fair closing proxy observed after the fill, less the price, net of Kalshi's maker fee. This is the simulator's `markout_close`, net.
- **Interval:** a weekly block bootstrap interval, with counts only under 4 weeks (hard rule 7).
- **Shown beside, never a condition:**
  - the markout to Kalshi's last mid before the start;
  - the markouts 5 and 30 minutes after the fill;
  - the profit at settlement, which is mostly the games' results.

**The start rule,** read privately at each Monday's weekly reading. All of these must hold:
- **(a) The measure:** its interval's lower bound is above 0, over at least 4 Monday-to-Sunday weeks of fills, with at least 90% of the fills having a closing proxy after them.
- **(b) The mechanics:**
  - the Kalshi runner is up (#347);
  - the live check from it has passed;
  - a demo week of two-sided quoting from it has finished, with every run complete and every bid cancelled at its run's end.
- **(c) The venue:** the venue commit and the owner's deposit (ADR 0039's (d)).

The rule holding permits a start, and the owner confirms the first live day.

**Real limits,** on a real bankroll of 100 € (ADR 0039):
- 2 contracts a bid;
- a net position of 6 contracts a game;
- 15 € of a day's filled and resting cost.

The paper simulator keeps its own limits (10, 30 and $100).

**The stops,** each until the owner restarts:
- the real bankroll falls 20% (ADR 0039's);
- a weekly reading puts the primary measure's interval entirely below 0, on the paper record before the start or on the real fills after it.

ADR 0047's scale-up reading applies to the real maker fills.

**The Pinnacle policy, demoted:**
- **Unchanged:** its decisions, paper ledger, settlement, CLV and challengers, and the formal review on 2027-04-12 (ADR 0032).
- **The weekly report:** its CLV becomes a background section, and its real-stakes section is replaced by the market-making track's counts.
- **Stopped:**
  - the midday Telegram bet alerts (`nhl real alert`);
  - ADR 0040's practice orders of its bets (`nhl real place`), so that the demo account carries only the quotes;
  - ADR 0039's start rule, its window and its weekly CLV stop.

## Backtest evidence

None public. The simulator's readings are private (#280). They went to the owner in the session on 2026-11-20. The evidence for the start rule is the weekly private reading of the record named above.

## Consequences

**Real money waits on evidence the policy never had.** That evidence is the fills beating the close, net of fees. Because the measure is market-relative, the games' results can't pass the rule by luck.

**Some figures stay private.** The committed weekly report can carry the track's counts only. The figures go to the owner privately, in the session and on the dashboard's owner-only tables (#398).

**What changes**, a PR each:
- **The live quoting job:** `nhl real quote` gains the live environment with the real limits, on the Kalshi runner.
- **The real ledger:** its mode reads the new start rule, with `real.VENUE`.
- **The weekly report:** its sections change, and so does the routine's prompt.
- **The bet alerts and practice orders:** they are switched off.
- **Docs:** `docs/handover.md` and `docs/model-card.md`.

**Unchanged:** no hard rule changes. Hard rule 4 holds, since a bid's expected return at its own price is the half-spread.

## Revisit when

- **A stop fires:** a weekly reading puts the primary measure's interval entirely below 0.
- **The Pinnacle policy improves:** its CLV interval at a weekly report lies above 0, so the background track deserves a look again.
- **Real fills differ from the paper ones:** after 4 weeks of real fills, their rate or markouts differ clearly from the simulator's, so the paper record no longer stands for the real one.
