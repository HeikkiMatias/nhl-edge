# Gate 1's review of B2's gaps above 8 points (#79, hard rule 8)

The review covers run `backtest-20261001-388bb85`, E1, the development seasons 2018-19 and 2021-22. It follows the method the owner approved on 2026-10-01 (#79): an automated screen of every gap, then a hand review of every flagged game, every gap above 20 points and a seeded random sample of 40 more.

The review read only inputs and facts public before each game: its standings and goal difference from earlier games, each team's previous game, the goalie candidates, and news. **No game's own result was read,** and nothing was tuned in response (hard rule 8).

## The screen

`nhl gap-review` writes every gap game's input contributions and flags to `gap-screen.csv`, and the games to review to `gap-review-set.csv`.

| | Games |
| --- | ---: |
| Gaps above 8 points (E1) | 822 |
| `history_gap`: team strength read fewer earlier games than played | 0 |
| `out_of_range`: an input more than 4 standard deviations from training | 22 |
| `away_from_home`: a non-neutral game outside the home team's arena | 7 |
| `no_candidates`: a team without goalie candidates | 0 |
| `early_season`: a team with fewer than 5 games this season | 49 |
| `limited_seats`: an arena open to part of its seats | 22 |
| **Flagged (any of the above)** | **98** |
| Gaps above 20 points (15 of them not flagged) | 19 |
| Random sample of the rest (seed 20261001) | 40 |
| **Reviewed by hand** | **153** |

## What the review found

**No bug.** Every reviewed gap traces to a correct input, or to information B2 does not have:

1. **Rare but correct inputs** (the 22 out-of-range flags). Each is a home team just back from a trip across time zones, or a Global Series game. The previous games check out against the schedule:
   - NJD came home from Gothenburg (6 hours), DET from Anaheim, NYI from San Jose and OTT from Arizona;
   - FLA played WPG in Helsinki on 2018-11-01 (6 hours east of New Jersey).

   These values are rare for home teams, so they sit far from the training mean, but they are right.
2. **Arenas and seats are right.**
   - **Away from the home arena (7):** every one is an Islanders home game at the Nassau Coliseum in 2018-19, when they split home games with Barclays Center (`home_arenas.csv`). Travel is measured from the arena actually used.
   - **Limited seats (22):** all January and February 2022 games in Canadian arenas. They match the audited limits (#26), for example Quebec's 50% from 2022-02-21 and Ontario's caps of about 500 in late January.
3. **Information B2 does not have: injuries, trades, COVID absences.** The market prices public news, while B2 reads team strength from expected goals and goalie starts from boxscores:
   - **NJD in March 2019** (WSH@NJD 2019-03-19, +28 points; BOS@NJD 2019-03-21, +23). Taylor Hall had not played since December and had knee surgery on 2019-02-28, missing the final 47 games ([TSN](https://www.tsn.ca/devils-hall-undergoes-knee-surgery-1.1265556)). B2 rates NJD close to even with expected goals from an 80-game memory.
   - **OTT after the 2019 deadline** (TOR@OTT 2019-03-16, +22; OTT@TBL 2019-03-02, -20). Stone, Duchene and Dzingel, 41% of Ottawa's goals, were traded in February 2019 ([CBC](https://www.cbc.ca/1.5029705), [SI](https://www.si.com/nhl/2019/02/25/nhl-trade-deadline-senators-mark-stone)).
   - **MTL on 2022-01-01** (MTL@FLA, -23). Montreal played with 16 skaters and both regular goalies in COVID protocol ([NHL.com](https://nhl.com/canadiens/news/pregame-canadiens-panthers-what-you-need-to-know-329284252), [NBC Sports](https://nhl.nbcsports.com/2022/01/01/canadiens-pause-through-jan-6-home-games-postponed-through-jan-10/)).
   - **PHI in January 2022** (PIT@PHI 2022-01-06, +21). Philadelphia was in an 11-game losing streak without Couturier, Ellis, Hayes and Farabee ([Inquirer](https://www.inquirer.com/flyers/flyers-injuries-losing-streak-last-place-midway-point-20220123.html)).
4. **Expected goals against results.** In these games, the standings and goal difference from earlier games agree with the market, and B2's team strength does not:
   - CAR@WSH 2018-12-27 (-20): CAR was 15-20 with goal difference -13, yet rated well above WSH (+26) on expected goals;
   - COL@LAK 2018-11-21 (+21): LAK was 7-13 (-20), but rated above COL;
   - NJD in March 2019, as in finding 3.

   An expected-goals rating with an 80-game memory follows shot quality and last season. That is a limit of the rating, not an error in it.
5. **The early season** (49 flagged). Ratings carry last season's games until the new season's accumulate, so offseason changes (rebuilds, signings, Seattle's arrival) show only slowly. Examples: DAL@NYR 2021-10-14 and NSH@NYR 2018-10-04.
6. **Timidity everywhere.** In every category B2 sits closer to 50% than the market in most games, the calibration slope of 1.35 seen game by game. Gaps grow from 21% of near coin flips to 65% when the market is more than 20 points from 50%.

The goalie inputs were sane throughout: candidates and probabilities matched each team's recent starters, and no team lacked candidates. Uncertain starters are not over-represented among the gaps (33% of such games against 31% of the rest).

## Where the findings go

- **No change to B2** before gate 1. Retuning on the development seasons would spend them (ADR 0011).
- **Phase 3 (the player layer, #12):** injuries, trades and lineup changes (findings 3 and 5) are what player ratings and projected lineups add.
- **Phase 4 (#13):**
  - the timidity (finding 6) is for the blend or a per-fold recalibration;
  - the expected-goals-against-results gap (finding 4) is for the blend's attribution;
  - logged on #13.
- **Model card:** the known weaknesses gain findings 3 to 5.
