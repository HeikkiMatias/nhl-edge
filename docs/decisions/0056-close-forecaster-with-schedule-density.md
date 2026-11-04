# 0056. `close_forecast` version 2: schedule density beside B3's split, with its own live size

- Status: Proposed
- Date: 2026-11-04
- Part of: #370, whose rule kept schedule density; ADR 0054's "Later: #370's features enter as a new version". From the owner on 2026-11-04: "It should help us pick an early line."

## Context

**What #370 found** (`reports/yardstick/targeted-features-20261104-ee21ec8.md`, explore half, rule fixed before scoring):
- **Density anticipates the move** from SBR's opener to the close. Its five columns (games over the 3 and 6 days before, a rest mismatch, road games in a row) scored −1.343 [−1.696, −1.012] thousandths of squared log-odds, −0.373 [−0.536, −0.211] on the rested games, in both seasons.
- **It adds beyond B3's split** (descriptive, after the decision): −1.556 [−1.899, −1.219], and −0.394 [−0.599, −0.195] on the rested games.
- **It adds nothing beyond the close:** +6.0 [+3.4, +8.7]. It says where the price goes, not who wins.

**Where it comes from:**
- Most of it sits on back-to-back games: −4.30 [−5.30, −3.35] beyond the split.
- There, SBR's opener (assumed public at 10:00 ET, ADR 0006) may have been posted before the previous night's game ended. Pinnacle's 07:05 ET pair comes after last night's results, so that part may not transfer.
- The rested part is the clean part.

**What an early line needs:**
- A price taken early beats the close when the market then moves toward it (CLV).
- `close_forecast/1`'s history: its moves average 0.66 points, and taking SBR's opener didn't pay the margin (−1.68% [−2.25%, −1.05%] without the moves above 8 points; ADR 0054).
- Density adds skill. Whether that is enough to pay a bookmaker's margin is the live question. A quote, which earns the spread (#358), or an exchange price needs less.

**Every history season is spent** (ADR 0038), so the test is on future mornings.

## Options

1. **Density added to version 1 in place.** This is simplest, but v1 was registered on 2026-11-02, so its evidence would restart and nothing would show what density adds.
2. **Version 2 beside v1, with one shared size λ.** This mirrors v1. But if density's size on history comes from stale openers, one λ shrinks the split's part along with it.
3. **Version 2 beside v1, with density on its own size** (recommended).
   - **The form:** ŷ = λ_s·(a + b_o·o + b·x) + λ_d·(b_d·d), each λ refitted weekly.
   - **The cost:** one more number fitted live.
   - **The gain:** density's size can fall to whatever Pinnacle's 07:05 price leaves without dragging the split's.
4. **Density's five weights refitted live.** Five weights on a few hundred mornings are noisy.

## Decision (Proposed): option 3, as `close_forecast` version 2, registered on the day its code merges. Version 1 runs unchanged beside it.

**The inputs:**
- **v1's,** from the morning bundle at its input cutoff (ADR 0054): o, Pinnacle's de-vigged morning pair; x, B3's four parts.
- **d, density's five columns as #370 defines them,** computed from two things:
  - tonight's slate, public a day before (ADR 0005);
  - the season's games whose results were public before the morning snapshot (ADR 0003: last night's are public at 10:00 UTC, before 07:05 ET).

**The frozen weights (a, b_o, b, b_d):** one least-squares fit of y = c − o on SBR's 2012-13 to 2017-18, as v1's.
- train_cutoff 2018-04-09.
- Committed as `reports/live/close-forecast-2-<yyyymmdd>-<shortsha>.json` and pinned by sha256.

**The sizes λ_s and λ_d:**
- the posterior mode under independent N(1, 0.5²) priors, with the residual variance estimated;
- fitted on earlier ET weeks' mornings with a closing proxy after their snapshot, as v1's λ (hard rule 1);
- without rows, both are 1.

**Before registration** (in the build PR):
- **The report:** the frozen fit's out-of-sample history on 2013-14 to 2017-18, beside v1's on the same games.
- **The stop:** if v2 less v1 isn't below 0 on the rested games there, the build stops and returns to the owner.

**The primary measure,** per game of each morning: v2's squared error on the move to Pinnacle's closing proxy, less v1's (ADR 0054's move), where negative favours v2.

**Reported beside it:**
- each version against no move;
- both on the rested games;
- λ_s and λ_d by week;
- v1's shadow takes at 07:05 for v2: a side where p̂_close · the morning snapshot's best price − 1 > 0, one unit, valued at the closing proxy (CLV) and at settlement. This is the figure for picking an early line.

**In #358's simulator:** a fifth fair value, `forecast2`, from its first day. Its readings stay private (#280).

**Multiplicity:** the forecasters' family at the 2027-04-12 review is two tests, v1 against no move and v2 against v1, with Holm's step-down over both (ADR 0048's method).

**No real bet or order.** A policy that takes early lines needs its own ADR, freeze and evidence (ADRs 0052 and 0054). Counts only under 4 weeks.

## Backtest evidence

From `reports/yardstick/targeted-features-20261104-ee21ec8.md`, explore half (2013-14 and 2014-15, fitted from 2012-13). It is development evidence: the features' settings were tuned on these seasons (ADR 0011), and #361's diagnostics had already read the early-season subset.

| Reading | Games | Move skill, thousandths |
| --- | --- | --- |
| Density, against the opener's drift | 2,445 | −1.343 [−1.696, −1.012] |
| The same, rested games | 1,717 | −0.373 [−0.536, −0.211] |
| Density on top of B3's split, less the split (descriptive) | 2,445 | −1.556 [−1.899, −1.219] |
| The same, rested games | 1,717 | −0.394 [−0.599, −0.195] |
| The same, back-to-back games | 728 | −4.295 [−5.303, −3.346] |
| Result beyond the close, split + density less split | 2,445 | +5.999 [+3.404, +8.713] (worse) |

**Still to come:** the frozen fit's out-of-sample history against v1 on 2013-14 to 2017-18. The build produces it before registration, and the stop above applies.

## Consequences

**The code:**
- `live/close_forecast.py`: version 2 beside version 1, density at the morning cutoff, two sizes, its weights artifact and pin;
- `close_forecasts` rows: one per version;
- `nhl live forecast`;
- the nightly check;
- `forecast2` in `live/kalshi_quotes.py`;
- the report's section.

**Lake reads:** `games`, through the results reader, for the earlier games' dates and public times.

**The leakage tests:**
- density reads only games whose results were public before the morning snapshot, and never tonight's or a later game;
- the sizes' rows are earlier weeks', public before the week's first morning;
- the weights read nothing after 2018-04-09;
- the forecast never reads its own game's close.

**What doesn't change:** v1, the decision, the morning view and `close_model`.

## Revisit when

- λ_d settles near 0, meaning density doesn't transfer to 07:05 ET, or far above 1.
- The shadow takes' CLV has its first interval, at 4 weeks.
- #370's lineup_change is kept, which would make a version 3.
- The review on 2027-04-12.
