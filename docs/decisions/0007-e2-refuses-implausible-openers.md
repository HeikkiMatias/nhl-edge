# 0007. E2 refuses implausible SBR openers

- Status: Proposed (the owner chose this option on 2026-09-30; the record awaits his confirmation)
- Date: 2026-09-30

## Context

#56 lists 40 SBR openers of 2010-11 to 2021-22 that are likely wrong, with four flags:
- **extreme_open:** the de-vigged home probability is outside 0.15 to 0.85. There are 8, such as Edmonton -1010 against Minnesota 705 on 2022-02-20, which SBR's own page prints that way.
- **swapped:** the opener names the other favourite, and swapping its sides lands within 5 points of the close. There are 15.
- **below_100:** the two prices sum below 100%. There are 3.
- **big_move:** the opener moves more than 15 points to the close. There are 33.

E2 bets at the opener. A wrong opener makes E2's result partly a result on typos. On 2026-09-29 the owner chose to drop all 40. Codex then rated that a P0 under hard rule 1 on PR #59: big_move and swapped compare the opener with the close, which is public only at the start. Dropping those games would pick E2's sample with information its prediction lacks, and flatter E2.

## Options

1. **Keep every opener.** This is point-in-time, but E2 then scores the 8 impossible prices as if a book had offered them.
2. **Drop all 40, or the 26 with a hard flag.** This picks E2's sample with the close (hard rule 1). For the development seasons it cuts E2's cost against E1 to +0.0026, partly by dropping real news moves.
3. **Refuse the openers that are implausible on their own.** Refuse an opener whose de-vigged home probability is outside 0.15 to 0.85. The rule reads the opener alone, so a live E2 could apply it when it reads the price. The other 32 listed openers stay in.

## Decision

Option 3, chosen by the owner on the decision card of 2026-09-30.

E2 refuses an opener when its home probability, de-vigged under B1's method, is outside `EXTREME_OPEN` (`walk_forward.implausible`). The refused game leaves E2's scoring and B1's E2 fits, and coverage counts it as `implausible`. Openers below 100% are refused by de-vigging, as before. E1 reads the close and is unchanged. Every backtest also reports E2 on every opener, under `sensitivity` in summary.json.

## Backtest evidence

Run `backtest-20260930-e908d53` in `reports/backtest/runs.csv`, on 2018-19 and 2021-22, with 95% weekly block bootstrap intervals. E2 refuses 5 scored test games.

| E2, B0 multiplicative | Log loss | E2 − E1 |
| --- | --- | --- |
| Refusing implausible openers | 0.6605 [0.6505, 0.6700] | +0.0034 [+0.0006, +0.0064] |
| Every opener | 0.6617 [0.6512, 0.6716] | +0.0046 [+0.0011, +0.0083] |

## Consequences

- E2 now leaves out 8 openers from 2018-19 to 2021-22. Five are in the test seasons. Six, from 2018-19 to 2020-21, leave B1's E2 fit for the 2021-22 fold (12,589 games to 12,583).
- The bounds were set with the training and development seasons' closes in view (no close is outside 0.21 to 0.84). They are fixed from now on and must not be tuned on a held-out season.
- The live E2 from October 2026 should apply the same rule to Pinnacle snapshots when a bet is selected.
- `tests/leakage/test_backtest.py::test_the_implausible_rule_reads_the_opener_alone` holds the rule to the opener: swapping every close changes nothing E2 reads.

## Revisit when

- A held-out season, or the live snapshots, shows a genuine market outside 0.15 to 0.85. That would mean the bounds are too tight.
- Or a later audit finds a class of opener errors that can be told from the opener alone.
