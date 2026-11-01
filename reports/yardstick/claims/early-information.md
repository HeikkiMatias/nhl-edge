# Claim: early-information

Written down on 2026-11-01 from the explore half (2013-14 and 2014-15), before any game of the confirm half (2015-16 to 2017-18) was read. The owner chose it (#361, ADR 0053 as amended). It is scored once by `nhl research yardstick --half confirm --claim early-information --r2`, which reads its tests from `yardstick.CLAIMS`.

## The tests

The three tests are one family. Every candidate is fitted per season on the earlier seasons from 2012-13. Figures are in thousandths of squared log-odds, from `yardstick-20261101-c7bfa52` (`reports/yardstick/explore-yardstick-20261101-c7bfa52.md`).

1. **B3's split anticipates the move:** its move skill is negative.
   - Explore: −0.464 [−0.842, −0.134], p 0.003.
2. **Team strength alone anticipates the move:** ΔS's move skill is negative.
   - Explore: −0.256 [−0.397, −0.140], p below 0.001.
3. **B2's inputs anticipate it more on back-to-backs:** their move skill on games where either team played the day before, less that on the rested games, is negative.
   - Explore: −0.376 [−0.743, −0.010], p 0.045.
   - The two groups are resampled apart, each by its weeks, as `metrics.difference` does.

## The rule

- **The family:** each test's two-sided weekly block bootstrap p-value goes through Holm's step-down at 0.05 over the three tests (ADR 0048).
- **When a test holds:** it passes Holm's step-down and its estimate is negative.
- Nothing else from the run is read as a verdict.

## What follows

- **Tests 1 and 2** decide whether the model's features are worth a fair value for quoting early: #358, and step 3's close forecaster.
- **Test 3** decides whether back-to-backs get their own term in that forecaster.
- **A failed test stays failed:** the confirm half is not read again for it.
