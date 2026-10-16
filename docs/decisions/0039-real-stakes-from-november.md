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
- **Live:** 24 paper bets so far, with counts only until 4 weeks of a policy's games. v2's CLV interval needs its eligible bets with a proxy to span 4 Monday-to-Sunday weeks. The 4th starts on 2026-11-02, so the first interval can appear at the weekly report of 2026-11-09 at the earliest.

**Profit over a few weeks is mostly luck:**
- At odds near 2.0, one bet's return has a standard deviation of about one stake. Over the 100 to 120 bets expected by mid-November, a policy that truly loses 2% per bet still ends up in profit about 4 times in 10.
- CLV varies far less per bet, so the same bets give a much narrower interval. That is why ADR 0032 made it the primary measure.

## Options

1. **Keep ADR 0032: no real stake before the formal review.** The safest option, but no real money until April 2027.
2. **Start on a date if the paper bankroll is up.** This is the owner's condition as stated. It is a point estimate: a losing policy passes about 4 times in 10, and hard rule 7 allows no pass on a point estimate alone.
3. **Start on a date if the CLV interval isn't entirely negative and the bankroll is up,** with a capped real bankroll and a stop rule. The CLV interval is the test, the measure that can tell a losing policy apart. The owner's bankroll condition stays as a veto beside it, and the cap limits what can be lost.

## Decision: option 3 (the owner, 2026-10-15)

1. **The start:** the first decision day from **2026-11-16 to 2026-11-30** on which the start rule held at the latest weekly report. A start later in November is as good (the owner), for example while the venue is made ready. If the rule fails, it is checked again each week. If it hasn't held by November's last decision day, no real stake starts, and a later start needs a new decision by the owner (Codex on #272).
2. **The start rule,** fixed now, before the evidence:
   - (a) v2's CLV per bet, under ADR 0032's safeguards: its interval over at least 4 weeks has its upper bound above 0, the coverage floor holds (at least 90% of the eligible bets have a closing proxy), and the pessimistic bound has its upper bound above 0 too (each eligible bet without a proxy counted at the 10th percentile of the observed CLV) (Codex on #272);
   - (b) the season's paper bankroll is above its 100 units. This is the owner's own veto, not evidence: a single number, so it can only block a start, never start one on its own, and condition (a) is the actual test. Hard rule 7 allows no pass or fail on a point estimate alone; keeping it beside (a) is the owner's decision (2026-10-16, Codex's P0 on #272 answered won't-fix);
   - (c) the operations are clean: every decision day's run bundle is complete, no day was skipped without a reason, and the 20% drawdown review has not been triggered.
   - (d) the venue is ready (#266): its terms are confirmed for a Finnish resident, and either its API access is granted or manual placement from alerts works.
3. **What is bet:** exactly the frozen policy's paper bets, unchanged (v2, ADRs 0028, 0029 and 0037):
   - each bet is placed only at an executable price where its EV still clears the hurdle (`p × odds − 1`, hard rule 4);
   - its fraction is the frozen staking rule (`betting/staking.fractions`) computed again at that executable price, with the day's cap applied again. It is never more than the paper fraction, so a worse price stakes less, and a better one no more (Codex on #272);
   - never once the game has started, and never twice.
4. **The real bankroll:** **100 €** (the owner), an amount the owner can afford to lose entirely. It is not topped up during the season. A stake is its fraction (item 3) of the real bankroll at the decision: at most 1.5% of it, so 1.50 € at the start, rising and falling with the bankroll (Codex on #272). It is rounded down to the venue's stake increment. A stake below the venue's minimum isn't placed, and is logged as skipped beside its paper twin; rounding it up would break the 1.5% cap.
5. **Stop rules:** real bets stop, and the paper ledger goes on, if either happens:
   - the real bankroll falls 20% from its start. This is checked before every day's alerts or placement, from the real ledger's settled balance, not only weekly (Codex on #272);
   - at a weekly report, v2's CLV evidence turns adverse under the same safeguards as the start (Codex on #272): the CLV per bet interval falls entirely below 0, or the pessimistic bound's interval falls entirely below 0, or the coverage floor fails (fewer than 90% of the eligible bets with a closing proxy), which pauses real bets until the floor holds again.

   Restarting needs the owner.
6. **Venue and placement:** chosen in #266.
   - **Manual by default:** until an API venue's terms and access are confirmed, each decision posts an alert (game, side, minimum price, stake) and the owner places the bet by hand.
   - **Automated placement** gets its own review before its first real bet.
7. **Records:**
   - **A real ledger** sits beside the paper one, with one row per real bet, written once. Each row holds the requested and matched price, the stake actually placed, the time and the venue's bet id (Codex on #272).
   - **Real settlements** are appended, never rewritten, as one row per settled real bet: the result on the full game (hard rule 2), the settlement time, the profit at the matched price and stake, and the bankroll after it. The daily 20% stop reads that settled bankroll, and the weekly reconciliation compares it with the venue's statement.
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

- The start rule hasn't held by November's last decision day.
- A stop rule fires.
- No venue can be confirmed.
- The formal review on 2027-04-12.
