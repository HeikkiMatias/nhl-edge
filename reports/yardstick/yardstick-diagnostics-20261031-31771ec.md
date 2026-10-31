# Yardstick diagnostics: explore, yardstick-diagnostics-20261031-31771ec

Step 2 of #361 (ADR 0053), on the explore half only. Every candidate is fitted from
2012-13 and scored on 2013-14 and 2014-15, so all compare on the same games and fits.
Figures are in thousandths of squared log-odds with 95% weekly block bootstrap intervals.
Negative move skill means a forecast of SBR's move from the opener to the close beats the
opener's own drift. With this many figures, some intervals exclude zero by chance: a
finding is a claim to score once on the confirm half, not a result.

## 2011-12 in the fit

Each candidate that can read 2011-12, fitted from 2011-12 less fitted from 2012-13, on
the same games. Positive means 2011-12 makes the fit worse.

| Candidate | From 2011-12 less from 2012-13 |
| --- | --- |
| B2 inputs | +0.108 [+0.030, +0.188] |
| delta_g_hat | +0.445 [+0.327, +0.573] |
| delta_s | +0.015 [-0.035, +0.065] |
| five_diff | +0.241 [+0.142, +0.345] |
| special_diff | +0.009 [-0.015, +0.034] |
| goalie_diff | +0.055 [+0.035, +0.074] |

## What each part adds

All the features together less all of them without the part, game by game. Negative
means the part adds beyond the others.

| Part | All less all without it |
| --- | --- |
| delta_s | -0.015 [-0.039, +0.010] |
| delta_g | +0.003 [-0.035, +0.041] |
| h_s | +0.021 [-0.020, +0.066] |
| five_diff | -0.122 [-0.272, +0.029] |
| special_diff | +0.033 [-0.029, +0.100] |
| finishing_diff | +0.050 [-0.036, +0.132] |
| goalie_diff | +0.023 [-0.005, +0.053] |

## Where the market learns late

Each subset (definitions in `scripts/yardstick_diagnostics.py`) and its complement: the
squared move (where the market moves most), the move skill of B2's inputs, B3's split and
all of them, and B3's split less B2's inputs on the games both scored.

| Games | Count | Squared move | B2 inputs | B3 split | All | Split less B2 |
| --- | --- | --- | --- | --- | --- | --- |
| early season | 581 | +10.419 [+8.991, +11.726] | +0.044 [-0.221, +0.299] | -0.042 [-0.467, +0.347] | -0.116 [-0.528, +0.254] | -0.086 [-0.357, +0.175] |
| not early season | 1,864 | +12.413 [+10.804, +14.214] | -0.421 [-0.611, -0.244] | -0.595 [-1.045, -0.211] | -0.557 [-0.978, -0.169] | -0.174 [-0.472, +0.110] |
| back-to-back | 728 | +15.073 [+13.281, +16.916] | -0.575 [-0.881, -0.283] | -0.251 [-0.743, +0.244] | -0.449 [-0.971, +0.045] | +0.324 [-0.042, +0.716] |
| not back-to-back | 1,717 | +10.610 [+8.959, +12.624] | -0.199 [-0.454, +0.040] | -0.554 [-1.061, -0.121] | -0.454 [-0.985, -0.010] | -0.355 [-0.630, -0.110] |
| long travel | 616 | +10.056 [+8.950, +11.158] | -0.257 [-0.553, +0.046] | -0.409 [-0.885, +0.115] | -0.390 [-0.850, +0.097] | -0.152 [-0.489, +0.229] |
| not long travel | 1,829 | +12.573 [+10.881, +14.561] | -0.329 [-0.556, -0.123] | -0.482 [-0.961, -0.073] | -0.474 [-0.956, -0.062] | -0.154 [-0.454, +0.126] |
| results ahead of xG | 612 | +9.626 [+8.373, +10.935] | -0.218 [-0.545, +0.102] | -0.079 [-0.633, +0.438] | -0.187 [-0.754, +0.333] | +0.138 [-0.211, +0.432] |
| not results ahead of xG | 1,833 | +12.711 [+11.121, +14.614] | -0.342 [-0.559, -0.131] | -0.592 [-1.023, -0.208] | -0.541 [-0.991, -0.142] | -0.251 [-0.544, +0.022] |
| goalie surprise | 1,161 | +12.847 [+11.385, +14.333] | -0.367 [-0.590, -0.160] | -0.454 [-0.822, -0.095] | -0.496 [-0.857, -0.148] | -0.087 [-0.367, +0.200] |
| not goalie surprise | 1,284 | +11.118 [+9.221, +13.685] | -0.260 [-0.547, +0.018] | -0.473 [-1.040, +0.047] | -0.413 [-1.005, +0.114] | -0.214 [-0.552, +0.106] |
