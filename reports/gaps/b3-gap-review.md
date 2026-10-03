# B3 gap review (gate 2, #107, hard rule 8)

## Scope

This is the manual review of B3's gaps above 8 points against B1 at the close (E1). It covers 411 of 2,583 games in `backtest-20261003-b1a7b04`.

The method is the one the owner chose on 2026-10-03, #79's proposal for B2:
- an automated screen for bug signatures (`nhl audit gaps`, report `b3-gaps-20261003-a520271.md`);
- a manual review of every flagged game, every gap above 20 points, and a seeded sample of 40 more, split by season.

Each game was checked against the facts its own and later boxscores record: who dressed, who started in goal, where each player played before and after. No result was read, and B3 is not tuned to shrink a gap.

**Result: no data error.** Every gap reviewed traces to a known limit of B3's inputs or to a real disagreement with the market. Nothing needs fixing, and the backtest stands as run.

## What the gaps look like

All 411, from the screen, inputs only:
- **B3 is closer to 50% than the market** in 81% of gaps, and picks the other favourite in 37%.
- **The largest term in B3's log-odds is Δĝ** in 313 gaps, the away team's back-to-back in 55, the home team's in 27, and a time-zone change in 16.
- **Timing:** 29 gaps fall in a season's first two weeks (16 of them flagged). 49 fall in 2021-22's COVID winter, 15 December 2021 to 31 January 2022 (10 of them flagged).

## Flags

| Flag | Games | What it found |
| --- | --- | --- |
| projection_miss | 29 | Opening weeks (summer moves not yet in a boxscore), returns from injury and COVID protocols, and rested regulars at season's end |
| starter_surprise | 32 | A starter under 10%: summer signings and trades, goalies back from injury or protocol, call-ups |
| jump | 2 | The Rangers' strength settling in 2021-22's first week, and Florida's after a protocol-depleted game |
| no_candidates, average_goalie | 1 each | Seattle's first game: no history, so it plays replacements and an average goalie |
| out_of_range | 0 | |

## The games reviewed

### Flagged (51) and above 20 points (1)

