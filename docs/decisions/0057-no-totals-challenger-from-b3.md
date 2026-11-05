# 0057. No totals or props challenger from B3's projection

- Status: Accepted (by the owner, 2026-11-05, #359): option 2. "let's leave totals for the future. We'd need a proper model for it": option 3 is future work, #397
- Date: 2026-11-05
- Part of: #359, proposed after the live review of 2026-10-30 and agreed by the owner as a proposal. Its evidence rule was fixed on the issue on 2026-11-05, before any result was read.

## Context

**B3 projects more than one moneyline number.** It projects each team's expected 5v5, power-play and shorthanded goals, with finishing and the opposing goalie. It also projects each skater's minutes and ratings. Game totals and player props are thinner markets than the moneyline: totals were found inefficient (Woodland & Woodland, 2010), and props are softer. Kalshi lists both, and the watch already logs their tops privately (`kalshi_lines`, #319).

**History can say whether a shadow is worth building first.** SBR's totals, the opener and the close at one line, cover the training seasons. So the yardstick's protocol (ADR 0053) can ask whether B3's projected total knows anything about the total that the market doesn't know yet:
- **The move:** the market's implied mean total moving from the opener to the close.
- **The result beyond the close:** the over at the closing line.

**Props have no history here.** SBR has no props, and they rest on the same projection, so they follow the totals' answer.

## Options

1. **Build the totals challenger.** A count model from B3's team rates, scored as a paper shadow against Kalshi's totals, with props later. It costs a daily job, a table, a report section and the owner's attention. Its reading would be months away.
2. **Don't build it, by the pre-registered rule.** #359 closes as not planned. The private `kalshi_lines` logging stays, at no cost, as the owner decided on #319.
3. **Build a totals-specific model instead.** It would use inputs B3 lacks, such as pace, save rates or each team's recent totals. That is a new proposal, not this one, and would be scored on the explore half by the same script first.

## Decision

**Option 2 (the owner, 2026-11-05).** By the rule fixed on #359:
- No measure's interval over all games lies below 0 on the explore half for either candidate (`total`, or its four parts as `total split`), so no claim is written and no confirm run is made.
- The rule then recommends not building. The projection's total adds nothing measurable to the market's on history, much as no feature added to the moneyline's close in ADR 0053's explore run.
- Props go with it.

## Backtest evidence

`scripts/totals_history.py`, `reports/yardstick/totals-history-20261105-0ebf4c5.md`.
- **The sample:** the explore half, 2013-14 and 2014-15, each season fitted on the games from 2012-13 whose results were public before its fold start. That is 2,445 games, 1,717 of them rested.
- **The prices:** SBR's totals, de-vigged multiplicatively. μ is the mean of a Poisson total at the quote's line and price.
- **The figures:** thousandths, with weekly block bootstrap 95% intervals; negative favours the projection.

| Candidate | Move skill, all games | Move skill, rested | Result beyond the close, all games | Result beyond the close, rested |
| --- | --- | --- | --- | --- |
| total | +0.47 [+0.12, +0.85] | +0.64 [+0.19, +1.08] | −0.67 [−2.15, +0.81] | −0.90 [−2.64, +0.95] |
| total split | −0.10 [−0.48, +0.28] | +0.05 [−0.39, +0.48] | −1.08 [−3.81, +1.80] | −2.59 [−5.40, +0.34] |

**Reading the table:**
- **The move:**
  - The projected total makes the forecast of the move worse.
  - The seasons disagree: 2013-14's fit, on the shortened 2012-13 alone, is +1.62 [+1.01, +2.26]; 2014-15's is −0.68 [−1.06, −0.31].
- **The result:** both candidates' estimates lean toward the projection, with every interval spanning 0.
- **Described, not tested:**
  - The projection correlates 0.07 with the final total, against 0.11 for the closing implied mean. Their correlation with each other is 0.50.
  - The projected total averages 4.83 goals, against 5.48 scored. It leaves out empty-net, 3-on-3 overtime and shootout goals, and each fit absorbs the level.
- **Caveats:**
  - B3's settings were tuned on these seasons' moneyline results (ADR 0011), so this is no clean holdout.
  - SBR's opener may be from the evening before (ADR 0006), so the rested games are shown beside all.

## Consequences

- No code, table or job is added beyond the script, its tests and its report.
- #359 closes as not planned.
- **A proper totals model is future work (#397).** It is scored on the explore half by this script's measures before any shadow.
- Whether the watch keeps logging Kalshi's totals and props stays the owner's choice (#319). Nothing reads those tables.
- **Kalshi's settlement rule:** whether Kalshi counts the shootout goal in its totals was never needed and remains unconfirmed.

## Revisit when

- **A totals-specific model is proposed** (option 3, #397). It would be scored on the explore half with this script's measures first.
- **Kalshi's totals logs show a market that lags a sharp book's totals.** Reading that would need a sharp book's totals feed, which costs Odds API credits and is the owner's decision.
