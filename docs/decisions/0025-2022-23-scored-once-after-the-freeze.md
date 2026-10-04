# 0025. 2022-23: its 342 SBR-priced games are scored once, last, after the freeze

- Status: Accepted (by the owner, 2026-10-04)
- Date: 2026-10-04

## Context

docs/plan.md §5 makes 2022-23 the market validation season, and phase 4's full backtest covers the development seasons and 2022-23 (§6). The SBR archive stops on 2022-11-27 (ADR 0006, #7), so it prices 342 of 2022-23's 1,312 games (26.1%), all from October and November. Phase 1 left those prices unread (#10). #66 asked phase 4 to choose the season's role, and to run the audit's price checks before any of its games is scored.

The price checks are now run (`nhl audit report`, SBR section; they read prices only, never a result or a model):
- **Join:** all 342 match an NHL game, with no score mismatch. One SBR price is missing, 2022020259's closing total, and none on the moneyline.
- **Vig:** the opening median is 4.1% (10th to 90th percentile 3.7% to 4.7%), and the closing median 2.4% (2.0% to 2.7%). None sums below 100%. This is the same two-book pattern as 2018-19 to 2021-22 (#65).
- **Open against close:** the median move is 1.9 points and the 90th percentile 5.3. None moves more than 15 points, the favourite changes in 15 games, and every opener differs from its close.
- **Puck line:** no closing moneyline favourite is +1.5 on its closing puck line (#64's check).
- **Suspect openers:** none meets a criterion of #56, so the list stays at 40.
- **E2's bounds (ADR 0007):** the closes before the season bound the openers to 0.211 to 0.840. 2022-23's run from 0.300 to 0.786, so E2 refuses none.

## Options

1. **Score the 342 priced games.** It's free, but the intervals will be wide, and early-season games carry the least in-season information.
2. **Buy the historical odds for 2022-23 onward** (about $90, a paid endpoint). That would mean more market seasons and real Pinnacle closes, but it reverses plan §9's decision that v1 stays free.
3. **Drop 2022-23 as a market test.** Live 2026-27 would then carry the whole market test, and the blend's historical evidence would be 2021-22 alone.

If option 1 is chosen, the 342 games can either be scored along with the development seasons, or once, last, after the blend and the policy are frozen.

## Decision

Option 1, scored once and last. The owner chose it on 2026-10-04 (phase 4 plan, #13).

- **When:** after the blend, u, the selection policy and the guard are frozen (#144), one run (#145) scores the 342 games. Nothing is changed for it.
- **What it scores:** the blend against B1 on E1 and E2, the B3 blend against the B2 blend (hard rule 3), and E3 on the bets the frozen policy selects. Each comes with its weekly block bootstrap interval (hard rule 7).
- **Once only:** the run is claimed before anything is scored, as `backtest/one_time.py` does for 2025-26, so a second run is refused.
- **The blend it scores** learns from the out-of-sample predictions of 2018-19 to 2021-22 (the whole-season folds of the phase 4 plan).
- **E2's openers:** ADR 0007's bounds apply, as for any fold.

The price checks found nothing to fix or refuse. Scoring the season last keeps it as a check that no phase 4 choice was made on, and it costs nothing.

## Backtest evidence

None yet. #145's one-time run will provide it.

## Consequences

- **What reads 2022-23 now:** the audit reads its prices (`PRICE_ROLES` in `audit/sbr.py`), and the suspect-opener list covers it. Nothing reads its results or scores a model on it until #145. `nhl backtest` keeps refusing the season until #145 adds the one-time run.
- **A thin test:** 342 early-season games give wide intervals. B3's ratings in October and November still rest mostly on the season before. The report says so beside its figures. §1's per-season rule ("no test season losing more than the pooled gain") counts 2022-23 as a test season.
- **The other 970 games** stay unused as a market test. Their results already train later folds, as they did at gate 2.
- **Once scored,** 2022-23 counts as development evidence (plan §5).

## Revisit when

- The owner buys the historical odds (plan §9's first v2 upgrade). Then the season's other games and the later seasons could become market tests, decided before any of them is scored.
- Or #145's run finds a data problem the price checks missed.
