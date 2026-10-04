# 0029. The market move guard: skip a bet whose side's probability fell more than 5.35 points since the morning

- Status: Proposed
- Date: 2026-10-04

## Context

docs/plan.md §10 skips a bet when the moneyline moved sharply against it between the morning and the decision, which usually means news the model lacks. Its threshold is "set on development seasons using SBR open versus close, then frozen".

Live bets are placed at 12:45 ET (ADR 0028), so the guard compares 07:05 with 12:45. SBR has no midday price. From 2018-19 on, SBR's close also comes from a lower-margin book than its opener (#65), so the development seasons' moves mix news with differences between books.

## Options

1. **The threshold's seasons:**
   - 2011-12 to 2017-18, when one book set both prices;
   - the development seasons, as §10 words it;
   - both.
2. **The statistic:**
   - the 95th percentile of the move in either direction;
   - the 90th percentile;
   - a fixed number of points.
3. **Tuning the threshold on which bets it would have saved.** That would tune on seen seasons, and it is ruled out.

## Decision

The first option of each, as in the phase 4 plan approved by the owner on 2026-10-04 (`betting/guard.py`):
- **θ** is the 95th percentile of SBR's open-to-close move in the de-vigged home probability (multiplicative, ADR 0008) over 2011-12 to 2017-18: **0.0535**, from 8,140 games, frozen as `MOVE_THRESHOLD`.
  - It reads prices only, never a result or the model.
  - It departs from §10's wording, since one book gives a move like Pinnacle against Pinnacle.
- **The rule:** a bet is skipped when the de-vigged probability of its side fell by more than θ between Pinnacle's 07:05 and 12:45 ET quotes of the decision day.
  - The guard fires on a fall against one side only, so a given side is skipped in about 1 game in 40.
  - A game with no game-day 07:05 quote has no move to check, so the guard doesn't apply to it.
- **On history** the bet is taken at the opener, before the close is known, so the guard can't act. The backtest reports θ recomputed from the lake, and how often the guard would have fired between the opener and the close.

## Backtest evidence

None yet. The guard acts only on live bets, and phase 5's paper trading provides its evidence. Run `backtest-20261004-6f74840` gives only these descriptive counts:
- θ recomputed from the lake is 0.0535, the same as the frozen value.
- By season, 2011-12 to 2017-18, θ runs from 0.047 to 0.065.
- In 2021-22, 134 of 1,309 games moved more than θ from open to close (10%, against the training seasons' 5%, as the change of closing book predicts). The guard would have fired on 7 of E2's 465 bets.

## Consequences

- **The guard mostly matters live.** θ is a whole-day move, so between 07:05 and 12:45 it fires only on large news moves.
- **Phase 5 calls it at 12:45 ET** for each pick. `tests/leakage/test_guard.py` checks that it reads only the decision day's quotes taken before the decision.

## Revisit when

- Live Pinnacle 07:05-to-12:45 moves turn out far smaller than θ's whole-day moves, so the guard never fires.
- Or the 07:05 Pinnacle quote is often missing.

Any change is a new policy version, judged only after its own freeze date (plan §5).
