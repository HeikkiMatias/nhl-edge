# 0028. The selection and staking policy: expected return at the executable price, a hurdle that rises with doubt, quarter Kelly with caps, bets at the midday snapshot

- Status: Accepted (by the owner, 2026-10-04, in the phase 4 plan). It freezes at #144, and any change after that restarts the CLV count (plan §5).
- Date: 2026-10-04

## Context

Gate 3 needs a selection policy frozen before live games (docs/plan.md §1, §6). §5 and §11 give its outline:
- bets selected on expected return at the executable price, never on the probability gap (hard rule 4);
- at least 2.5% expected return, more when the uncertainty score u is high;
- a quarter of Kelly, at most 1.5% of bankroll per bet and 5% per day, one bet per game;
- a 100-unit paper bankroll, and a review of data and code at a 20% drawdown.

§5 adds that a higher u raises the hurdle and lowers the stake, without numbers. The policy must also say when a live bet is placed. The odds are snapshotted at 07:05, 12:45 and before games (US Eastern), and history has only SBR's opener and close (ADR 0006).

## Options

1. **How u moves the hurdle and the stake:**
   - continuously: one point of hurdle and a stake divided by (1 + u⁺) per standard deviation of doubt (chosen);
   - in tiers by u's quantiles;
   - not at all, leaving u to the blend alone.
2. **When live bets are placed:**
   - at 12:45 ET, with the move guard against 07:05 (chosen);
   - at 07:05, closest to history's opener, where the guard never fires;
   - at the last pre-game snapshot, where the price is the closing proxy itself and closing line value is near zero by design.

## Decision

As the phase 4 plan, approved by the owner on 2026-10-04 (#13). `betting/selection.py` and `betting/staking.py` hold it as `Policy`:
- **Selection.** Each side's expected return at its own decimal price: EV_home = p·o_home − 1 and EV_away = (1 − p)·o_away − 1, with p the blend's full-game home probability (ADR 0027, hard rule 2).
  - The side with the higher EV is bet when it clears the hurdle: 2.5%, plus 1 point for each standard deviation of u above its training mean (ADR 0026), where below average counts as average.
  - At most one bet per game.
- **Stake.** A quarter of Kelly, ¼·EV/(o − 1), divided by (1 + u⁺), where u⁺ is u's standard deviations above average or 0.
  - It is capped at 1.5% of the bankroll at the start of the day.
  - When a day's shares add up to more than 5%, all of them are scaled down together to 5%.
  - Every bet of a day must be decided before the day's first game starts, or the ledger is refused. A day's bankroll counts only the earlier results that were public before its first decision.
- **Bankroll.** 100 paper units per season. Bets settle on the full game, overtime and shootout included. A 20% drawdown calls for a review of data and code, never a model change.
- **Live bets** (phase 5) are decided and placed at the 12:45 ET snapshot. That is late enough for most morning-skate goalie news, with hours left before the closing proxy.
  - The executable price is Pinnacle's at that snapshot, and the best EU book's price is logged beside it (plan §9).
  - The market input is that snapshot's de-vigged Pinnacle price (ADR 0008), and the market move guard compares it with 07:05 (ADR 0029).
- **On history (E2),** the price taken is SBR's opener at E2's prediction time (ADR 0006). History has no midday price.

## Backtest evidence

A local `nhl backtest` run on the development seasons at commit `cb3453b`. #144's committed run logs it in `reports/backtest/runs.csv`. E2's blend bets in 2021-22 only, since 2018-19 has no blend (ADR 0027).

| 2021-22, at the SBR opener | |
| --- | --- |
| Games considered | 1,306 |
| Bets | 465 (109 home, 356 away) |
| Mean expected return, by the blend | 7.7% at a mean price of 2.08 |
| Return per unit staked, per bet | +6.1% [−0.5%, +13.3%] (95% weekly block bootstrap) |
| Units staked, profit, ROI | 674.9, +64.0, +9.5% |
| Final bankroll, largest drawdown | 164.0, 14.2% |

The return's interval includes 0. ROI over a few hundred bets is mostly noise (plan §11).

**The judgement belongs to E3 (#143):** whether the opener prices taken beat SBR's close. A mean expected return of 7.7% on 36% of games is far more than a sharp market should allow. It says the blend disagrees with the opener often, mostly by backing underdogs and away teams. Whether that disagreement is information or error is what E3 and live 2026-27 test.

## Consequences

- **The backtest now reports bets** (`summary.json`'s `bets` section, `reports/backtest/bets.csv`): the bets per season, their mean EV and price, units staked and won, ROI, the return per unit with its interval, the final bankroll and the largest drawdown.
- **Tests:** selection has unit tests (§5's 52.5% at 1.90 is no bet, the doubt hurdle, the caps), and golden settlement tests on regulation, overtime, shootout and empty-net games. A leakage test checks that picks read the opener and never the close or the result, and that a day's stakes read only earlier days.
- **Phase 5's paper trading** applies this policy at 12:45 ET with Pinnacle's price. History's opener is from another book and time, a known gap the model card records.

## Revisit when

- A 20% drawdown on live paper bets. That calls for a review of data and code only, never a change of policy for its results.
- Or phase 5 shows the 12:45 snapshot often lacks Pinnacle's price for games that day.

Any change after the freeze is a new policy version, judged only on games after its own freeze date, and it restarts the CLV count.
