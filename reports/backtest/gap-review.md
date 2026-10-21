# Gate 1's review of B2's gaps above 8 points (#79, hard rule 8)

The review covers run `backtest-20261001-388bb85` on the development seasons 2018-19 and 2021-22: E1 first, then E2's gaps on 2026-10-21 (Codex on #93). The frozen run `backtest-20261005-b7639f5` has the same gaps. It follows the method the owner approved on 2026-10-01 (#79): an automated screen of every gap, then a hand review of every flagged game, every gap above 20 points and a seeded random sample of 40 more.

The review read only inputs and facts public before each game: its standings and goal difference from earlier games, each team's previous game, the goalie candidates, and news. **No game's own result was read,** and nothing was tuned in response (hard rule 8).

## The screen

`nhl gap-review` writes every gap game's input contributions and flags to `gap-screen.csv`, and the games to review to `gap-review-set.csv` (E2: `gap-screen-e2.csv` and `gap-review-set-e2.csv`). Each experiment is refit at its own fold starts and predicted at its own times, and the refit must reproduce the backtest's B2 for every game. B2's probability is the same in both experiments; only the market's differs, at the opener or the close.

| | E1 | E2 |
| --- | ---: | ---: |
| Gaps above 8 points | 822 | 775 |
| `history_gap`: team strength read fewer earlier games than played | 0 | 0 |
| `out_of_range`: an input more than 4 standard deviations from training, a goalie pair's ΔG included | 24 | 22 |
| `away_from_home`: a non-neutral game outside the home team's arena | 7 | 6 |
| `no_candidates`: a team without goalie candidates known before the prediction | 0 | 0 |
| `early_season`: a team with fewer than 5 games this season | 49 | 42 |
| `limited_seats`: an arena open to part of its seats | 22 | 20 |
| **Flagged (any of the above)** | **100** | **87** |
| Gaps above 20 points, not flagged | 15 | 9 |
| Random sample of the rest (seed 20261001) | 40 | 40 |
| **The review set** | **155** | **136** |
| Of it, already reviewed under E1 | | 71 |

