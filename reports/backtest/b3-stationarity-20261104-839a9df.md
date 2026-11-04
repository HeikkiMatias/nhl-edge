# B3's mapping across the training seasons (b3-stationarity-20261104-839a9df)

#378 step 1, with its rule fixed on the issue before this run. B3's logistic is fitted on
each training season's own games, as B3 trains (Δĝ from the starters, schedule terms,
empty seats, h_s as the offset), without a penalty. Intervals are weekly block bootstrap
intervals within each season (2000 draws). The settings behind the inputs
were tuned on these seasons (ADR 0011), so this is a descriptive check of stability.

| Season | Games | Δĝ's coefficient per goal | Over the pooled fit's | Intercept at zero inputs |
|---|---|---|---|---|
| 2012-13 | 720 | +1.128 [+0.615, +1.674] | +0.80 [+0.43, +1.18] | -0.044 [-0.231, +0.161] |
| 2013-14 | 1,230 | +1.187 [+0.757, +1.675] | +0.84 [+0.53, +1.18] | -0.082 [-0.236, +0.063] |
| 2014-15 | 1,230 | +1.499 [+1.134, +1.938] | +1.06 [+0.80, +1.37] | -0.059 [-0.257, +0.124] |
| 2015-16 | 1,230 | +1.320 [+0.899, +1.751] | +0.93 [+0.63, +1.24] | +0.037 [-0.149, +0.230] |
| 2016-17 | 1,230 | +1.705 [+1.262, +2.173] | +1.20 [+0.89, +1.53] | +0.011 [-0.186, +0.216] |
| 2017-18 | 1,271 | +1.546 [+1.056, +2.085] | +1.09 [+0.75, +1.47] | +0.079 [-0.113, +0.262] |
| Pooled | 6,911 | +1.417 | 1 | -0.006 |

**The tests** (Cochran's Q, each value weighted by its bootstrap variance; Holm over the two):

| Test | Q | df | p | Holm threshold | Heterogeneous |
|---|---|---|---|---|---|
| coefficient | 4.22 | 5 | 0.518 | 0.025 | no |
| intercept | 2.39 | 5 | 0.793 | 0.050 | no |

**Step 2 stops:** neither test finds the mapping heterogeneous across the training seasons, so under #378's rule the issue closes as rejected. The drift seen from 2018-19 on is not read here.
