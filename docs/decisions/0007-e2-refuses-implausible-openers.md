# 0007. E2 refuses implausible SBR openers

- Status: Accepted (by the owner, 2026-09-30, with bounds per fold and no tuned step)
- Date: 2026-09-30

## Context

#56 lists 40 SBR openers of 2010-11 to 2021-22 that are likely wrong, with four flags:
- **extreme_open:** the de-vigged home probability is outside 0.15 to 0.85. There are 8, such as Edmonton -1010 against Minnesota 705 on 2022-02-20, which SBR's own page prints that way.
- **swapped:** the opener names the other favourite, and swapping its sides lands within 5 points of the close. There are 15.
- **below_100:** the two prices sum below 100%. There are 3.
- **big_move:** the opener moves more than 15 points to the close. There are 33.

E2 bets at the opener. A wrong opener makes E2's result partly a result on typos. On 2026-09-29 the owner chose to drop all 40. Codex then rated that a P0 under hard rule 1 on PR #59: big_move and swapped compare the opener with the close, which is public only at the start. Dropping those games would pick E2's sample with information its prediction lacks, and flatter E2.

On PR #60 Codex rated the fixed 0.15 and 0.85 a P0 too. #56 chose them with every close up to 2021-22 in view, so closes from after each fold's start decided which games that fold scored and fitted on. A first fix widened each fold's range of earlier closes outward to a multiple of 0.05. Codex rated that step a P0 as well, since it was chosen on the development seasons to keep two borderline openers in.

## Options

1. **Keep every opener.** This is point-in-time, but E2 then scores the 8 impossible prices as if a book had offered them.
2. **Drop all 40, or the 26 with a hard flag.** This picks E2's sample with the close (hard rule 1). For the development seasons it cuts E2's cost against E1 to +0.0026, partly by dropping real news moves.
3. **Refuse the openers outside fixed bounds, 0.15 to 0.85.** The rule reads the opener alone, but the bounds were chosen with later closes in view.
4. **Refuse the openers more extreme than every close before the fold, widened to a step of 0.05.** The step was chosen on the development seasons.
5. **Refuse the openers more extreme than every close before the fold.** The bounds come from the closes public before the fold starts, and nothing is tuned, so each fold uses only what it could have known. A live E2 can apply the same rule with the closes it has.

## Decision

Option 5. The owner chose on the decision card of 2026-09-30 to refuse only the implausible openers, asked for bounds per fold after Codex's first P0 on #60, and dropped the step after its second.

For each fold, `walk_forward.bounds` takes the de-vigged home probability (B1's method) of every SBR close public before the fold starts. The bounds are the lowest and highest of these. E2 refuses an opener outside them (`walk_forward.implausible`), in the fold's test season and in the fold's B1 fit. Each fold starts from every opener, so its fit never depends on which other seasons a run requests. Coverage counts a refused test game as `implausible`. Openers below 100% are refused by de-vigging, as before. E1 reads the close and is unchanged. Every backtest also reports E2 on every opener, under `sensitivity` in summary.json.

On the development seasons the bounds are 0.236 to 0.837 for the 2018-19 fold and 0.228 to 0.837 for the 2021-22 fold. E2 refuses 10 openers in all: the 8 of #56's `extreme_open` flag, at 0.089, 0.112 and 0.862 to 0.919, and two just past the bounds, at 0.231 in 2018-19 and 0.228 in 2021-22.

## Backtest evidence

Run `backtest-20260930-a18ef13` in `reports/backtest/runs.csv`, on 2018-19 and 2021-22, with 95% weekly block bootstrap intervals. E2 refuses 7 scored test games.

| E2, B0 multiplicative | Log loss | E2 − E1 |
| --- | --- | --- |
| Refusing implausible openers | 0.6603 [0.6505, 0.6698] | +0.0033 [+0.0005, +0.0064] |
| Every opener | 0.6617 [0.6512, 0.6716] | +0.0046 [+0.0011, +0.0083] |

## Consequences

- E2 leaves out 7 scored test games: 4 in 2018-19 and 3 in 2021-22. Six earlier openers, from 2018-19 to 2020-21, leave B1's E2 fit for the 2021-22 fold (12,589 games to 12,583). The 2018-19 opener at 0.231 stays in that fit, since it is inside that fold's bounds.
- The rule has no tuned constant. The bounds widen once a close goes past every earlier close, so a genuine market that extreme is refused only until such a close exists before a later fold, or, live, before a later day.
- The live E2 from October 2026 should apply the same rule to Pinnacle snapshots, with bounds from every close before the day.
- `tests/leakage/test_backtest.py::test_the_implausible_rule_reads_no_close_from_the_fold` holds the rule to earlier closes. Changing the test season's closes or the results changes nothing E2 refuses.

## Revisit when

- A held-out season, or the live snapshots, shows genuine markets refused often. That would mean the bounds are too tight.
- Or a later audit finds a class of opener errors that can be told from the opener and earlier closes alone.
