# 0054. The close forecaster: B3's split at 07:05 ET forecasts Pinnacle's close, as a fair value for early quotes

- Status: Accepted (by the owner, 2026-11-02, #369): option 2
- Date: 2026-11-02
- Part of: #369, step 3 of #361. From the owner on 2026-11-02: take every edge we can find, and aim with all of them to beat the market.

## Context

**What #361 found** (ADR 0053):
- **B3's split anticipates the move.** On the confirm half, it forecast part of SBR's move from the opener to the close: −1.294 [−1.752, −0.902] thousandths of squared log-odds, and −1.429 [−1.828, −1.089] on the rested games.
- **Nothing adds to the close.** So the model can't beat the closing price, but it can partly forecast it.

**The history of a forecaster** (`scripts/close_forecast_history.py`, `reports/yardstick/close-forecast-history-20261102-21a7cba.md`):
- **Its moves are small.** The forecast moves average 0.66 probability points. Only 21% of games get a forecast of 1 point or more, and 3% get 2 points or more.
- **It needs the split's own weights.** Moving the opener k of the way toward B3's whole difference, which is what the morning view stores, keeps about half the skill: +0.451 [+0.276, +0.629] thousandths worse than the split on the same games. The market moves toward the parts weighted as the split weighs them, not as B3's outcome model does.
- **Taking at the opener doesn't beat the close.**
  - Sides taken where the forecast close gives a positive expected return at SBR's opener price have a CLV of +0.50% [−0.72%, +1.85%].
  - That mean comes from a few games whose price moved more than 8 points, which no 10:00 feature foresees. Without them the CLV is −1.68% [−2.25%, −1.05%], against a median margin of 3.8%.
  - So a predicted move averaging 0.66 points can't pay a margin. It can pay for quoting, where we earn the spread instead of paying it (#358).

**What exists:**
- **The morning view** (ADR 0052): B3 and the blend at 07:05 ET with Pinnacle's pair, and a bundle that replays them.
- **`close_model`** (ADR 0049): it forecasts the move from the 12:45 ET decision, too late for early quotes, and it has no fit until about 200 games.
- **#358's quoting simulator:** its fair values are the blend, the blend anchored to Kalshi's mid, and Pinnacle. None forecasts where the market goes.

**What doesn't transfer:** the weights are fitted on SBR's opener (2012-13 to 2017-18, assumed at 10:00 ET). Pinnacle's 07:05 pair is a sharper price than a consensus opener of the 2010s, so its moves may be smaller. The opener's own terms (a, b_o) are SBR's.

## Options

1. **The six weights frozen from history,** applied to Pinnacle's 07:05 pair. Nothing is fitted live. But the size is SBR's. If Pinnacle moves less, it over-forecasts, and can score worse than no move even when its direction is right.
2. **History's direction, the live size:** ŷ = λ·ŷ_hist, with λ refitted weekly on earlier mornings under a prior centered on 1. It starts as option 1 and learns the one number least likely to transfer. The cost is a weekly fit, like `close_model`'s.
3. **All six weights refitted live,** with history's as priors. It adapts most, but six weights on a few hundred games are noisy, with six prior widths to choose.
4. **One weight toward the morning view's stored B3,** fitted live. It needs no new inputs, but on history it keeps about half the skill (above).

## Decision: option 2 (the owner, 2026-11-02), as `close_forecast` version 1, registered on the day its code merges

**The time and inputs:** the morning run's bundle (`bundles/morning/<date>/`, ADR 0052), at its input cutoff, the morning snapshot.
- **o:** logit of Pinnacle's morning pair, de-vigged multiplicatively (the morning view's p_b0, hard rule 3).
- **x:** B3's four parts (`b3.split_inputs`, ADR 0044), from the bundle's B3 rows as the morning view's B3 sees them. The goalie part is averaged over the goalie scenarios, with v2's starters where they were called.
  - History averaged the scenarios without starters (ADR 0053). A starter called by 07:05 is rare (ADR 0052's dry run).

**The forecast:** ŷ = λ·(a + b_o·o + b·x), and p̂_close = sigmoid(o + ŷ).
- **The frozen weights (a, b_o, b):** the report's fit on 6,896 games from 2012-13 to 2017-18, with train_cutoff 2018-04-09.
  - They are committed as `reports/live/close-forecast-<yyyymmdd>-<shortsha>.json` and pinned by sha256, as `b3_split`'s fit is.
  - a +0.0219; b_o −0.1287; 5v5 +0.3074; special teams +0.1496; finishing +0.0813; goalie +0.0781.
- **λ, the size:**
  - It is refitted weekly, at the posterior mode of y = λ·ŷ_hist + ε under λ ~ N(1, 0.5²), with the residual variance estimated, as `close_model` fits.
  - Its rows are the mornings of earlier ET weeks (Monday to Sunday) with a closing proxy after their snapshot. Both were public before the week's first morning snapshot (hard rule 1).
  - With no such rows, λ is 1.

**When it runs:** in the morning job, right after the morning view, from the bundle just written.
- Each date is written once, to `forecast/live/<date>.parquet`, with its publication time. Every prediction is published before its game starts.
- The lake's `close_forecasts` table is rebuilt from those files, and a nightly check replays each stored day from its bundle.

**The primary measure,** per game: (y − ŷ)² − y², the forecast's squared error less that of no move, as ADR 0049 measures.
- **The move:** y = logit p_close − o, where p_close is Pinnacle's closing proxy after the morning snapshot (`live/movement.py`), de-vigged the same way.
- **The reading:** a weekly block bootstrap interval, where negative favours the forecaster.
- **Multiplicity:** a family of its own, one test at 5%, as the morning view's (ADR 0052). It is a fair value for quotes, and no candidate for the decision's policy.

**Reported beside it:**
- **The size:** the slope of y on ŷ, where 1 means the right size, and λ by week.
- **The same measure on the rested games.**
- **On results:** p̂_close's log loss on the full-game result (hard rule 2), against the morning pair and against the decision's blend.
- **Shadow takes at 07:05,** so every edge stays measured.
  - **The rule:** a side where p̂_close · its price − 1 > 0, at the best price of the morning snapshot (hard rule 4), one unit each.
  - **The best price** is Pinnacle's or the best fresh bookmaker's there. The exchanges are left out, as their prices are before commission. This was set in the build, before registration.
  - **The value:** at the closing proxy (CLV) and at settlement.

**In #358's simulator:**
- **A fourth fair value, `forecast`:** p̂_close, from its publication to the start, at the same half-spreads.
- **When it counts:** its rules are fixed before its first day, the first morning after registration.
- **Privacy:** its readings stay private, since they come from Kalshi's books.

**No real bet or order.** A policy that bets or quotes at 07:05 needs its own ADR, freeze and evidence (ADR 0052), and real quotes are #358 step 4. There are counts only under 4 weeks. The primary test is read at the review on 2027-04-12. The simulator's readings are design evidence for #358 at any time.

## Backtest evidence

From `reports/yardstick/close-forecast-history-20261102-21a7cba.md`, on SBR's training seasons. The features' settings were tuned on them (ADR 0011), so this is development evidence.

| Reading | Games | Figure |
| --- | --- | --- |
| Frozen fit, in sample, against no move | 6,896 | −1.029 [−1.363, −0.732] thousandths |
| Out of sample (2013-14 to 2017-18), against no move | 6,176 | −0.510 [−0.825, −0.207] |
| The same, on the rested games | | −0.633 [−0.937, −0.328] |
| The same, against the opener's drift | | −0.965 [−1.256, −0.701] |
| k toward B3's whole difference, less the split (2014-15 to 2017-18) | 4,949 | +0.451 [+0.276, +0.629] |
| Taking at the opener, expected return above 0: CLV | 238 sides | +0.50% [−0.72%, +1.85%] |
| The same without moves above 8 points (diagnosis) | 229 sides | −1.68% [−2.25%, −1.05%] |

**The no-move baseline's error** is 14.2 thousandths. The forecast beats no move out of sample, but by less than it beats the opener's drift, since SBR's opener drifts in a way Pinnacle's needn't. That gap is why the size is learned live.

## Consequences

**The code:**
- `live/close_forecast.py`;
- the frozen weights' artifact and its pin;
- the `close_forecasts` table and its schema;
- `nhl live forecast`, as a step after the morning view in `odds-snapshots.yml`, and its check in `ingest-nightly.yml`;
- the fourth fair value in `live/kalshi_quotes.py`;
- the report's section.

**The leakage tests:**
- every input is known by the morning cutoff;
- λ's rows are earlier weeks', public before the week's first morning;
- the weights' train_cutoff is 2018-04-09, and nothing after it is read;
- the forecast never reads its own game's close.

**What doesn't change:** the decision, the morning view and `close_model`.

**Later:** #370's features enter as a new version, with its own registration, once scored on the yardstick.

## Revisit when

- λ settles far from 1, which would say the weights themselves don't transfer (option 3).
- Near-start snapshots are added (#254), which changes what the close is.
- A feature from #370 is kept.
- The review on 2027-04-12.
