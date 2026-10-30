# 0047. Growing the real bankroll: 12 weeks of fills, read once

- Status: Accepted (by the owner, 2026-10-29, #322): option 1, one fixed reading date. The owner decided the sample and the measure's bound on 2026-10-23.
- Date: 2026-10-28
- Amends: ADR 0039, which fixes when real stakes start and stop but not when the real bankroll may grow
- Part of: #322, from the external review of 2026-10-23 (#317)

## Context

**ADR 0039's start rule asks little,** by the owner's choice for a 100 € experiment. v2's CLV interval only needs its upper bound above 0, which says losses aren't established, not that an edge is. Nothing yet says what must hold before the real bankroll grows past its 100 €, or past the code's 200 € ceiling (`placement.CEILING_BANKROLL`).

**The owner's decision (2026-10-23):** growth needs at least 12 weeks of real fills, with the net CLV interval's lower bound above 0.

**What is still open is when the evidence is read.** A fixed-sample interval read every week and acted on at the first pass would pass a policy with no edge far more often than its 5% (repeated looks). So the reading has to be fixed in advance: one date, or a sequential test that spends its error across the looks.

## Options

1. **One fixed reading date.** The rule is read once, at the weekly report of the Monday 12 weeks after the first real fill's week. It is simple to state and check, and it can't be peeked at. A failed reading waits for a new date, set by the owner in advance.
2. **A group-sequential test** fixed in advance (an alpha-spending boundary over weekly looks from week 12). It can decide sooner, but its boundaries are built for normal test statistics. Matching them to a weekly block bootstrap is non-standard and harder to check.
3. **A weekly look at the fixed-sample interval.** Rejected: it inflates false passes, which is what this ADR exists to prevent.

## Decision: option 1 (the owner, 2026-10-29)

1. **The measure:** net CLV per real fill, at the matched cost with fees, against #318's reference observed after the fill (`live/fills.py`, status valued). Its 95% weekly block bootstrap interval must have its **lower bound above 0** (hard rule 7).
2. **The sample:**
   - at least 12 Monday-to-Sunday weeks with real fills;
   - and the coverage floor: at least 90% of the real fills have a reference observed after them.
3. **When it's read:** once, at the weekly report on the Monday 12 weeks after the Monday of the first real fill's week, on the fills and references public at that report's clock. For a first fill in the week of 2026-11-16, that is the report of 2027-02-08. No earlier reading states a verdict, only counts and the interval as context.
4. **No stop has fired** since the start (ADR 0039 item 5).
5. **Each fill type apart:** taker and maker fills are read separately, each needing its own 12 weeks. A policy whose taker fills pass but whose maker fills don't may grow only the taker part's stake base.
6. **What passing allows:** the owner may grow the real bankroll, up to the code's ceiling of 200 € without a code change. Beyond 200 € needs a commit raising `CEILING_BANKROLL` (`docs/security.md`). Growth stays the owner's decision. A pass permits it, never requires it.
7. **A failed reading:** the bankroll stays as it is. A new reading needs the owner's new decision with its own date fixed in advance, never a rolling re-read.

## Backtest evidence

None, by design: the rule is fixed before the evidence it judges. History has no Kalshi fills (#280), and on SBR's opener the policy's CLV was −1.23% [−2.01%, −0.46%] per bet (ADR 0039).

## Consequences

- **The weekly report** gains a "Scale-up (ADR 0047)" section with counts only: the reading date, and each fill type's weeks of fills and coverage.
- **The interval and the verdict stay private,** since they come from Kalshi's fills (#280). They go to the live exchange's private figures (`nhl real fills`, `real/live/scale-up/<date>.json`), and the Monday report task passes them to the owner. Until the reading date the figures give the interval as context with no verdict; on the date, the verdict per fill type. The owner chose this on 2026-10-30, after #322 built it.
- **The reading is read-only:** nothing in the code grows the bankroll on its own. The owner changes it.
- **Waiting is the cost:** a first real fill in mid-November is read in February at the earliest.

## Revisit when

- The reading fails, or a stop rule fires.
- The venue's fees or the order types change (ADR 0045), which changes what a fill's CLV means.
- The real bankroll reaches the code's ceiling.
