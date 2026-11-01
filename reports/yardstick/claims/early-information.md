# Claim: early-information

Written down on 2026-11-01 from the explore half (2013-14 and 2014-15), before any game of the confirm half (2015-16 to 2017-18) was scored. The owner chose it (#361, ADR 0053 as amended). It is scored once by `nhl research yardstick --half confirm --claim early-information --r2`, which reads its tests from `yardstick.CLAIMS` and scores only the claim's candidates.

## The tests

The two tests are one family. Each is on the rested games, where neither team played the day before (the owner's choice). There, neither team's own last game falls between an opener posted the evening before (ADR 0006) and 10:00 ET. League-wide refits (team strength's league rates, RAPM's joint fit) still take in the night's other games, so the rested games narrow that gap without closing it. Every candidate is fitted per season on the earlier seasons from 2012-13. Figures are in thousandths of squared log-odds, explore half, 1,717 rested games.

1. `B3 split: move_rested`: B3's split anticipates the move on the rested games. Its move skill there is negative.
   - Explore: −0.554 [−1.061, −0.121], p 0.004.
2. `delta_s: move_rested`: team strength alone anticipates the move on the rested games. ΔS's move skill there is negative.
   - Explore: −0.314 [−0.499, −0.159], p below 0.001.

## The rule

- **The family:** each test's two-sided weekly block bootstrap p-value goes through Holm's step-down at 0.05 over the two tests (ADR 0048), with thresholds 0.025 and 0.05.
- **When a test holds:** it passes Holm's step-down and its estimate is negative.
- Nothing else from the run is read as a verdict.

## Before the confirm run

- **A third test was dropped:** that B2's inputs anticipate the move more on back-to-backs than on rested games (explore −0.376 [−0.743, −0.010]).
  - **Why:** the leakage check found it can't tell early information from a stale opener. On a back-to-back, the 10:00 ET inputs, the goalie terms most, already know the previous night's game, which an opener of the evening before may not.
  - The explore edge came from the goalie terms: ΔG −0.297 [−0.507, −0.089]. Team strength was better on the rested games.
  - The question waits for a source that dates SBR's openers (ADR 0006's revisit condition).
- **The features are in-sample in their settings:** they were tuned on 2011-12 to 2017-18's results, the confirm half included (ADR 0011, ADR 0053). The confirm half is fresh for this claim's tests, not for the features' settings, so a hold is development evidence. The confirmation that counts is live (ADR 0038).

## What follows

- **The tests together** decide whether the model's features are worth a fair value for quoting early: #358, and step 3's close forecaster.
- **A failed test stays failed:** the confirm half is not read again for it.
