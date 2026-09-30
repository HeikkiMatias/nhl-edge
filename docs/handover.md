# Handover, 2026-09-30

Where the build stands after the cloud sessions of 2026-09-29 and 30, and how to pick it up from a terminal. CLAUDE.md holds the rules. This file holds the state. Update or delete it when it goes stale.

## State

- **Lake (R2):** every regular-season game from 2010-11 to 2025-26 (19,152), and 2026-27's as the nightly ingest adds them.
  - `games` and `schedule` (ADR 0003 and 0005), `players`
  - the per-game tables `shots`, `shifts`, `actual_lineups` and `shift_coverage` (#5, ADR 0004)
  - `odds_snapshots`, every stored Odds API snapshot from 2026-09-28 on, matched to NHL games (#20)
  - `sbr_odds`, the SBR archive's opening and closing lines from 2010-11 to 2022-11-27, matched to NHL games (#7, 133,594 prices)
  - `pregame_goalies` and `dailyfaceoff_goalies`, from the goalie polls since 2026-09-29 (#43, #48)
- **Reference files (`src/nhl_edge/reference/`, #8 and #26):** team codes, arenas, venue names, home arenas by season, head coach stints and attendance limits. `nhl audit reference` checks them against every game. Features read them through `coaches_known_at` and `capacity_share`, never the CSVs.
- **SBR archive (#7, #52, #56, ADR 0006):**
  - HTML only, and it stops on 2022-11-27, so 2022-23 has 342 of its 1,312 games (26%). 2010-11 to 2021-22 join 100%.
  - A price counts as seen from the game's start. E2 reads the opener at 10:00 US Eastern on the game date (ADR 0006), a separate column.
  - `reference/sbr_suspect_openers.csv` lists 40 openers that are likely wrong, with the evidence for each. Its `bad_close` column marks 3 of them, all in 2015-16, where the close is the error instead (#64). B1 still fits on them, by the owner's choice.
  - `nhl odds sbr --replay` rebuilds the table from the stored pages.
- **Audit (#9):** `nhl audit report` writes `reports/audit/<as-of>.md`: games per season, the SBR section, shift coverage, reference files, snapshot health and starting goalies (#63).
  - The report with data to 2026-09-29 had 28 problems. The review on #9 (comment of 2026-09-30) ties each to an issue, and no season or source is dropped.
  - New from that review: #64 (three 2015-16 closes that contradict their own puck line), #65 (SBR's closing book changes in 2018-19), #66 (2022-23 in phase 4), #67 (`EXPECTED_GAMES` for 2026-27) and #68 (2024-25's 57 empty shift charts).
  - No report is committed yet. The goalie section needs two weeks of polls first.
- **Backtest (#10):** `nhl backtest` runs the walk-forward on the development seasons 2018-19 and 2021-22 and writes `reports/backtest/summary.json`, `runs.csv` and the model card's figures. The current run is `backtest-20260930-7301709`.
  - B0 is the de-vigged market, multiplicative by default (ADR 0008, #61). Power and Shin tie with it: every paired interval includes 0.
  - B1 is a logistic recalibration of B0's log-odds, fitted per fold on the earlier seasons. It does not beat B0: B0 minus B1 is +0.0004 [-0.0005, +0.0013] on E1.
  - E1 (the close) B0 log loss 0.6571 [0.6469, 0.6668]. E2 (the opener) 0.6603 [0.6505, 0.6698]. E2 minus E1, paired: +0.0033 [+0.0005, +0.0064].
  - E2 refuses openers more extreme than every close before their fold (ADR 0007, #60): 10 openers, 7 of them scored test games. E2 on every opener is reported under `sensitivity` in summary.json (+0.0046 [+0.0011, +0.0083] against E1).
  - `diagnostics.book_era` (#65) runs the walk-forward on every season from 2011-12 and finds no sign that SBR's change of closing book in 2018-19 hurts: B0 minus B1 on E1 is -0.0007 [-0.0014, -0.0000] before it and +0.0002 [-0.0004, +0.0008] after. E2's cost against E1 (+0.0018 before, +0.0024 after, difference +0.0005 [-0.0016, +0.0027]) can't separate the book from timing. The closes stay as they are, the owner's choice.
  - The golden odds tests (#62) check vig, de-vig and an overtime game's moneyline against its regulation market on a frozen snapshot.
- **Jobs:** GitHub Actions runs the nightly ingest (09:00 UTC), five odds slots a day, and the goalie polls hourly at :50 from 12:50 to 02:50 UTC. docs/data-sources.md, "When jobs run", has the times.
- **Run times (#50):** GitHub's `schedule` starts this repo's runs 3 to 6 hours late, after most puck drops. The Cloudflare Worker `nhl-edge-timer` (`infra/timer`, #53), deployed on the owner's Cloudflare account, dispatches every job on time; `infra/timer/README.md` covers setup. After storing a new token on 2026-09-30 (it expires in about a year), the owner set the repository variable `TIMER_ACTIVE` to `true` at 09:32 UTC. The Worker's first dispatch, the morning odds slot at 11:05 UTC, ran on time and succeeded (run 36706376160).
  - While `TIMER_ACTIVE` is `true`, GitHub's own scheduled runs skip, so if the Worker fails, nothing runs at all. `gh run list --event workflow_dispatch --limit 10` shows the Worker's runs.
  - Close #50 once a scheduled run also shows its job skipped. By 12:13 UTC on 09-30 no scheduled run had started since the switch.
  - If the dispatches stop: run `gh variable set TIMER_ACTIVE --body false` so the late GitHub schedule takes over, then `npx wrangler@4 tail` in `infra/timer`. A 401 is a bad or expired token; a 403 or 404 means it lacks Actions read and write on this repository. `npx wrangler@4 secret put GITHUB_TOKEN` stores a new one.
  - In that case, start the goalie polls by hand with `gh workflow run pregame-goalies.yml --ref main` about 40 minutes before puck drop. On 2026-09-30 that is 22:50 UTC (PIT at PHI, NYI at TOR) and 01:50 UTC (LAK at COL). A poll uses free endpoints. An odds slot (`gh workflow run odds-snapshots.yml --ref main -f slot=<slot>`) spends Odds API credits, so don't start one by hand.
  - To retire the timer: set `TIMER_ACTIVE` to `false`, then `npx wrangler@4 delete`.
- **Goalie polls (#42, #43, #48):** the NHL pre-game poll and the Daily Faceoff poll run at every odds slot and hourly at :50. RotoWire is left out, because its terms forbid scraping. On the first night, 2026-09-29, the NHL's starter flag was set for no team in any poll, while Daily Faceoff already listed most starters as Confirmed (6 of 8 teams at 22:40 UTC, 4 of 4 at 01:37 UTC). Late runs left CAR at FLA and NYR at BOS with no poll in their last 80 minutes.
- **Closed in P1:** #4, #5, #6, #7, #8, #20, #24, #25, #26, #27, #29, #43, #48, #52, #56.

## In flight

- **#30's feed recheck** (phase 2, task 1): the PR adding `nhl recheck` and the audit report's corrections section. Once merged, the nightly run fetches each game's feeds again a week after the tables' copy. The first 2026-27 games are due on 2026-10-07.

## Decisions waiting for the owner

1. **2022-23 in phase 4 (#66).** The plan runs phase 4's full backtest on 2022-23, but SBR covers only 26% of it. Decide when phase 4 starts.

## Next

1. **#10, the phase 1 gate.** Every deliverable is merged: `devig.py` with its default (ADR 0008), B0 and B1 on E1 and E2, the backtest, the model card and the golden tests. What is left is the done-when's "live snapshots landing five times a day". Once the timer has run a full day of slots, check the live odds section of `nhl audit report`, then close #10.
2. **#9 and #42, the committed report.** After two weeks of polls (from 2026-09-29, so around 2026-10-13), run `nhl audit report`, commit it to `reports/audit/`, and review it as in the comment on #9: every problem gets an issue in a milestone, and a dropped season or source gets an ADR. #42 closes with the report's answer on the NHL's starter flag, #9 with the reviewed report.
3. **Phase 2, started 2026-09-30.** The owner approved the phase plan, and it is pasted into every phase 2 PR. #11 lists its ten tasks in order:
   - #30 recheck (in flight);
   - #28 strength source: ADR 0009, accepted on 2026-09-30. Shots take their skater counts from a complete shift chart, and `situationCode` otherwise;
   - #72 strength time;
   - #73 xG, with an ADR;
   - #74 team strength, with the tuning-protocol ADR;
   - #75 goalie effect;
   - #76 goalie-start model;
   - #77 schedule and home terms;
   - #78 B2 in the walk-forward;
   - #79 gate 1.
   #30 closes after two weeks of rechecked games (about 2026-10-22), once the corrections section is reviewed on #30.

## Keep an eye on

- **Missed pre-game polls can't be redone.** If the timer stops, a late poll loses that game's data for #9 and #42.
- **#21 (P5):** the closing proxy should key on the Odds API event and its commence time, not `game_id`.
- **Reference upkeep in 2026-27:** at a coaching change, end the old stint in `coaches.csv` and add the new one. Add a new venue name to `venues.csv`, and the building to `arenas.csv` if it is new. `nhl audit reference` flags both once the nightly ingest adds the game.
- **#67 (P5):** `EXPECTED_GAMES` has no 2026-27 entry, so the season's game count and the reference checks that need a finished season skip it. It matters from April 2027.

## Continuing from a terminal

1. `git pull` on `main`, then `uv sync`.
2. `uv run nhl status` compares this machine with R2. To catch up: `uv run nhl lake restore-raw`, `uv run nhl ingest --seasons 20102011-20252026 --replay`, `uv run nhl odds replay`, `uv run nhl odds sbr --replay` and `uv run nhl goalies replay`. None of them calls a paid endpoint.
3. `uv run nhl backtest` reruns the backtest in under a minute. Commit code first: the run's version string ends in `-dirty` when `src/`, `pyproject.toml` or `uv.lock` has uncommitted changes.
4. The SessionStart hook lists the open issues of the earliest milestone. CLAUDE.md's workflow (branch per issue, Codex review budget, merge when ready) applies as before.
