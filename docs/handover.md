# Handover, 2026-09-30

Where the build stands after the cloud sessions of 2026-09-29 and 30, and how to pick it up from a terminal. CLAUDE.md holds the rules. This file holds the state. Update or delete it when it goes stale.

## State

- **Lake (R2):** every regular-season game from 2010-11 to 2025-26 (19,152).
  - `games` and `schedule` (ADR 0003 and 0005), `players`
  - the per-game tables `shots`, `shifts`, `actual_lineups` and `shift_coverage` (#5, ADR 0004)
  - `odds_snapshots`, every stored Odds API snapshot from 2026-09-28 on, matched to NHL games (#20)
  - `sbr_odds`, the SBR archive's opening and closing lines from 2010-11 to 2022-11-27, matched to NHL games (#7, 133,594 prices)
  - `pregame_goalies` and `dailyfaceoff_goalies`, from the goalie polls since 2026-09-29 (#43, #48)
- **Reference files (`src/nhl_edge/reference/`, #8 and #26):** team codes, arenas, venue names, home arenas by season, head coach stints and attendance limits. `nhl audit reference` checks them against every game. Features read them through `coaches_known_at` and `capacity_share`, never the CSVs.
- **SBR archive (#7, #52, #56, ADR 0006):**
  - HTML only, and it stops on 2022-11-27, so 2022-23 has 342 of its 1,312 games (26%). 2010-11 to 2021-22 join 100%.
  - A price counts as seen from the game's start. E2 reads the opener at 10:00 US Eastern on the game date (ADR 0006), a separate column.
  - `reference/sbr_suspect_openers.csv` lists 40 openers that are likely wrong, with the evidence for each.
  - `nhl odds sbr --replay` rebuilds the table from the stored pages.
- **Audit (#9):** `nhl audit report` writes `reports/audit/<as-of>.md`: games per season, shift coverage, reference files, snapshot health and the SBR section. On the real lake the SBR section found, among other problems, the closing vig dropping from 3.8% to 2.3% in 2018-19 (likely another closing book), 3 openers summing below 100% and 33 open-to-close moves above 15 points. No report is committed yet: the goalie section comes first.
- **Backtest (#10):** `nhl backtest` runs the walk-forward on the development seasons 2018-19 and 2021-22 and writes `reports/backtest/summary.json`, `runs.csv` and the model card's figures.
  - B0 is the de-vigged market under multiplicative, power and Shin (`market/devig.py`). The three tie: every paired interval includes 0.
  - B1 is a logistic recalibration of B0's log-odds, fitted per fold on the earlier seasons. It does not beat B0: B0 minus B1 is +0.0004 [-0.0005, +0.0013] on E1.
  - E1 (the close) B0 log loss 0.6571 [0.6469, 0.6668]. E2 (the opener) 0.6605 [0.6505, 0.6700]. E2 minus E1, paired: +0.0034 [+0.0006, +0.0064].
  - E2 refuses the 8 openers whose de-vigged home probability is outside 0.15 to 0.85 (ADR 0007). E2 on every opener is reported under `sensitivity` in summary.json (+0.0046 [+0.0011, +0.0083] against E1).
- **Jobs:** GitHub Actions runs the nightly ingest (09:00 UTC), five odds slots a day, and the goalie polls hourly at :50 from 12:50 to 02:50 UTC. docs/data-sources.md, "When jobs run", has the times.
- **Run times (#50):** GitHub's `schedule` starts this repo's runs 3 to 6 hours late, after most puck drops. The Cloudflare Worker in `infra/timer` dispatches every job on time (`infra/timer/README.md`). Its first GitHub token got 401. The owner stored a new one on 2026-09-30 at 09:23 UTC, and set the repository variable `TIMER_ACTIVE` to `true` at 09:32 UTC, before the Worker had dispatched anything with it.
  - While `TIMER_ACTIVE` is `true`, GitHub's own scheduled runs skip, so if the Worker fails, nothing runs at all.
  - The first job due is the morning odds slot at 11:05 UTC (07:05 ET). After it, run `gh run list --workflow odds-snapshots.yml --event workflow_dispatch --limit 3`.
  - If no run from 11:05 shows, run `gh variable set TIMER_ACTIVE --body false` so the late GitHub schedule takes over again. `npx wrangler@4 tail` in `infra/timer` then shows each dispatch's response.
  - A goalie poll can be started by hand 30 to 60 minutes before puck drop: `gh workflow run pregame-goalies.yml`. Never start an odds slot by hand, since it spends Odds API credits.
  - #50 closes once a day of runs has landed on time.
- **Closed in P1:** #4, #5, #6, #7, #8, #20, #24, #25, #26, #27, #29, #43, #48, #52, #56.

## In flight

- **PR #60, E2 refuses implausible openers (#10, part 5).** It also carries this handover. CI is green. Codex's automatic review of `7ad29c9` was still running at handover. If Codex posts findings, triage them with `/review-triage`; if it answers with a 👍 or "Didn't find any major issues", merge it. #10 stays open after it.

## Decisions waiting for the owner

1. **Confirm ADR 0007** (E2 refuses implausible openers). It records the "Only the 8" choice of 2026-09-30 and is merged as Proposed. Mark it Accepted, or reply with changes.
2. **The default de-vig method (#10).** The recommendation is multiplicative: the backtest can't tell the three methods apart, and B1's slope already corrects a favourite-longshot bias, so power or Shin would correct it twice. B1's slope is 1.07 to 1.12, the opposite of the bias they correct. A draft sits in the project's files as `plans/adr-0007-de-vig-default-draft.md`; it becomes ADR 0008, since 0007 is taken. Its figures come from run `backtest-20260929-31f72de` and need the current run's.
3. **SBR's 2018-19 closing-book switch.** The default keeps the close as it is. E2 against E1 from 2018-19 on then mixes timing with a change of book, as the model card says.
4. **2022-23 in phase 4.** The plan runs phase 4's full backtest on 2022-23, but SBR covers only 26% of it. Decide the phase 4 test season when phase 4 starts.

## Next

1. **#10, the phase 1 gate.** What is left:
   - the de-vig ADR (decision 2), then give `devig.py` a default method and use it in B0, B1 and the audit;
   - the two golden tests in the comment on #10, vig and regulation against moneyline. The second waits for a finished overtime game with both `h2h` and `h2h_3_way` quotes. The owner runs the freeze script, since `tests/golden/` is protected;
   - the done-when's "snapshots landing five times a day", which waits on #50.
2. **#9 and #42, the goalie section.** After two weeks of polls (from 2026-09-29, so around 2026-10-13), compare how early and how accurately `pregame_goalies` (NHL) and `dailyfaceoff_goalies` (Daily Faceoff: status, report time, source) name the starter, against `actual_lineups`. Then commit the report, review it, and file an issue in a milestone for every problem. #42 closes with that report.
3. **Then P2:** #11, with #28 (strength source where `situationCode` drifts) and #30 (post-game corrections) before the first B2 backtest.

## Keep an eye on

- **Missed pre-game polls can't be redone.** Until #50 is settled, a late poll loses that game's data for #9.
- **#21 (P5):** the closing proxy should key on the Odds API event and its commence time, not `game_id`.
- **Reference upkeep in 2026-27:** at a coaching change, end the old stint in `coaches.csv` and add the new one. Add a new venue name to `venues.csv`, and the building to `arenas.csv` if it is new. `nhl audit reference` flags both once the nightly ingest adds the game.
- **`EXPECTED_GAMES` has no 2026-27 entry.** The 2026-27 schedule has 1,344 regular-season games. Without the entry, a season ingest's game count and the reference checks that need a finished season skip 2026-27.
- **The implausible-opener bounds (ADR 0007) are fixed.** They were set with the development seasons' closes in view, so never tune them on a held-out season.

## Continuing from a terminal

1. `git pull` on `main`, then `uv sync`.
2. `uv run nhl status` compares this machine with R2. To catch up: `uv run nhl lake restore-raw`, `uv run nhl ingest --seasons 20102011-20252026 --replay`, `uv run nhl odds replay`, `uv run nhl odds sbr --replay` and `uv run nhl goalies replay`. None of them calls a paid endpoint.
3. `uv run nhl backtest` reruns the backtest in under a minute. Commit code first: the run's version string ends in `-dirty` when `src/`, `pyproject.toml` or `uv.lock` has uncommitted changes.
4. The SessionStart hook lists the open issues of the earliest milestone. CLAUDE.md's workflow (branch per issue, Codex review budget, merge when ready) applies as before.
