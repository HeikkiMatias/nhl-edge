# 0030. The frozen policy's live inputs: the blend fitted on the close, and u as on history

- Status: Accepted (by the owner, 2026-10-05)
- Date: 2026-10-05

## Context

The backtest review of `backtest-20261004-6f74840` (#144) found two places where ADRs 0026 to 0028 don't fix what runs live. The live blend would then differ from the one backtested:
- **Which fit runs live.** ADR 0027 fits the blend separately for E1 (SBR's close) and E2 (SBR's 10:00 ET opener). The policy bets with E2's fit. That fit learned its market weight against a soft opener with a 4.1% margin. Live, the market input is Pinnacle's 12:45 ET price (ADR 0028), which is sharper. E2's fit would trust B3 too much and bet too often.
- **u live.** History's goalie doubt is always the goalie-start model's: a mean of 0.29 to 0.31 per fold. ADR 0026 let a confirmed starter set it to 0 live. That lowers u by about 1.17, or 1.7 of its standard deviations. Since b_u is positive, it would cut B3's weight from 0.71 to about 0.45 and switch off the doubt part of the hurdle.

## Options

1. **The live fit:**
   - E1's fit, learned against the close;
   - E2's fit, learned against the opener.
2. **Live u:**
   - the same quantity as history: goalie doubt from the goalie-start model, never a confirmation;
   - drop u from the blend (b_u = 0), which needs another run on 2021-22;
   - keep ADR 0026's wording.

## Decision

The first option of each, chosen by the owner on 2026-10-05:
- **The live blend is E1's.** It has the form and fit of ADR 0027.
  - **Training rows:** the E1 out-of-sample predictions of every priced fold before the live season: 2018-19 to 2021-22, plus 2022-23 once its one run has scored it (ADR 0025).
  - **The market input:** Pinnacle's 12:45 ET price, de-vigged multiplicatively (ADR 0008).
  - SBR's close is the sharpest price history has, and the nearest to Pinnacle at midday.
- **Live u is history's u.** Goalie doubt reads the goalie-start model's `p_start` (ADR 0012), never a confirmation. B3 itself still uses a confirmed starter where phase 5 provides one. u's scale is fitted on the live blend's training games.
- **Recorded in code:** `LIVE_BLEND_EXPERIMENT = "E1"` in `betting/selection.py`. `uncertainty.Tables` reads no confirmation table, and `tests/unit/test_freeze.py` checks both.

## Backtest evidence

Run `backtest-20261004-6f74840`, 2021-22. These are paired log-loss differences with 95% weekly block bootstrap intervals. E1's fit learned from 2018-19 to 2020-21 (3,221 games):

| E1 blend minus | 2021-22 |
| --- | --- |
| B1 | −0.0015 [−0.0043, +0.0018] |
| BLEND_B2 | −0.0018 [−0.0040, +0.0005] |
| BLEND_MARKET | −0.0022 [−0.0054, +0.0011] |

- **Calibration:** intercept +0.021 [−0.138, +0.170] and slope 1.16 [0.95, 1.39].
- **The fit** (95% intervals from its own standard errors):
  - b_m 0.58 [0.23, 0.93];
  - b_x 0.59 [0.16, 1.02];
  - b_u +0.22 [−0.06, +0.51].

  E2's fit had b_m 0.49 and b_x 0.71.
- **For bets with E1's fit: none yet.** No run has bet with it, and phase 5's paper trading at Pinnacle's 12:45 price provides that evidence.

## Consequences

- **Live bets** rest on the fit whose 2021-22 interval against B1 includes 0. E2's −0.0042 [−0.0078, −0.0007] mostly measures how soft SBR's 2021-22 opener was: that season's B0 lost +0.0049 [−0.0000, +0.0102] of log loss from close to opener, against +0.0018 [+0.0007, +0.0029] in 2011-12 to 2017-18 (the book-era diagnostic).
- **History's figures still describe E2's fit at the opener.** That covers the 465 bets, CLV and the return in ADR 0028.
- **ADR 0026's line** "Live, confirmed starters lower u" no longer holds, and neither does the same line in `game/uncertainty.py`.
- **ADR 0028's CLV explanation is corrected.** With multiplicative de-vig, CLV = (1 + fair move) / the opener's overround − 1, so the close's margin plays no part. The −1.24% comes from the opener's ~4.1% margin outweighing the +2.82% fair move.

## Revisit when

- Live u falls outside its training range often, for example because the goalie-start model's live inputs differ from history's.
- Or phase 5 shows that Pinnacle at 12:45 sits nearer SBR's opener than its close, measured by B0's log loss at each.

Any change is a new policy version, judged only on games after its own freeze date.
