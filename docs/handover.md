# Handover, 2026-10-01

Where the build stands after the cloud sessions of 2026-09-29 to 10-01, and how to pick it up from a terminal. CLAUDE.md holds the rules. This file holds the state. Update or delete it when it goes stale.

## For the owner

- Nothing is waiting on you right now. The open decision on 2022-23 (#66) comes up when phase 4 starts.

## State

- **Lake (R2):** every regular-season game from 2010-11 to 2025-26 (19,152), and 2026-27's as the nightly ingest adds them.
  - `games` and `schedule` (ADR 0003 and 0005), `players`
  - the per-game tables `shots`, `shifts`, `actual_lineups`, `shift_coverage` and `strength_time` (#5, #72, ADR 0004)
    - `shots` takes its skater counts from a complete shift chart, and `situationCode` otherwise (#28, ADR 0009); `strength_source` says which.
    - `shots` carries the play before each shot (`prev_` columns, #73).
    - `strength_time` gives each team's seconds at each strength state. Every game's seconds add up but 7 old games' (2011-12 to 2015-16), which the audit report lists.
  - `shot_xg`, every unblocked shot's xG from 2011-12 on (#73, ADR 0010), from one model per season fitted on earlier seasons
  - `team_strength`, every game's rolling team strength ΔS from 2011-12 on (#74, ADR 0011), with frozen settings: a half-life of 80 games and a pull worth 40 games, tuned on the training seasons. Ratings of 2011-12 to 2017-18 count as known only from the tuning cutoff, 2018-04-09.
  - `goalie_starts`, each candidate goalie's probability of starting every team-game from 2011-12 on (#76, ADR 0012), from one model per season fitted on earlier boxscores
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
- **Run times (#50, closed):** GitHub's `schedule` starts this repo's runs 3 to 6 hours late. The Cloudflare Worker `nhl-edge-timer` (`infra/timer`, #53) dispatches every job on time; `infra/timer/README.md` covers setup. The repository variable `TIMER_ACTIVE` is `true`, set by the owner around 18:50 UTC on 2026-09-30 (before that it did not exist, so GitHub's late runs ran too).
  - From then on GitHub's scheduled runs skip, and the Worker's run on time: for example, runs 36776626369 and 36805609955 were skipped, and pre7 run 36787373903 at 22:45 UTC succeeded.
  - If the Worker fails, nothing runs at all. `gh run list --event workflow_dispatch --limit 10` shows its runs.
  - If the dispatches stop: run `gh variable set TIMER_ACTIVE --body false` so the late GitHub schedule takes over, then `npx wrangler@4 tail` in `infra/timer`.
    - A 401 is a bad or expired token, and the token expires around 2027-09.
    - A 403 or 404 means the token lacks Actions read and write on this repository.
    - `npx wrangler@4 secret put GITHUB_TOKEN` stores a new token.
  - While the timer is down, start the goalie polls by hand with `gh workflow run pregame-goalies.yml --ref main` about 40 minutes before puck drop. Don't start an odds slot by hand: it spends Odds API credits.
  - To retire the timer: set `TIMER_ACTIVE` to `false`, then `npx wrangler@4 delete`.
- **Odds snapshots:** a book's market priced at 1.0 (GTbets on 2026-09-30) used to fail the whole snapshot. Now the market is skipped and the raw copy keeps it (#83, #84).
- **Goalie polls (#42, #43, #48):** the NHL pre-game poll and the Daily Faceoff poll run at every odds slot and hourly at :50. RotoWire is left out, because its terms forbid scraping. On the first night, 2026-09-29, the NHL's starter flag was set for no team in any poll, while Daily Faceoff already listed most starters as Confirmed (6 of 8 teams at 22:40 UTC, 4 of 4 at 01:37 UTC). Late runs left CAR at FLA and NYR at BOS with no poll in their last 80 minutes.
- **Closed in P1:** #4, #5, #6, #7, #8, #20, #24, #25, #26, #27, #29, #43, #48, #50, #52, #56, #64, #65, #83.
- **Closed in P2:** #28, #72, #73, #74, #76.

## In flight

- **#75, the goalie effect ΔG** (phase 2, task 6), the PR on `phase-2/goalie-effect`:
  - It adds `features/goalie.py`, `nhl goalie-effect` (`--tune`) and the `goalie_effects` table: each goalie-start candidate's goals saved above expected per shot, times the shots his team is expected to allow.
  - Tuned per ADR 0011 by the ΔG expected under the goalie-start probabilities. Thirteen of 16 settings tied. The rule took the steadiest, a half-life of 160 goalie games and a pull worth 4,000 shots, on the grid's corner and tying by about 2e-6. The owner chose to follow the rule.
  - After merge: `nhl goalie-effect --r2`.
- **#30's recheck** runs nightly; the first 2026-27 games are due on 2026-10-07.

## Decisions waiting for the owner

1. **2022-23 in phase 4 (#66).** The plan runs phase 4's full backtest on 2022-23, but SBR covers only 26% of it. Decide when phase 4 starts.

## Next

1. **#10, the phase 1 gate.** Every deliverable is merged: `devig.py` with its default (ADR 0008), B0 and B1 on E1 and E2, the backtest, the model card and the golden tests. What is left is the done-when's "live snapshots landing five times a day". Once the timer has run a full day of slots, check the live odds section of `nhl audit report`, then close #10.
2. **#9 and #42, the committed report.** After two weeks of polls (from 2026-09-29, so around 2026-10-13), run `nhl audit report`, commit it to `reports/audit/`, and review it as in the comment on #9: every problem gets an issue in a milestone, and a dropped season or source gets an ADR. #42 closes with the report's answer on the NHL's starter flag, #9 with the reviewed report.
3. **Phase 2, started 2026-09-30.** The owner approved the phase plan, and it is pasted into every phase 2 PR. #11 lists its ten tasks in order:
   - #30 recheck (merged as #80; closes after two weeks of rechecked games, about 2026-10-22, once the corrections section is reviewed);
   - #28 strength source (ADR 0009), #72 strength time and #73 xG (ADR 0010): done;
   - #74 team strength, with the tuning protocol (ADR 0011): done;
   - #76 goalie-start model (ADR 0012): done. It came before #75, whose tuning uses its probabilities;
   - #75 goalie effect, tuned per ADR 0011: in review;
   - #77 schedule and home terms. Their settings follow ADR 0011;
   - #78 B2 in the walk-forward. B2's L2 strength follows ADR 0011 too;
   - #79 gate 1.

## Keep an eye on

- **Missed pre-game polls can't be redone.** If the timer stops, a late poll loses that game's data for #9 and #42.
- **In-sample training seasons:** xG of 2012-13 to 2017-18 is in-sample for the rebound term per season (ADR 0010), and team strength and goalie effects there for their tuned settings (ADR 0011). Both may train later models, but no result may present them as out-of-sample. Every fold from 2018-19 on is clean.
- **Later tunings reuse earlier frozen settings:** the goalie effect was tuned on 2012-13 to 2017-18 with team strength's frozen settings in its expected shots, though those were chosen on the same seasons. Codex raised it as a P0 on #88, and the owner kept it as ADR 0011 intends: one choice per component, later ones building on earlier ones. B2's L2 strength (#78) will be tuned the same way, on ΔS and ΔG.
- **xG's known misses (ADR 0010):** 3v3 overtime is under-predicted by about 1.6 goals per 100 shots, and shots from 0 to 10 feet are over-predicted by about 0.6.
- **Merged branches left on GitHub:** `phase-1/odds-placeholder-prices`, `phase-2/xg` and earlier ones. The session's git proxy refuses branch deletes. Delete them from GitHub's branch page if you like.
- **#21 (P5):** the closing proxy should key on the Odds API event and its commence time, not `game_id`.
- **Reference upkeep in 2026-27:** at a coaching change, end the old stint in `coaches.csv` and add the new one. Add a new venue name to `venues.csv`, and the building to `arenas.csv` if it is new. `nhl audit reference` flags both once the nightly ingest adds the game.
- **#67 (P5):** `EXPECTED_GAMES` has no 2026-27 entry, so the season's game count and the reference checks that need a finished season skip it. It matters from April 2027.

## Continuing from a terminal

1. `git pull` on `main`, then `uv sync`.
2. `uv run nhl status` compares this machine with R2. To catch up: `uv run nhl lake restore-raw`, `uv run nhl ingest --seasons 20102011-20252026 --replay`, `uv run nhl odds replay`, `uv run nhl odds sbr --replay`, `uv run nhl goalies replay`, then `uv run nhl xg`, `uv run nhl team-strength`, `uv run nhl goalie-start` and `uv run nhl goalie-effect`. None of them calls a paid endpoint.
3. `uv run nhl backtest` reruns the backtest in under a minute. Commit code first: the run's version string ends in `-dirty` when `src/`, `pyproject.toml` or `uv.lock` has uncommitted changes.
4. The SessionStart hook lists the open issues of the earliest milestone. CLAUDE.md's workflow (branch per issue, Codex review budget, merge when ready) applies as before.