| Game | Date | Teams | Gap | Finding |
| --- | --- | --- | --- | --- |
| 2018020005 | 2018-10-04 | BUF v BOS | +0.082 | Opening week. Summer arrivals (Skinner, Sobotka, Berglund, Sheary, Dahlin) are not candidates; O'Reilly, traded in July, is still projected for BUF. Hutton, a summer signing, started. |
| 2018020006 | 2018-10-04 | NYR v NSH | +0.096 | Opening week. Eight NYR skaters unprojected: summer moves and returns from injury. |
| 2018020008 | 2018-10-04 | CAR v NYI | -0.094 | Opening week. Hamilton, Ferland and de Haan (summer trades) unprojected, Skinner still projected for CAR. Mrazek, a summer signing, started. |
| 2018020014 | 2018-10-04 | VGK v PHI | -0.100 | Opening week. Stastny, Pacioretty and Holden (summer moves) unprojected. |
| 2018020020 | 2018-10-06 | NJD v EDM | +0.103 | Played in Gothenburg. h_s is 0 at a neutral site, but the time-zone terms (+0.35 home, -0.14 away) drive the gap. Kinkaid started at 0.10. |
| 2018020095 | 2018-10-18 | CHI v ARI | -0.084 | Crawford's first start after ten months out injured: not a candidate. |
| 2018020122 | 2018-10-23 | NYR v FLA | +0.127 | Georgiev started at 0.01. |
| 2018020164 | 2018-10-28 | ANA v SJS | +0.153 | Anaheim's injury returns (Silfverberg, Larsson) and the home time-zone term. |
| 2018020263 | 2018-11-12 | ANA v NSH | +0.114 | Anaheim's returns from injury (Kase) and call-ups. |
| 2018020636 | 2019-01-05 | PHI v CGY | +0.103 | Rittich started at 0.10. |
| 2018020731 | 2019-01-17 | MIN v ANA | -0.088 | A trade (Grant from PIT) and Anaheim call-ups. |
| 2018020797 | 2019-02-02 | WPG v ANA | -0.108 | Anaheim's returns from long injuries (Perry, Eaves). |
| 2018020980 | 2019-02-27 | NJD v CGY | +0.205 | New Jersey rated closer to Calgary than the market rates it, plus Calgary's back-to-back. Vatanen back from injury. |
| 2018020983 | 2019-02-27 | ANA v CHI | +0.099 | Miller (0.05) back in Anaheim's net; Crawford back from injury for CHI. |
| 2018020997 | 2019-03-01 | ANA v VGK | +0.165 | Gibson back from injury at 0.03. |
| 2018021150 | 2019-03-23 | NJD v ARI | +0.126 | New Jersey's returns from injury (Hischier and Vatanen at 0.03). |
| 2018021185 | 2019-03-27 | PHI v TOR | +0.102 | Hart back from injury at 0.07. |
| 2018021241 | 2019-04-03 | ANA v CGY | +0.154 | Season's end: Calgary rested Monahan, Bennett and Lindholm; Anaheim's regulars returned. |
| 2018021250 | 2019-04-04 | MIN v BOS | -0.096 | Season's end: Boston rested Marchand, Krejci, Chara and McAvoy. |
| 2021020001 | 2021-10-12 | TBL v PIT | -0.159 | Opening night. Summer moves and Kucherov's return from a season out. Pittsburgh's candidates are the call-ups who closed 2020-21 (D'Orio 0.45, Lagace 0.40); Jarry started at 0.10. |
| 2021020002 | 2021-10-12 | VGK v SEA | -0.118 | Seattle's first game: no candidates, 18 replacements rated 0, an average goalie. |
| 2021020005 | 2021-10-13 | COL v CHI | +0.091 | Opening week. Kuemper and Fleury, both traded in the summer, started without being candidates. |
| 2021020007 | 2021-10-14 | BUF v MTL | +0.139 | Opening week. Anderson, a summer signing, started; Montreal on a back-to-back (+0.149). |
| 2021020010 | 2021-10-14 | FLA v PIT | -0.091 | Opening week. Reinhart and Thornton (summer moves) unprojected; Bobrovsky started at 0.05. |
| 2021020011 | 2021-10-14 | NYR v DAL | -0.082 | Opening week. Holtby, a summer signing, started; NYR's home back-to-back (-0.209); NYR's strength still settling. |
| 2021020014 | 2021-10-14 | NSH v SEA | -0.083 | Opening week. Rinne, retired in the summer, is still a candidate (0.41); Saros started at 0.14. |
| 2021020017 | 2021-10-15 | NJD v CHI | -0.089 | Opening week. Hamilton, Tatar and Graves (summer moves) unprojected; Bernier, a summer signing, started. |
| 2021020041 | 2021-10-19 | DET v CBJ | -0.092 | Korpisalo started at 0.10. |
| 2021020043 | 2021-10-19 | NJD v SEA | -0.098 | Daccord, a call-up, started for Seattle; Seattle on a back-to-back. |
| 2021020045 | 2021-10-19 | WSH v COL | +0.095 | Samsonov started at 0.10; MacKinnon back from protocol. |
| 2021020227 | 2021-11-14 | WSH v PIT | +0.089 | Crosby and Dumoulin back from injury and protocol (0.09, 0.11); Vanecek started at 0.10. |
| 2021020242 | 2021-11-16 | VGK v CAR | +0.119 | Raanta started at 0.01. |
| 2021020399 | 2021-12-08 | NYR v COL | +0.089 | Kuemper back from injury at 0.06; NYR's home back-to-back. |
| 2021020468 | 2021-12-17 | STL v DAL | +0.090 | Dallas's returns from protocol (Radulov, Hintz). |
| 2021020552 | 2021-12-30 | FLA v TBL | -0.093 | COVID winter: Lagace started with Vasilevskiy out; Florida's strength back after a depleted game; Florida's home back-to-back. |
| 2021020554 | 2021-12-30 | CAR v MTL | -0.092 | COVID winter: six Carolina regulars back from protocol at about 0.2 each. |
| 2021020576 | 2022-01-02 | NYR v TBL | +0.120 | COVID winter: Vasilevskiy back at 0.06. |
| 2021020580 | 2022-01-02 | COL v ANA | -0.099 | COVID winter: Makar, Landeskog, Toews and others back from protocol; Kuemper at 0.12. |
| 2021020590 | 2022-01-04 | CBJ v TBL | +0.089 | Korpisalo started at 0.01. |
| 2021020596 | 2022-01-05 | TOR v EDM | -0.110 | COVID winter: McDavid projected but out; Smith back at 0.03. |
| 2021020608 | 2022-01-06 | VGK v NYR | -0.083 | Lehner and Stone back (0.08, 0.14). |
| 2021020620 | 2022-01-08 | MIN v WSH | +0.099 | COVID winter: Kaprizov and Brodin projected but out. |
| 2021020701 | 2022-01-19 | NJD v ARI | -0.107 | Returns from protocol on both teams. |
| 2021020824 | 2022-02-25 | CHI v NJD | +0.081 | Lankinen back from injury: not a candidate. |
| 2021020830 | 2022-02-26 | OTT v MTL | -0.087 | Murray back at 0.04. |
| 2021020936 | 2022-03-12 | CAR v PHI | -0.110 | Andersen back from injury at 0.09. |
| 2021020981 | 2022-03-18 | ANA v FLA | +0.098 | Knight started at 0.09. |
| 2021021219 | 2022-04-18 | VGK v NJD | -0.109 | Lehner back at 0.08. |
| 2021021274 | 2022-04-26 | OTT v NJD | -0.092 | Blackwood back from injury: not a candidate. |
| 2021021301 | 2022-04-29 | NJD v DET | -0.089 | Blackwood back at 0.08; New Jersey's home back-to-back. |
| 2021021308 | 2022-04-29 | MIN v COL | -0.142 | Season's end: Colorado rested MacKinnon, Makar, Toews, Nichushkin and Byram. |
| 2018021042 | 2019-03-07 | LAK v STL | +0.204 | Above 20 points, not flagged. Los Angeles rated closer to St. Louis than the market rates it, plus St. Louis's back-to-back. |

### The seeded sample (40)

- **Clean:** no input anomaly in any of the 40.
- **Shape:** B3 is closer to 50% in 33 of them (82%), and its largest term is Δĝ in 29, a back-to-back in 9 and a time zone in 2.
- **Teams:** they are mostly weak teams that B3 rates closer to average than the market does:
  - Seattle 2021-22 (8 games), whose skaters' expected-goal ratings were sound but whose goaltending and finishing were not;
  - Arizona 2021-22 (7);
  - New Jersey (4) and Anaheim (4) in 2018-19.

## Known weaknesses recorded

Each of these goes to the model card. None is fixed by retuning B3 on these seasons.

1. **B3 rates teams closer together than the market**, most for weak teams. Its calibration slope is 1.34 [1.11, 1.58] on the development seasons, but 0.94 [0.80, 1.08] on 2023-24 and 2024-25. Phase 4's blend corrects what remains, out of sample.
2. **The boxscores are slow to show news:**
   - summer signings and trades at the start of each season (#120);
   - returns from injury and from COVID protocols;
   - rested regulars at season's end;
   - goalies who are new, recalled or back from injury.

   Hard rule 9 keeps the backtest to earlier boxscores. Live, the goalie polls (#9, #42) and an injury feed (#121) can cover this.
3. **A replacement skater is rated 0,** RAPM's reference skater, not a replacement-level one (ADR 0018, ADR 0023). An expansion team's first game and short lineups come out about average.
4. **B2's schedule terms are large and shared with B3:**
   - a home back-to-back is -0.21 in the 2021-22 fold;
   - the time-zone terms reach +0.35 in a game played in Europe.

## Follow-ups

- **#120:** a comment with the opening-week evidence from this review.
- **#134:** rating replacement skaters below the reference skater, for the owner to decide (it changes ADR 0018 and ADR 0023).
- **#13, phase 4's findings log:** a comment on the schedule terms' size and B3's under-confidence on the development seasons.
