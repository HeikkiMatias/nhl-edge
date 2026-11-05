# #359: B3's projected total against SBR's totals, explore half (totals-history-20261105-0ebf4c5)

Scored seasons 20132014, 20142015, each fitted on the games from 20122013 whose results were public before its fold start (ADR 0053). By the rule on #359 of 2026-11-05. Figures are means in thousandths with weekly block bootstrap 95% intervals; negative favours the projection.

- **move:** the squared-error skill on y = μ_c - μ_o, the implied mean total's move from the opener to the close, against the opener's own drift (goals²).
- **result:** the log-loss difference of the over at the closing line, pushes dropped, beyond logit p_over_c and μ_c.

| Candidate | Measure | All games | p | Rested games | p |
| --- | --- | --- | --- | --- | --- |
| total | move | +0.47 [+0.12, +0.85] | 0.0080 | +0.64 [+0.19, +1.08] | 0.0050 |
| total | result | -0.67 [-2.15, +0.81] | 0.3680 | -0.90 [-2.64, +0.95] | 0.3140 |
| total split | move | -0.10 [-0.48, +0.28] | 0.6230 | +0.05 [-0.39, +0.48] | 0.8200 |
| total split | result | -1.08 [-3.81, +1.80] | 0.4460 | -2.59 [-5.40, +0.34] | 0.0870 |

## Per season

| Candidate | Measure | Season | Estimate | Games |
| --- | --- | --- | --- | --- |
| total | move | 20132014 | +1.62 [+1.01, +2.26] | 1227 |
| total | move | 20142015 | -0.68 [-1.06, -0.31] | 1218 |
| total | result | 20132014 | -1.57 [-3.59, +0.58] | 1101 |
| total | result | 20142015 | +0.24 [-1.99, +2.28] | 1076 |
| total split | move | 20132014 | +0.26 [-0.27, +0.80] | 1227 |
| total split | move | 20142015 | -0.46 [-1.05, +0.11] | 1218 |
| total split | result | 20132014 | -1.95 [-6.95, +3.56] | 1101 |
| total split | result | 20142015 | -0.19 [-2.05, +1.57] | 1076 |

## Described, not tested

- 2445 games, 1717 rested; 268 pushes at the closing line; the line moved from the opener to the close on 266.
- Mean projected total 4.825; mean implied total 5.455 at the opener and 5.428 at the close; mean final total 5.481.
- The projection's correlation with μ_o 0.479, with μ_c 0.498, with the final total 0.073; μ_c's with the final total 0.113.
- The opener's drift leaves a squared error of 0.01739 goals² on the move.

## Supported on the explore half

None: no measure's interval over all games lies below 0, so no claim and no confirm run (the rule on #359).

Caveats: B3's settings were tuned on these seasons' moneyline results (ADR 0011), so this is no clean holdout; SBR's opener may be from the evening before (ADR 0006), so the rested games are shown beside all.
