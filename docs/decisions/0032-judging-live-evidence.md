# 0032. Judging live evidence: one formal review at the season's end, practical calibration limits, and every denominator shown

- Status: Accepted (by the owner, 2026-10-07)
- Date: 2026-10-07

## Context

Phase 5 paper-trades the frozen policy `policy-20261005-8ec5cf3` on live 2026-27 (ADR 0031). Two things could make its evidence misleading (#172):
- **Repeated looks.** #166 reports weekly. If a result counted as soon as one weekly interval cleared zero, the chance of a false "edge" would be far above 5%, because each week's look is another chance.
- **Intervals that contain the ideal value.** ADR 0031 passed calibration because the intervals contained 0 and 1. 2022-23's slope interval, 0.25 to 1.21, contains both sound calibration and badly over-confident forecasts. Containing the ideal value is absence of evidence, not evidence of adequacy.

This ADR fixes how live evidence is judged before any live result exists. It changes neither the frozen policy nor ADR 0031's historical verdicts.

**What a season can show.** Per-bet CLV spread by 6% to 9% in the historical ledgers, at SBR's opener. The 12:45 price is nearer the close, so live spread should be smaller. The first logged slate falls around 2026-10-10. From then to the season's end on 2027-04-10, the policy should place about 450 to 580 bets, at history's bet rates of 36% to 47%. That gives a 95% interval about ±0.6 to ±0.9 points wide. A true CLV under about 1% may well end the season as "insufficient evidence".

## Options

1. **Weekly descriptive reports, and one formal review on a fixed date.** Simple and honest. Its cost is that no verdict comes before the date.
2. **A group-sequential design:** a few planned interim reviews, with the error rate split across them (an alpha-spending rule such as O'Brien–Fleming). It allows an early verdict, at the cost of stricter early thresholds and more machinery.
3. **Anytime-valid confidence sequences:** verdicts at any time with a controlled error rate (Howard et al.). They are wider at every point, and their assumptions about independent bets fit games that cluster in weeks less well.

## Decision

Option 1, if the owner accepts it.

- **The primary measure:** mean CLV per paper bet against Pinnacle's fair closing proxy, de-vigged multiplicatively through `market/devig.py`.
  - **Bets counted:** every paper bet of the frozen policy, from the first logged slate to the end of the regular season.
  - **Bets without a valid proxy** are excluded, and counted by reason.
  - **Reported beside it, never instead of it:** stake-weighted CLV and the fair move. Return at the taken price is reported once, at the season's end (plan §11).
- **One formal review, on 2027-04-12,** after the last regular-season result is public. Its verdict:
  - **favourable prices,** if the 95% weekly block bootstrap interval of the primary measure is above zero;
  - **unfavourable,** if it is below zero;
  - **insufficient evidence,** otherwise.

  The weekly reports before then are labelled interim. No verdict, promotion or real stake follows from them.
- **The model comparisons,** judged at the same review:
  - **Shown:** BLEND minus B1 on live E2 (adds information) and B3 minus B2 (the player layer). BLEND minus BLEND_B2 and BLEND minus BLEND_MARKET are reported beside them.
  - **How they are computed:** paired on the same games, with weekly block bootstrap intervals, and every exclusion counted.
  - **No multiplicity correction.** Each answers its own §1 question, and only the primary measure can open the way to a real stake.
- **Calibration limits** for the blend, with practical meaning:
  - **The limits:**
    - the slope within 0.80 to 1.25;
    - the intercept within ±0.10 on the log-odds scale.
  - **Why these:** at a 60% forecast, a slope of 0.8 moves the truth to about 58%, and an intercept of 0.1 moves a 50% forecast to about 52.5%. Both are the size of the 2.5% expected-return hurdle.
  - **The verdict:**
    - it passes only when the whole 95% interval lies inside the limits;
    - it fails when the whole interval lies outside them;
    - otherwise it is "insufficient evidence".
- **Coverage is part of the evidence.** Every report shows:
  - the slate games, those predicted, the bets, and the bets with a valid closing proxy;
  - the exclusions by reason;
  - each quote's freshness, measured apart from its lead time before the start.
- **Operational alerts are kept apart from success claims.** They never change the policy:
  - missing feeds, stale prices or skipped days;
  - the 20% drawdown review;
  - u outside its training range;
  - the guard's firing rate.
- **A new policy version** starts its own evidence record from its own freeze date. Earlier versions' records are kept, and games seen under one version never confirm another.

## Backtest evidence

None for live play. The precision estimate above uses the spread of CLV in the historical ledgers (`reports/backtest/bets.csv`, and 2022-23's run). It is a planning figure, not a result.

## Consequences

- #166's report implements these rules and labels. Synthetic fixtures cover:
  - too few weeks;
  - wide calibration intervals;
  - missing closing quotes;
  - different coverage across models;
  - interim dates.
- The closing proxy's freshness threshold is fixed before the first counted prediction (plan, task 4).
- ADR 0031's verdicts stand, and so does gate 3's interval-overlap wording for history.

## Revisit when

- The owner wants a verdict before the season's end. A group-sequential design must then be set before the first interim look, never after.
- Or fewer than about 300 bets look likely by mid-season.
