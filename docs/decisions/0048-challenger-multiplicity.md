# 0048. Challengers at the 2027-04-12 review: Holm's adjustment over every registered candidate

- Status: Accepted (by the owner, 2026-10-29, #322): option 1, Holm's adjustment at the review
- Date: 2026-10-28
- Amends: ADR 0038 rule 4 (multiplicity)
- Part of: #322, from the external review of 2026-10-23 (#317)

## Context

**Today (ADR 0038 rule 4):** every registered challenger is reported. At the formal review on 2027-04-12, one becomes a candidate if its pre-stated primary measure's 95% interval excludes zero.

**With several challengers, one can pass by chance.** Four are registered as candidates (#256 to #259, ADRs 0041 to 0044), and #260 may follow. At 5% each, four independent tests give about a 19% chance that at least one passes with no edge.
- **The comparisons overlap:** two primary measures are against `conservative_blend` (ADRs 0042, 0043), so the tests aren't independent.
- **ADR 0038 anticipated this:** its "Revisit when" asks for a stricter rule as the count grows.

**The rule has to be fixed now,** before any challenger has 4 weeks of evidence: the three blends are still unfitted, and `b3_split` has 2 weeks.

## Options

1. **Holm's adjustment at the review,** over every candidate registered by 2027-04-12. It holds the chance of any false pass to 5% under any dependence between the tests. It decides at the review, with no further delay, but it is stricter for each challenger as their number grows.
2. **A confirmation period:** a candidate passing at the review must pass again on a further fixed stretch of future games before it enters a policy. That gives the strongest evidence, but the season ends in April, so a change waits for 2027-28.
3. **Both.** The safest, and the slowest.

## Decision: option 1 (the owner, 2026-10-29)

1. **The family:** every challenger registered by 2027-04-12, except `market_only`, which is the mechanism's control, not a candidate (ADR 0038). Its size m is fixed at the review.
   - A challenger with under 4 weeks of evidence, or without its primary measure for want of a fit, still counts in m, with p = 1. It can't pass, and it can't be dropped to ease the others.
2. **Each test:** the challenger's own primary measure from its ADR, as a two-sided p-value from the same weekly block bootstrap that gives its interval: twice the smaller of the shares of resamples at or below 0, and at or above 0.
3. **Holm's step-down:** sort the p-values from smallest to largest. The k-th passes if it, and every one before it, is at most 0.05 / (m − k + 1). A passing challenger is a candidate only if its difference favours it (log loss below the comparison's).
4. **Unchanged from ADR 0038:**
   - every challenger is reported, with its raw interval beside its adjusted result;
   - a candidate enters a policy only through a new policy version, its ADR, its freeze and the owner's decision;
   - the frozen policy never changes because of a challenger.

## Backtest evidence

None, by design: this is the review's rule, fixed before any challenger's evidence exists.

## Consequences

- **The review's challenger section** gives each challenger's p-value, its Holm threshold and the result. Interim weekly reports stay as they are: raw intervals and no verdict.
- **A real edge needs a larger effect to pass** as the family grows, so registering a challenger now has a cost to the others, which is the point.
- **The code:** `metrics` gets the bootstrap p-value and the Holm step-down. The report reads them only at the review.

## Revisit when

- More than about eight challengers are registered by the review.
- A challenger's primary measure has no bootstrap distribution to take a p-value from.
- The owner wants a confirmation period as well (option 3).
