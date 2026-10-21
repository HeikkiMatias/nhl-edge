# 0028. The selection and staking policy: expected return at the executable price, a hurdle that rises with doubt, quarter Kelly with caps, bets at the midday snapshot

- Status: Accepted (by the owner, 2026-10-04); amended by 0030 (live inputs, CLV wording)
- Date: 2026-10-04

## Context

Gate 3 needs a selection policy frozen before live games (docs/plan.md §1, §6). §5 and §11 give its outline:
- bets selected on expected return at the executable price (hard rule 4);
- at least 2.5% expected return, more when u is high;
- a quarter of Kelly, at most 1.5% of bankroll per bet and 5% per day, and one bet per game.

§5 says a higher u raises the hurdle and lowers the stake, without numbers. The policy must also say when live bets are placed. Odds are snapshotted at 07:05, 12:45 and before games (US Eastern), and history has only SBR's opener and close (ADR 0006).

## Options

1. **How u moves the hurdle and the stake:**
   - continuously, one point of hurdle per standard deviation of doubt, with the stake divided by (1 + u⁺);
   - in tiers by u's quantiles;
   - not at all.
2. **When live bets are placed:**
   - at 12:45 ET, with the move guard against 07:05;
   - at 07:05, where the guard never fires;
   - at the last pre-game snapshot, where closing line value is near 0 by design.

## Decision

The first option of each, as in the phase 4 plan approved by the owner on 2026-10-04 (`betting/selection.py`, `betting/staking.py`):
- **Selection.** EV_home = p·o_home − 1 and EV_away = (1 − p)·o_away − 1, with p the blend's full-game probability (ADR 0027).
  - The side with the higher EV is bet when it clears 2.5% plus 1 point per standard deviation of u above its training mean.
  - Below-average doubt counts as average.
  - One bet per game.
- **Stake.** ¼·EV/(o − 1), divided by (1 + u⁺) and capped at 1.5% of the bankroll at the start of the day. A day's stakes are scaled down together to at most 5%.
  - Each bet is decided before its own game, and a day's bets share one decision time.
  - A day's bankroll counts only results public before that decision.
- **Bankroll.** 100 paper units per season, settled on the full game (hard rule 2). Live, a 20% drawdown calls for a review of data and code, never a model change. The backtest gives no verdict on it.
- **Live (phase 5):** bets are placed at the 12:45 ET snapshot, at Pinnacle's price, with the best EU book logged beside it. A game without a Pinnacle 12:45 price gets no bet.
- **History (E2):** the price taken is SBR's opener.

## Backtest evidence

Run `backtest-20261004-6f74840` in reports/backtest/runs.csv. 2021-22 at the SBR opener, the only season with a blend:
- **Bets:** 465 of 1,306 games (109 home, 356 away), with a mean expected return by the blend of 7.7%.
- **Return per unit staked, per bet:** +6.1% [−0.5%, +13.3%], 95% weekly block bootstrap. The interval includes 0.
- **ROI and the paper bankroll:** ROI +9.5%, a final bankroll of 164.0 units, and a largest drawdown of 14.2%. These are secondary figures: over a few hundred bets they are mostly noise (§11).
- **Against the close (E3, #143):**
  - CLV per bet is −1.24% [−2.15%, −0.24%] against SBR's close.
  - The fair move toward the bets is +2.82% [+1.90%, +3.85%].
  - The market moved toward the picks, but not by enough to pay back the opener's ~4.1% margin. With multiplicative de-vig, CLV = (1 + fair move) / the opener's overround − 1, so the close's margin plays no part (corrected by 0030).

## Consequences

- **The backtest reports bets** (`summary.json`'s `bets`, `bets.csv`) and E3. `tests/leakage/test_bets.py` checks that picks read the opener, never the close or the result.
- **Historical CLV fails §1's criterion against SBR's close.** Whether Pinnacle's lower margin on the price taken turns the fair move into positive CLV is phase 5's question (corrected by 0030).
- **36% of games are bet,** at a mean EV far above what a sharp market allows. The blend disagrees with the opener often, mostly backing underdogs and away teams.

## Revisit when

- Phase 5 shows the 12:45 snapshot often lacks Pinnacle's price.
- Or a live 20% drawdown calls for a review of data and code. That review never changes the policy for its results.

Any change after the freeze is a new policy version, judged only after its own freeze date, and the CLV count restarts.
