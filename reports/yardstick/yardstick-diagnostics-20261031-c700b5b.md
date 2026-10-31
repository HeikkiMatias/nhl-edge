# Yardstick diagnostics: explore, yardstick-diagnostics-20261031-c700b5b

Step 2 of #361 (ADR 0053), on the explore half (2012-13 to 2014-15) only. Figures are in
thousandths of squared log-odds with 95% weekly block bootstrap intervals. Negative move
skill means a forecast of SBR's move from the opener to the close beats the opener's own
drift. With this many figures, some intervals exclude zero by chance: a finding is a
claim to score once on the confirm half, not a result.

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
| finishing_diff | -0.403 [-0.549, -0.255] |
| goalie_diff | +0.023 [-0.005, +0.053] |

## Where the market learns late

Each subset (definitions in `scripts/yardstick_diagnostics.py`) and its complement: the
squared move (where the market moves most), the move skill of B2's inputs, B3's split and
all of them, and B3's split less B2's inputs on the games both scored.

| Games | Count | Squared move | B2 inputs | B3 split | All | Split less B2 |
| --- | --- | --- | --- | --- | --- | --- |
| early season | 871 | +10.995 [+9.822, +12.071] | +0.097 [-0.119, +0.315] | -0.042 [-0.467, +0.347] | -0.116 [-0.528, +0.254] | -0.075 [-0.385, +0.245] |
| not early season | 2,294 | +12.366 [+10.829, +14.025] | -0.227 [-0.491, +0.023] | -0.595 [-1.045, -0.211] | -0.557 [-0.978, -0.169] | -0.320 [-0.588, -0.065] |
| back-to-back | 950 | +14.983 [+13.430, +16.686] | -0.202 [-0.566, +0.124] | -0.251 [-0.743, +0.244] | -0.449 [-0.971, +0.045] | -0.017 [-0.376, +0.366] |
| not back-to-back | 2,215 | +10.705 [+9.336, +12.329] | -0.110 [-0.365, +0.128] | -0.554 [-1.061, -0.121] | -0.454 [-0.985, -0.010] | -0.365 [-0.615, -0.135] |
| long travel | 792 | +10.350 [+9.314, +11.345] | -0.156 [-0.453, +0.152] | -0.409 [-0.885, +0.115] | -0.390 [-0.850, +0.097] | -0.230 [-0.585, +0.152] |
| not long travel | 2,373 | +12.536 [+11.113, +14.205] | -0.132 [-0.403, +0.100] | -0.482 [-0.961, -0.073] | -0.474 [-0.956, -0.062] | -0.272 [-0.529, -0.025] |
| results ahead of xG | 792 | +10.844 [+9.529, +12.242] | +0.063 [-0.252, +0.376] | -0.108 [-0.710, +0.441] | -0.226 [-0.825, +0.317] | -0.201 [-0.593, +0.142] |
| not results ahead of xG | 2,373 | +12.371 [+11.010, +13.929] | -0.205 [-0.459, +0.027] | -0.577 [-1.012, -0.195] | -0.524 [-0.967, -0.137] | -0.281 [-0.532, -0.031] |
| goalie surprise | 1,503 | +13.235 [+11.669, +15.032] | -0.181 [-0.487, +0.083] | -0.454 [-0.822, -0.095] | -0.496 [-0.857, -0.148] | -0.280 [-0.577, +0.018] |
| not goalie surprise | 1,662 | +10.862 [+9.373, +12.634] | -0.099 [-0.414, +0.189] | -0.473 [-1.040, +0.047] | -0.413 [-1.005, +0.114] | -0.245 [-0.546, +0.027] |