E1's first review covered 153 games. On 2026-10-21 the screen began counting each goalie pair's ΔG (Codex on #93), which flagged two more, NYR@DET 2022-03-30 and DET@NYR 2022-04-16, and they were reviewed then. A set already reviewed is never drawn again: a later screen only adds the games it newly flags. In all, 220 games were reviewed by hand: 155 for E1 and 65 more for E2.

## What the review found

**No bug.** Every reviewed gap traces to a correct input, or to information B2 does not have:

1. **Rare but correct inputs** (the out-of-range flags). Each is a home team just back from a trip across time zones, a Global Series game, or an extreme but real pair of goalies. The previous games check out against the schedule:
   - NJD came home from Gothenburg (6 hours), DET from Anaheim, NYI from San Jose and OTT from Arizona;
   - FLA played WPG in Helsinki on 2018-11-01 (6 hours east of New Jersey).

   These values are rare for home teams, so they sit far from the training mean, but they are right.
2. **Arenas and seats are right.**
   - **Away from the home arena (7):** every one is an Islanders home game at the Nassau Coliseum in 2018-19, when they split home games with Barclays Center (`home_arenas.csv`). Travel is measured from the arena actually used.
   - **Limited seats (22):** all January and February 2022 games in Canadian arenas. They match the audited limits (#26), for example Quebec's 50% from 2022-02-21 and Ontario's caps of about 500 in late January.
3. **Information B2 does not have: injuries, trades, COVID absences.** The market prices public news, while B2 reads team strength from expected goals and goalie starts from boxscores:
   - **NJD in March 2019** (WSH@NJD 2019-03-19, +28 points; BOS@NJD 2019-03-21, +23). Taylor Hall had not played since December and had knee surgery on 2019-02-28, out for the season ([TSN](https://www.tsn.ca/devils-hall-undergoes-knee-surgery-1.1265556)). B2 rates NJD close to even with expected goals from an 80-game memory.
   - **OTT after the 2019 deadline** (TOR@OTT 2019-03-16, +22; OTT@TBL 2019-03-02, -20). Stone, Duchene and Dzingel, 41% of Ottawa's goals, were traded in February 2019 ([CBC](https://www.cbc.ca/1.5029705), [SI](https://www.si.com/nhl/2019/02/25/nhl-trade-deadline-senators-mark-stone)).
   - **MTL on 2022-01-01** (MTL@FLA, -23). Montreal played with 16 skaters and both regular goalies in COVID protocol ([NHL.com](https://nhl.com/canadiens/news/pregame-canadiens-panthers-what-you-need-to-know-329284252), [NBC Sports](https://nhl.nbcsports.com/2022/01/01/canadiens-pause-through-jan-6-home-games-postponed-through-jan-10/)).
   - **PHI in January 2022** (PIT@PHI 2022-01-06, +21). Going in, Philadelphia was 13-20 with goal difference -24 after 33 games and had lost its last 3. Couturier hadn't played since 2021-12-18, and Ellis had missed every game since 2021-12-10. These are the boxscores before the game; an earlier version of this line cited an article from after it (leakage check of #93).
4. **Expected goals against results.** In these games, the standings and goal difference from earlier games agree with the market, and B2's team strength does not:
   - CAR@WSH 2018-12-27 (-20): CAR was 15-20 with goal difference -13, yet rated well above WSH (+26) on expected goals;
   - COL@LAK 2018-11-21 (+21): LAK was 7-13 (-20), but rated above COL;
   - NJD in March 2019, as in finding 3.

   An expected-goals rating with an 80-game memory follows shot quality and last season. That is a limit of the rating, not an error in it.
5. **The early season** (49 flagged). Ratings carry last season's games until the new season's accumulate, so offseason changes (rebuilds, signings, Seattle's arrival) show only slowly. Examples: DAL@NYR 2021-10-14 and NSH@NYR 2018-10-04.
6. **Timidity everywhere.** In every category B2 sits closer to 50% than the market in most games, the calibration slope of 1.35 seen game by game. Gaps grow from 21% of near coin flips to 65% when the market is more than 20 points from 50%.

The goalie inputs were sane throughout: candidates and probabilities matched each team's recent starters, and no team lacked candidates. Uncertain starters are not over-represented among the gaps (33% of such games against 31% of the rest).

## E2's gaps (2026-10-21)

E2 scores B2 against the market at the opener. 188 of its 775 gaps are gaps only at the opener: by construction, the close (E1's B1) moved to within 8 points of B2. The 65 games of E2's review set not already reviewed were checked the same way, against facts public before each game, reading no result: 22 flagged, 4 gaps above 20 points and 39 of the random sample. **No bug.** They fall in the findings above:

- **Early season (15 flagged):** October games of both seasons, with records of 0-0 to 3-1. For example, MIN@COL 2018-10-04: B2 gives COL 0.42, the opener 0.60 and the close 0.50.
- **Out of range** (E2's two, and E1's two added games):
  - **EDM@NJD 2018-10-06** was the Global Series game at the Scandinavium in Gothenburg, a neutral site, with both teams six time zones from home.
  - **DET@PHI 2018-12-18:** Philadelphia came home from western Canada, its last game being at Vancouver on 12-15.
  - **E1's NYR@DET 2022-03-30 and DET@NYR 2022-04-16** are rare goalie pairs. Shesterkin's goals saved was +0.31, beyond the season's 99th percentile of 0.21, which fits his Vezina season. Pickard's was -0.22, against a spread of 0.08 over 2021-22. Their pair's ΔG lies beyond 4 standard deviations. A pair counts by the product of its goalies' start probabilities, so the goalie mixture moves B2 by only 0.05 and 0.09 in log-odds in the two games.
- **Arenas and seats are right:**
  - CAR@NYI 2019-01-08 was at the Nassau Coliseum;
  - the five limited-seat games are Calgary, Vancouver, Edmonton and Ottawa in January and February 2022, at the audited limits (#26). Ottawa's 2.7% is Ontario's cap of 500.
- **Expected goals against results:** the largest E2 gaps sit here, and 2021-22's Rangers are the clearest case. Their results ran far ahead of their expected goals behind Shesterkin, so B2 rates them close to their opponents:
  - NYR@ARI 2021-12-15 (+22 points);
  - NYR@NJD 2022-03-22 (+21);
  - OTT@NYR 2022-04-09 (-16).

  The same holds for Tampa Bay's record 2018-19 (MIN@TBL 2019-03-07, TBL@STL 2019-03-23) and for teams whose results ran well below their expected goals (Philadelphia and Anaheim in 2018-19, New Jersey in 2021-22).
- **Back-to-backs:** NJD@COL 2022-04-14 (-20 points) and MTL@NYR 2022-04-27 (-26) are the second night of the home team's back-to-back.
  - **MTL@NYR:** the opener was NYR -380 and MTL +300, and the close NYR -165 and MTL +150. Both sides moved together, so it isn't a swapped quote. It is one of the four large open-to-close moves of 2021-22 the data audit lists (`reports/audit/2026-10-13.md`).
  - **The close:** it lands about 9 points from B2.
- **Timidity:** the rest of the sample. B2 sits closer to 50% than the market, as in finding 6.

## Where the findings go

- **No change to B2** before gate 1. Retuning on the development seasons would spend them (ADR 0011).
- **Phase 3 (the player layer, #12):** injuries, trades and lineup changes (findings 3 and 5) are what player ratings and projected lineups add.
- **Phase 4 (#13):**
  - the timidity (finding 6) is for the blend or a per-fold recalibration;
  - the expected-goals-against-results gap (finding 4) is for the blend's attribution;
  - logged on #13.
- **Model card:** the known weaknesses gain findings 3 to 5.
