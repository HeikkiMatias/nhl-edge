# Blend gap review, 2021-22 (#144, hard rule 8)

These are the market blend's gaps above 8 points against B1 in `backtest-20261004-6f74840`: 17 on E1 and 49 on E2, 53 games in all. The screen read the same 53 games from `backtest-20261004-509d3da`, whose code and gaps are the same. Each was screened by `nhl audit gaps --blend` (`reports/gaps/blend-gaps-20261004-509d3da.md`), with B3 refit as in the backtest's E1 fold, and read by hand. No game's result was read, and the screen shows none.

**Re-screened at each gap's own prediction time** (Codex, PR 155):
- **What was wrong:** the first screen refit B3 at each game's start, even for E2's gaps, which the backtest predicted at the opener. It also checked its refit against itself.
- **The re-screen:** `backtest-20261005-b7639f5` reproduces `backtest-20261004-6f74840`, with every figure identical. Its `gaps_blend.csv` carries each gap's prediction time and the backtest's own B3.
  - `nhl audit gaps --blend` refit B3 for each experiment's gap at that time and at the experiment's fold start, and checked it against the backtest's B3 (`reports/gaps/blend-gaps-20261005-ebb7710.md`).
  - That covers all 66 rows: 49 at E2's opener time and 17 at E1's start. A game that is a gap on both experiments is screened twice.
  - Every term and flag matches the first screen for its game, across 59 columns. The same 18 games are flagged.
- **So the review below stands as written.**

## Verdict

- **One data error:** game 2021020487 (ARI at VAN, 2022-02-08), a swapped SBR opener. The policy bet on it, and since the error lies in the price, not the model, the policy is unchanged (below).
- **No bug in the blend, B3 or their inputs.**
- **The other 52 gaps** come from lineup news the boxscores carry late, goalie surprises, and B3's known view of weak and new teams, the patterns gate 1's and gate 2's reviews found.

## The data error

Game 2021020487, ARI at VAN on 2022-02-08, rescheduled from December:
- **The prices:** SBR's opener has Arizona −310 and Vancouver +250, a 27% home probability. Its close has Vancouver −220 and Arizona +200 (67%), and the closing puck line agrees with the close. The opener has its sides swapped.
- **Why E2 kept it:** #56's suspect-opener list flags the game (`big_move`). That flag reads the close, which E2 can't see, so ADR 0007 keeps the opener: a 27% home probability is within the fold's bounds of 0.211 to 0.840.
- **What the policy did:** the blend (B3 66%) saw a 72% expected return at 3.50 and bet Vancouver: 2.3 units, the only bet on a listed suspect opener.
- **Why nothing changes:** live bets use Pinnacle's prices, not SBR's. Refusing the opener on the close's evidence would choose E2's sample with information the bet lacks (ADR 0007).
- **The sensitivity:** the backtest now reports E3 without bets on #56's list, beside the policy's own figures:

| E3, 2021-22 | Every bet (465) | Without the listed opener (464) |
| --- | --- | --- |
| CLV per bet | −1.24% [−2.15%, −0.24%] | −1.53% [−2.25%, −0.79%] |
| Fair move | +2.82% [+1.90%, +3.85%] | +2.51% [+1.79%, +3.26%] |
| Return per unit staked | +6.1% [−0.5%, +13.3%] | +5.6% [−1.0%, +12.8%] |

## The other gaps

**Flagged by the screen: 17 games besides the data error.**
- **The opening fortnight: 9 games** (2021-10-13 to 10-24: 2021020003, 005, 007, 009, 014, 017, 020, 041 and 080). Most have projection misses of 7 to 11 skaters and goalie surprises. Summer roster moves show in the boxscores only once teams have played, as gate 2's review found (#120).
- **Late December and January 2022, COVID protocols: 5 games.**
  - WSH vs LAK on 2021-12-19 had a starter outside the candidates.
  - DET vs WSH on 2021-12-31 had 4 and 2 skaters missed.
  - BOS vs MTL on 2022-01-12 had 9 Montreal skaters missed.
  - BOS vs PHI and CHI vs MTL on 2022-01-13 had 4 skaters missed each.
- **Goalie surprises:** LAK vs FLA on 2022-03-13, where Florida's starter had a 2% start probability.
- **Season's end:**
  - ARI vs SEA on 2022-03-22, with 4 Seattle skaters missed.
  - VAN vs LAK on 2022-04-28, with 7 Los Angeles skaters missed and a strength jump (rested regulars).
  - MIN vs COL on 2022-04-29, with 5 Colorado skaters missed.

  The data is correct in each: the model saw the lineup the boxscores implied.

**Unflagged: 35 games.**
- **Δĝ drives most of them,** and B3 is closer to 50% than the market in about a third.
- **Seattle appears in 14,** mostly where B3 rates the expansion team closer to average than the market does (gate 2's review, #134).
- **Arizona, Buffalo, Detroit and Ottawa** recur where the market is more pessimistic or more optimistic than B3's ratings. Their inputs are in range, and no flag fires.
- **A back-to-back term drives 7 games,** the large schedule terms of #13 (gate 2's finding 2).

**Nothing tunes on these games.** The review checked inputs, not who won, and no setting was changed for it.
