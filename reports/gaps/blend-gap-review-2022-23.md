# Blend gap review, 2022-23 (#145, hard rule 8)

These are the market blend's gaps above 8 points against B1 in 2022-23's one run, `market-validation-20261005-6ec331b`: 8 on E1 and 34 on E2, 35 games in all. Each experiment's gap was screened by `nhl audit gaps --blend` at its own prediction time and fold start, with B3 checked against the run's own B3 (`reports/gaps/blend-gaps-20261005-12094bd.md`). Each was then read by hand. No game's result was read, and the screen shows none.

## Verdict

- **No data error.** None of the 35 games is on #56's suspect-opener list, and no price looks swapped: every B1 probability sits between 0.28 and 0.81, and E1 and E2 agree in direction on every game gapped on both.
- **No bug in the blend, B3 or their inputs.** The screen's bug signatures stay silent: no team without candidates, no average goalie, no input outside its training range, no strength jump.
- **The gaps** come from the season's opening week and from B3's view of a handful of teams, the patterns of 2021-22's review (`reports/gaps/blend-gap-review.md`) and gate 2's (#134).

## The gaps

**Flagged: 6 games, all in the first five days (2022-10-11 to 10-15).**
- **Projection misses of 7 to 12 dressed skaters,** in five games (2022020004, 006, 007, 011 and 033). Summer moves show in the boxscores only once teams have played (#120).
- **Starter surprises** at a start probability of 0.00 to 0.13, in four games (006, 007, 011 and 025). Goalies who changed teams over the summer aren't yet among their new team's candidates.

  The data is correct in each: the model saw what the earlier boxscores implied.

**Unflagged: 29 games.**
- **Δĝ drives 39 of the 42 rows.** B3 is closer to 50% than the market in 21% of the rows, and picks the other favourite in 12%.
- **A few teams recur:**
  - The blend rates Montreal (9 rows), Columbus (7), Arizona (6) and Buffalo (5) below the market in every row.
  - It rates Nashville (6) and Washington (5) above the market in every row.

  B3's ratings drive each through Δĝ. Their inputs are in range, and no flag fires.
- **A back-to-back term drives 3 rows:** 2022020169, 212 and 269. These are the large schedule terms of #13 (gate 2's finding 2).

**Nothing tunes on these games.** The review checked inputs, not who won, and no setting was changed for it.
