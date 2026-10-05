# 0031. Gate 3: the blend adds no information beyond the market on history; the frozen policy goes to paper trading

- Status: Accepted (by the owner, 2026-10-05)
- Date: 2026-10-05

## Context

Gate 3 (docs/plan.md §6) judges §1's criteria on history, once the selection policy is frozen before live games. The policy was frozen on 2026-10-05 as `policy-20261005-8ec5cf3` (ADRs 0026 to 0030). The evidence comes from two runs:
- **2021-22,** the blend's development test: `backtest-20261005-b7639f5`, which reproduces `backtest-20261004-6f74840` exactly.
- **2022-23's 342 SBR-priced games,** scored once after the freeze: `market-validation-20261005-6ec331b` (ADR 0025).

On 2026-10-04 the owner decided that a failed criterion is recorded honestly and phase 5 goes ahead.

## Options

1. **Record the verdict and start phase 5** with the frozen policy. Its bets are paper bets, and live 2026-27 is the remaining test.
2. **Revise the blend or the policy now.** That would tune on seasons already seen, and a revision would be a new policy version, judged only on games after its own freeze.
3. **Stop before phase 5.**

## Decision

Option 1, as the owner decided. The pooled figures combine each season's weekly block bootstrap with a normal approximation, since the one run keeps no per-game rows. The bootstrap draws weeks within each season.

| §1 criterion | Evidence (95% intervals) | Verdict |
| --- | --- | --- |
| Adds information | BLEND minus B1: E1 2021-22 −0.0015 [−0.0043, +0.0018], 2022-23 +0.0044 [−0.0062, +0.0180], pooled −0.0002 [−0.0037, +0.0032]. E2 −0.0042 [−0.0078, −0.0007], +0.0033 [−0.0118, +0.0224], pooled −0.0027 [−0.0072, +0.0019]. 2022-23 loses more than the pooled gain on both. | **Fails** |
| Player layer | B3 minus B2 over 2018-19, 2021-22 and 2022-23: E1 −0.0068 [−0.0120, −0.0016], E2 −0.0068 [−0.0119, −0.0016]. After trades −0.0093 [−0.0151, −0.0035] and after injuries −0.0101 [−0.0155, −0.0046] (development seasons); lineup changes, 39 games, no interval. At the blend level, BLEND minus BLEND_B2 pooled: E1 −0.0005 [−0.0035, +0.0026], E2 −0.0018 [−0.0059, +0.0024]. | **Passes** for B3 against B2; not shown at the blend level |
| Calibrated | The blend: 2021-22 intercept +0.021 [−0.138, +0.170], slope 1.16 [0.95, 1.39]; 2022-23 intercept +0.003 [−0.189, +0.179], slope 0.71 [0.25, 1.21] (E1; E2 alike). | **Passes** on intervals, with the slope swinging from under- to over-confident |
| Favourable prices | CLV against SBR's close, 625 bets: −1.23% [−2.01%, −0.46%] (2021-22 −1.24% [−2.15%, −0.24%], 2022-23 −1.22% [−2.41%, −0.01%]). Fair move +2.83% [+2.04%, +3.62%]. | **Fails on history.** §1's Pinnacle test is live only. |
| Large disagreements | 53 games (2021-22) and 35 (2022-23) reviewed by hand. One swapped SBR opener, no bug. | **Passes** |

## Backtest evidence

The runs above, in reports/backtest/runs.csv. The return per unit staked, pooled, is +7.8% [+1.0%, +14.6%]. It is not a criterion:
- §11 counts ROI over a few hundred bets as mostly noise.
- The bets were taken at a soft 10:00 opener that live bets never see.

## Consequences

- **Gate 3 is not met.** Phase 5 starts with the frozen policy, and every bet is a paper bet. §1's remaining tests are live:
  - CLV against Pinnacle's closing proxy on games from 2026-10-06;
  - the blend against B1 on live E2.
- **ADR 0026's revisit trigger fired.** 2022-23's fits put b_u at +0.24 [+0.01, +0.48] on E1 and +0.25 [+0.02, +0.49] on E2. Doubt now significantly raises the model's weight. The frozen policy keeps u as it is: changing it on a seen season would be tuning. Live evidence decides, and any change is a new policy version.
- **`reports/backtest/accepted.json`** records these runs as the frozen policy's baseline, with gate 3 not met.
- **2022-23** now counts as development evidence (ADR 0025).

## Revisit when

- Phase 5's gate judges live CLV against Pinnacle and the live blend against B1.
- Or u's weight stays significantly positive once live games join the blend's training rows.
