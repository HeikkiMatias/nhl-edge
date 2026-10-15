# 0038. Challengers: pre-registered, judged on future games only

- Status: Accepted (by the owner, 2026-10-15, approving #255's plan)
- Date: 2026-10-15
- Part of: the review agenda (#250); the runner is #255

## Context

**Every history season that could test a new model has been seen:**
- the development seasons;
- the hockey validation;
- 2025-26's and 2022-23's one runs.

Choosing among new models on them again would fit the evaluation itself (Cawley and Talbot 2010). The review's model items (#256 to #260) need games no one has seen, and this season's games are accruing now.

**Each decision day's run bundle (#171) holds every row the decision read at its input cutoff,** and replays the day exactly. A candidate model, a challenger, can therefore be scored after the decision on exactly the decision's information. It never touches the frozen policy's code path, its timing or its evidence.

**What goes wrong without rules:**
- A challenger tuned after its first results, or scored on days chosen afterwards, fits the evaluation again.
- With several challengers, the best one's interval excludes zero by luck more often than one challenger's would.

## Options

1. **Score challengers on history again.** It is quick, but every season is spent, so a pass would mean little.
2. **Pre-registered challengers on future decision days, from the bundles.** Evidence comes slowly, a season at most, but it is evidence no fit has seen.
3. **Put challengers in the live decision run.** This gives the same evidence as option 2, but it touches the frozen policy's code path and the 12:45 ET timing.

## Decision

Option 2, under these rules.

1. **Registration:**
   - A challenger is an entry in `live/challengers.py`: its name, version, ADR, registration date and the lake tables it reads.
   - Before its first scored game, its ADR fixes its specification, its inputs, any fitting rule and its primary measure.
   - A changed challenger is a new version with a new registration, and its evidence restarts.
2. **Evidence:**
   - Only decision days after the registration date count, and nothing is ever backfilled.
   - A day's output is computed from the day's run bundle and the committed live fit. Any lake rows beyond the bundle must be known before the bundle's input cutoff, and the output records which were read.
   - It is written once per challenger, version and day, and it replays from its bundle.
   - Every such day counts: a decision day without its output withholds the challenger's figures until it is scored, so a challenger that fails on some days never chooses its own sample. The latest day, scored the night after its decision, is pending rather than missing.
3. **Measures:**
   - paired log loss against the frozen blend and against B1, on the same games;
   - the shadow bets the frozen selection, guard and staking (ADRs 0028 and 0029) would make on its probability, valued at Pinnacle's closing proxy (CLV), as #252 does for the market-only control;
   - its disagreement's slope on the market's move to the close (#251).

   Each figure has a weekly block bootstrap interval, with counts only under 4 weeks. There is no verdict before the formal review on 2027-04-12 (ADR 0032).
4. **Multiplicity:**
   - Every registered challenger is reported, failures included, and the report states how many are registered.
   - At the review, a challenger becomes a candidate only if its pre-stated primary measure's interval excludes zero.
   - A candidate enters a policy only through a new policy version, its ADR, its freeze and the owner's decision.
5. **No change to the frozen policy:** the frozen policy is never changed by a challenger's results, and no challenger figure is part of ADR 0032's verdicts.

The first registered challenger is `market_only`: the market-only control's probability, `p_blend_market`, as the decision computed it. It is a check of the mechanism, not a candidate. Its output must equal the ledger's value exactly.

## Backtest evidence

None, by design: this decision is about where a challenger's evidence comes from, and history is ruled out as that source. The first evidence is `market_only`'s output on the first decision day after its registration, checked against that day's ledger.

## Consequences

- **The code:**
  - `live/challengers.py`;
  - the `challenger_predictions` table;
  - `nhl live challengers` in the nightly job;
  - a "Challengers" section in the live report;
  - `shadow.selected` in place of the market-only shadow's own selection.
- **Evidence lags:** challengers are scored the night after each decision, so the nightly report shows them a night late.
- **The lake is mutable** (the nightly rewrites a date's rows once its games are final). So a challenger takes the slate's own inputs from the bundle, and from the lake only rows of earlier games. A lake read that changes later shows up when the day is checked.
- **The cost:** a challenger with real evidence needs most of a season. Waiting is the price of evidence no fit has seen.

## Revisit when

- A challenger needs an input the bundle lacks and the lake rewrites, which would mean saving more in each bundle.
- More than about five challengers are registered at once, which would call for a stricter rule at the review than its own interval.
