# 0039. Real stakes from mid to late November 2026, beside the paper ledger

- Status: Accepted (by the owner, 2026-10-15): option 3's start rule, a real bankroll of 100 €, and a start on or after 2026-11-16, later in November being as good
- Date: 2026-10-15
- Amends: ADR 0032, where "no verdict, promotion or real stake" follows from an interim report. The verdicts and the formal review on 2027-04-12 are unchanged.

## Context

**The owner's decision (2026-10-15):** bet real money from mid to late November 2026, even with little data, as long as the paper bankroll develops profitably. The paper ledger continues.

**What the evidence is:**
- **On history:** the policy's bets anticipated the market, but they lost to the close at the price taken (`reports/backtest/accepted.json`, 2021-22 and 2022-23, SBR's opener):
  - fair move +2.83% [+2.04%, +3.62%] per bet;
  - CLV −1.23% [−2.01%, −0.46%] per bet.
- **Gate 3 is not met** (ADR 0031).
- **Live:** 24 paper bets so far, with counts only until 4 weeks of a policy's games. v2 reaches 4 weeks of games on 2026-11-02, when its first CLV interval appears.

**Profit over a few weeks is mostly luck:**
- At odds near 2.0, one bet's return has a standard deviation of about one stake. Over the 100 to 120 bets expected by mid-November, a policy that truly loses 2% per bet still ends up in profit about 4 times in 10.
- CLV varies far less per bet, so the same bets give a much narrower interval. That is why ADR 0032 made it the primary measure.

## Options

1. **Keep ADR 0032: no real stake before the formal review.** The safest option, but no real money until April 2027.
2. **Start on a date if the paper bankroll is up.** This is the owner's condition as stated. It is a point estimate: a losing policy passes about 4 times in 10, and hard rule 7 allows no pass on a point estimate alone.
3. **Start on a date if the bankroll is up and the CLV interval isn't entirely negative,** with a capped real bankroll and a stop rule. It keeps the owner's condition, adds the measure that can tell a losing policy apart, and limits what can be lost.

## Decision: option 3 (the owner, 2026-10-15)

1. **The start:** the first decision day on or after **2026-11-16** on which the start rule held at the latest weekly report. A start later in November is as good (the owner), for example while the venue is made ready. If the rule fails, it is checked again each week.
2. **The start rule,** fixed now, before the evidence:
   - (a) v2's CLV per bet has an interval over at least 4 weeks, and its upper bound is above 0;
   - (b) the season's paper bankroll is above its 100 units (the owner's condition);
   - (c) the operations are clean: every decision day's run bundle is complete, no day was skipped without a reason, and the 20% drawdown review has not been triggered.
   - (d) the venue is ready (#266): its terms are confirmed for a Finnish resident, and either its API access is granted or manual placement from alerts works.
3. **What is bet:** exactly the frozen policy's paper bets, unchanged (v2, ADRs 0028, 0029 and 0037):
   - each bet's stake is its paper fraction (at most 1.5%) of the real bankroll;
   - a bet is placed only at a price where its EV still clears the hurdle (`p × odds − 1`, hard rule 4);
   - never once the game has started, and never twice.
4. **The real bankroll:** **100 €** (the owner), an amount the owner can afford to lose entirely. It is not topped up during the season. A stake is the paper fraction of the real bankroll at the decision, so at most 1.50 €. It is rounded down to the venue's stake increment. A stake below the venue's minimum isn't placed, and is logged as skipped beside its paper twin; rounding it up would break the 1.5% cap.
5. **Stop rules:** real bets stop, and the paper ledger goes on, if either happens:
   - the real bankroll falls 20% from its start;
   - v2's CLV per bet interval falls entirely below 0.

   Restarting needs the owner.
6. **Venue and placement:** chosen in #266.
   - **Manual by default:** until an API venue's terms and access are confirmed, each decision posts an alert (game, side, minimum price, stake) and the owner places the bet by hand.
   - **Automated placement** gets its own review before its first real bet.
7. **Records:**
   - **A real ledger** sits beside the paper one, with one row per real bet, written once. Each row holds the requested and matched price, the time and the venue's bet id.
   - **The paper ledger** and its evidence are unchanged, and the formal review judges the paper record (ADR 0032).
   - **The frozen policy never changes** because of either record's results.

## Backtest evidence

As in Context: CLV −1.23% [−2.01%, −0.46%] per bet on history, at SBR's opener. Nothing on history supports a real stake. The start rests on the live record and on the owner's decision.

## Consequences

- **Real money can be lost.** On history the policy didn't beat the close. The cap and the stop rules bound the loss, but don't prevent it.
- **Work this needs:**
  - the venue (#266);
  - a real ledger table;
  - the alert, or the placement client;
  - reconciliation against the venue's statements;
  - the weekly report checking the start and stop rules.
- **The dashboard** will need to show the real record apart from the paper one.

## Revisit when

- The start rule fails at three weekly checks in a row.
- A stop rule fires.
- No venue can be confirmed.
- The formal review on 2027-04-12.
