# 0029. The market move guard: skip a bet when its side's probability fell more than 5.35 points since the morning

- Status: Accepted (by the owner, 2026-10-04, in the phase 4 plan). It freezes with the policy at #144.
- Date: 2026-10-04

## Context

docs/plan.md §10: "When the moneyline moved sharply between the morning and pre-game snapshots and the model sides against that move, the bet is skipped. That pattern usually means goalie or injury news the model does not have. The threshold is set on development seasons using SBR open versus close, then frozen."

The policy (ADR 0028) places live bets at the 12:45 ET snapshot, so the guard compares 07:05 with 12:45. SBR gives an opener and a close per game, and no midday price. From 2018-19 on, its close comes from a lower-margin book than its opener (#51, #65), so the development seasons' open-to-close moves mix news with differences between books.

## Options

1. **The threshold's seasons:**
   - 2011-12 to 2017-18, when one book set both prices (chosen);
   - the development seasons, as §10 words it, where the moves include book differences;
   - both.
2. **The statistic:**
   - the 95th percentile of the move (chosen): only a move larger than 19 in 20 whole-day moves counts as sharp;
   - the 90th percentile, which would fire more often;
   - a fixed number of points.
3. **Tuning the threshold on results** (which bets it would have saved). That would be tuning on seen seasons, and it is ruled out.

## Decision

As the phase 4 plan, approved by the owner on 2026-10-04 (#13):
- **θ** is the 95th percentile of the open-to-close move in SBR's de-vigged home probability (multiplicative, ADR 0008), over 2011-12 to 2017-18: **0.0535**, from 8,140 games. It is frozen in `betting/guard.py` as `MOVE_THRESHOLD`.
- **It reads prices only,** never a result or the model.
- **It departs from §10's wording.** The training seasons give a move within one book, as the live guard compares Pinnacle with Pinnacle.
- **The rule:** a bet is skipped when the de-vigged probability of its side fell by more than θ between the morning snapshot (07:05 ET) and the decision snapshot (12:45 ET), both Pinnacle's. For a home bet that is p_morning − p_decision, and for an away bet the reverse.
- **`guard.live_moves`** reads both slots from `odds_snapshots` through `odds.available_at`, so only quotes taken before the decision, for games not yet started.
- **On history** the bet is taken at the opener, before the close is known, so the guard can't act. The backtest reports how often it would have fired, had the opener been the morning price and the close the decision's. It also recomputes θ from the lake and shows it beside the frozen value.

## Backtest evidence

A local `nhl backtest` run on the development seasons at the guard's commit. These are prices only.
- **θ by season, 2011-12 to 2017-18** (95th percentile): 0.047, 0.053, 0.050, 0.051, 0.054, 0.065, 0.049. Pooled, it is 0.0535 (the 90th percentile is 0.043 and the 99th 0.075).
- **In 2021-22:**
  - 134 of 1,309 games moved more than θ from open to close, 10% against the training seasons' 5%. That fits the change of closing book.
  - The guard would have fired on 7 of E2's 465 bets.
- **Live:** `live_moves` reads the 07:05 and 12:45 Pinnacle snapshots, as a smoke check of one 2026-10-01 slate's prices showed. No result was read.

## Consequences

- **The guard mostly matters live.** Its threshold is a whole-day move, so between 07:05 and 12:45 it fires only on large news moves.
- **Phase 5's paper trading** calls it at 12:45 ET for each pick, with Pinnacle's morning and midday quotes. Two cases are fixed now, so the frozen policy leaves nothing open:
  - a game without a Pinnacle midday quote has no executable price and gets no bet (ADR 0028);
  - a game with no Pinnacle morning quote has no move to check, so the guard doesn't apply to it.
- **Tests:** `tests/unit/test_guard.py` and `tests/leakage/test_guard.py`.

## Revisit when

- Live 2026-27 shows Pinnacle's 07:05-to-12:45 moves far smaller than the whole-day moves θ rests on, so the guard never fires. Any change is a new policy version, judged only after its freeze date (plan §5).
- Or the Pinnacle midday quote is often missing.
