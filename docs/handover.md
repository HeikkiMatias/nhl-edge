# Handover, 2026-10-02

Where the build stands after the cloud sessions of 2026-09-29 to 10-02, and how a fresh session picks it up. CLAUDE.md holds the rules. This file holds the state. Update or delete it when it goes stale.

## Start here (a fresh session)

1. **Read** CLAUDE.md, this file and `docs/model-card.md`.
2. **Finish any waiting item whose time has come** (next section). The SessionStart hook lists the open issues of the earliest milestone, P1 (#9, #42). They wait on the calendar, not on work, so don't start them early.
3. **Then phase 3** (#12), already under way: its plan is approved and pasted in every phase 3 PR (copy it from PR #116). The next task is **#100**, ice time and power-play units (below). Gate 1 is a checkpoint, not a stop (ADR 0002).

## Waiting items: when and how

| Issue | Ready when | Then |
| --- | --- | --- |
| #9 and #42, the committed audit report | Two weeks of goalie polls, from 2026-09-29: about **2026-10-13**. | First bring this machine's lake up to R2 with the commands `nhl status` prints. The report reads only the local lake, and it joins the polls to 2026-27's `games` and `actual_lineups`. Then run `nhl audit report` and commit it to `reports/audit/`. Review it as the comment on #9 says: every problem gets an issue in a milestone, and a dropped season or source gets an ADR. #42 closes with the report's answer on the NHL's starter flag (on the first night it was set for no team, while Daily Faceoff listed most starters as Confirmed). #9 closes with the reviewed report. |
| #30, post-game corrections | Two weeks of rechecked 2026-27 games: games of 2026-09-29 to about 10-12, each rechecked 7 days later, so about **2026-10-22**. The nightly ingest runs `nhl recheck --recent 3 --r2`. | Run `nhl audit report` and review its corrections section on #30. If corrections change more than scorer credit on more than a few games a season, reopen ADR 0004 with the owner. Close #30. GitHub closed it once by mistake (a pasted plan contained a closing keyword); it was reopened on 2026-10-01. |
| #79, gate 1 (draft PR #93, branch `phase-2/gate-1`) | #30 is reviewed. | Write ADR 0014 with the owner, from the outline in the comment on #79. If #30's corrections change any table B2 reads, through its features or its training (`shots`, `shifts` or `actual_lineups`, which feeds the goalie-start model and the starters B2 trains on), rerun `/leakage-check` and `/run-backtest`. Update the model card's gate 1 entry. Merge main into the branch: take main's `docs/handover.md` over the branch's version. Mark #93 ready (Codex reviews it then), triage, merge. Then close #11, phase 2's summary issue, by hand. |

### Gate 1 as it stands

- **The evidence:** `backtest-20261001-388bb85` on 2018-19 and 2021-22. B2 minus B1 is +0.0112 [+0.0044, +0.0185] on E1 and +0.0078 [+0.0013, +0.0146] on E2. B2 is under-confident (calibration slope 1.35 [1.09, 1.62]). The goalie-start Brier score is 0.415 [0.404, 0.426].
- **The gap review is done** (hard rule 8, in PR #93: `reports/backtest/gap-review.md`). `nhl gap-review` screened all 822 E1 gaps above 8 points, and 153 were reviewed by hand without reading results. It found no bug. The gaps come from B2's timidity, news the market prices (injuries, trades, COVID absences), results running far from expected goals, and early-season roster changes. The owner approved the method.
- **Already decided by the owner:** B2 is not recalibrated before phase 4. Findings for phase 4 are logged on #13 as they turn up (12 so far). ADR 0011 records every tuning (#91, #92).

## Phase 3: the player layer (#12)

The phase plan was approved on 2026-10-01. It is pasted in full in every phase 3 PR (copy it from the description of PR #116). Never write a closing keyword followed by another issue's number in a pasted plan: GitHub acts on it.

**Done:**
- **Task 1, `penalties` and `faceoffs`** from play-by-play.
- **Task 2, `stints` (#97, PR #111, ADR 0015).** Only impossible counts are dropped.
- **Task 3, #68 (PR #115).** The 57 empty shift charts of 2024-25 are rebuilt from the NHL's time-on-ice reports. These were fetched once with the owner's leave, since NHL.com's terms forbid scraping, and are never fetched again.
- **Task 4, `player_league_seasons` (#98, PR #116, ADR 0016).**
  - Each player's season lines in every league, from the cached landing pages. On R2 since 2026-10-02: 112,840 rows.
  - A line is public on July 1 after its season, and later for late leagues and seasons.
  - Never before the player's first NHL boxscore (`first_boxscore_utc`), and a player without one has no rows.
  - ADR 0016 accepts later corrections in the 2026 copies' goals and assists.
  - `docs/data-sources.md` "Player league seasons" has the details.
- **Task 5, `lineups` (#99, PR #119, ADR 0017).**
  - Each team-game's candidate skaters, from its last 10 public boxscores, get a probability of dressing (`p_available`). It comes from a logistic regression fitted per season on earlier seasons, with nothing tuned.
  - Each team-game's probabilities are shifted to add up to 18 less the expected newcomers: about 0.28 in ordinary games, 5.0 in a season's first.
  - Goalies' `p_start` is copied from `goalie_starts`.
  - It beats "dressed last game" in every shown season. The first run is `lineup-20261002-5abb69d`, with its report in `reports/lineups/`.
  - On R2 for 2011-12 to 2025-26. The live season waits for `goalie_starts` (phase 5).

**Next: #100, ice time and power-play units** (task 6). It reads `lineups` through `known_at` at the prediction time, as B2 reads `goalie_starts`. Its ADR goes to the owner before any fit is scored.

**Then, in order:** #101 (RAPM), #102 (priors), #103 (RAPM tuning), #104 (penalty rates), #105 (finishing and goalie conversion), #106 (B3 in the walk-forward), #107 (gate 2). Each modeling task stops for its ADR with the owner.

**For #102,** see the two comments on it:
- The table already enforces the first-game rule, but the priors still need a leakage test.
- 124 player-season-game types list one league under two names (all 2009-10 to 2018-19), so summing by `league` must handle them.

**Open follow-ups:**
- **#114 (P3):** `nhl status` should compare the stored time-on-ice reports with R2.
- **#117 (P3):** refetch the landing pages once a season. It first matters for the 2027-28 priors, and it measures ADR 0016's corrections.
- **#120 (P3):** two possible calibration gaps in the lineup model, seen as point estimates only. Skaters after an early exit: given 0.52, dressed 0.39. A season's first game: last season's regulars given 0.67, dressed 0.63; the others given 0.35, dressed 0.44. None is a finding yet (hard rule 7). First give each gap a weekly block bootstrap interval, then measure whether it moves #100 or B3. Any change needs a new ADR.
- **#121 (P5):** evaluate Yahoo or Daily Faceoff injury statuses as a live-only lineup source. Check the terms first, then log them and measure them against who dressed.

**Gate 2:** B3 beats B2 on future games, overall and after trades, injuries and lineup changes. That includes the one-time 2025-26 test, which needs the owner's explicit go-ahead. B2 is B3's reference (hard rule 3).

**Loose ends from 2026-10-02:**
- Merged branches can't be deleted from the cloud container: git fails with "remote end hung up". `phase-3/player-league-seasons` and `phase-3/handover-2026-10-02` are still on GitHub. Delete them from their PR pages.
- A stray backup, `/schemas.bak`, sits at the cloud container's root. It is harmless, and a new container won't have it.
- The local lake on the old container had the 2026-10-01 games only partly pulled. `nhl status` prints what to replay.
- The derived tables (`shot_xg`, `stints`, `team_strength` and the others) stop at 2026-09-30 on R2 too. Nothing needs them for live games before phase 5.

## The owner's standing decisions

- **Ask the owner first** before any ADR, any won't-fix on a P0, or anything outside an issue's scope. Explain choices in plain language, without jargon.
- **One PR per issue,** never combined.
- **Tuning (ADR 0011):** each component is tuned once on 2012-13 to 2017-18 and frozen. Near-ties go to the steadier setting, and grids are kept as fixed even when the choice sits on the edge.
  - Later tunings reuse earlier frozen settings. Codex's P0s on #88 and #92 are won't-fix by the owner.
  - The tuning runs' losses are not walk-forward evidence; the development seasons are.
- **B2 (ADR 0013):** it trains on the starters who played and predicts by mixing over the goalie-start probabilities. Codex's P0s on #90 and #92 are won't-fix by the owner.
- **The development seasons have been seen** (gate 1). Nothing may be tuned on them. A change to B2 now needs the owner and an ADR.
- **Phase 4 notes** go on #13 as they turn up, including B2's recalibration.
- **Stints (ADR 0015):** drop only impossible on-ice counts.
- **Time-on-ice reports (#68):** fetched once, for the 57 games only, and never again.
- **Season totals (ADR 0016):** the 2026 copies' goals and assists count as public at the season's end, corrections included. Per-game scoring credits still need their own decision (ADR 0004).
- **Lineup availability (ADR 0017):** the specification is fixed, with nothing tuned. A change to its inputs, window or total needs a new ADR (#120).

## State

- **Lake (R2):** every regular-season game from 2010-11 to 2025-26 (19,152), and 2026-27's as the nightly ingest adds them.
  - `games` and `schedule` (ADR 0003 and 0005), `players`
  - the per-game tables `shots`, `shifts`, `actual_lineups`, `shift_coverage` and `strength_time` (#5, #72, ADR 0004)
    - `shots` takes its skater counts from a complete shift chart, and `situationCode` otherwise (#28, ADR 0009); `strength_source` says which.
    - `shots` carries the play before each shot (`prev_` columns, #73).
    - `strength_time` gives each team's seconds at each strength state. Every game's seconds add up but 7 old games' (2011-12 to 2015-16), which the audit report lists.
  - `shot_xg`, every unblocked shot's xG from 2011-12 on (#73, ADR 0010, `xg-20261001-de27a2c`), from one model per season fitted on earlier seasons
  - `team_strength` (#74, ADR 0011, `team-strength-20261001-1b2a5b8`): a half-life of 80 games and a pull worth 40 games
  - `goalie_starts` (#76, ADR 0012, `goalie-start-20261001-784c4fb`): each candidate's start probability, one model per season
  - `goalie_effects` (#75, ADR 0011, `goalie-effect-20261001-c05c300`): a half-life of 160 goalie games and a pull worth 4,000 shots
  - `schedule_terms` (#77, ADR 0011, `schedule-terms-20261001-84bc182`): rest, travel, time zones, neutral sites, open seats, and h_s with a pull of 1,600 games
  - The tuned tables' rows of 2011-12 to 2017-18 count as known only from the tuning cutoff, 2018-04-09 10:00 UTC.
  - `odds_snapshots`, every stored Odds API snapshot from 2026-09-28 on, matched to NHL games (#20)
  - `sbr_odds`, the SBR archive's opening and closing lines from 2010-11 to 2022-11-27, matched to NHL games (#7, 133,594 prices)
  - `pregame_goalies` and `dailyfaceoff_goalies`, from the goalie polls since 2026-09-29 (#43, #48)
- **Reference files (`src/nhl_edge/reference/`, #8 and #26):** team codes, arenas, venue names, home arenas by season, head coach stints and attendance limits. `nhl audit reference` checks them against every game. Features read them through `coaches_known_at` and `capacity_share`, never the CSVs.
- **SBR archive (#7, #52, #56, ADR 0006):**
  - HTML only, and it stops on 2022-11-27, so 2022-23 has 342 of its 1,312 games (26%). 2010-11 to 2021-22 join 100%.
  - E2 reads the opener at 10:00 US Eastern on the game date (ADR 0006), or when the schedule became public if later.
  - `reference/sbr_suspect_openers.csv` lists 40 likely wrong openers. Its `bad_close` column marks 3 in 2015-16 whose close is the error instead (#64), and B1 still fits on them.
  - `nhl odds sbr --replay` rebuilds the table from the stored pages.
- **Backtest:** `nhl backtest` runs the walk-forward on the development seasons and writes `reports/backtest/summary.json`, `runs.csv` and `gaps.csv`.
  - **B0** is the de-vigged market (multiplicative, ADR 0008).
  - **B1** is its per-fold recalibration. It does not beat B0: B0 minus B1 is +0.0004 [-0.0005, +0.0013] on E1 and +0.0005 [-0.0006, +0.0015] on E2.
  - **B2** is the team and goalie model (ADR 0013), refused for any fold before the tuning cutoff.
  - E2 refuses implausible openers (ADR 0007), with every opener as a sensitivity.
  - `diagnostics.book_era` (#65) shows no sign that SBR's change of closing book in 2018-19 hurts B1. B0 minus B1 on E1 is -0.0007 [-0.0014, -0.0000] before it and +0.0002 [-0.0004, +0.0008] after, a difference of +0.0009 [-0.0000, +0.0019].
    - E2's cost against E1, +0.0018 before and +0.0024 after (difference +0.0005 [-0.0016, +0.0027]), can't separate the book from timing.
    - The interval still allows a book effect as large as the whole timing cost.
  - The model card has every figure.
- **Jobs:**
  - GitHub Actions runs the nightly ingest (09:00 UTC, then `nhl recheck`), five odds slots a day, and the goalie polls hourly at :50 from 12:50 to 02:50 UTC. docs/data-sources.md, "When jobs run", has the times.
  - The Cloudflare Worker `nhl-edge-timer` (`infra/timer`, #53) dispatches them on time, since GitHub's own schedule runs 3 to 6 hours late (#50).
  - The repository variable `TIMER_ACTIVE` is `true`, so GitHub's scheduled runs skip.
  - **If the dispatches stop:** run `gh variable set TIMER_ACTIVE --body false`, then `npx wrangler@4 tail` in `infra/timer`.
    - A 401 is a bad or expired token (it expires around 2027-09).
    - A 403 or 404 means the token lacks Actions read and write.
    - `npx wrangler@4 secret put GITHUB_TOKEN` stores a new token.
    - While it's down, start the goalie polls by hand. Never start an odds slot by hand: it spends Odds API credits.
- **Odds snapshots:** a book's market priced at 1.0 is skipped, and the raw copy keeps it (#83, #84).
- **Goalie polls (#42, #43, #48):** the NHL pre-game poll and Daily Faceoff run at every odds slot and hourly at :50. RotoWire is left out: its terms forbid scraping.
- **Closed in P1:** #4, #5, #6, #7, #8, #10 (the phase 1 gate, 2026-10-02: all five odds slots of 2026-10-01 landed on the timer), #20, #24, #25, #26, #27, #29, #43, #48, #50, #52, #56, #64, #65, #83, #109 (replays leave a date with an unfinished game alone), #110 (`nhl status` hints for fitted tables).
- **Closed in P2:** #28, #72, #73, #74, #75, #76, #77, #78, #91.
- **Open, waiting:** #9, #42 (P1); #30, #79, #11 (P2).
- **Later milestones:** #66 (P4, the owner decides on 2022-23 when phase 4 starts), #67 and #21 (P5).

## Keep an eye on

- **Missed pre-game polls can't be redone.** If the timer stops, a late poll loses that game's data for #9 and #42.
- **In-sample training seasons:** xG of 2012-13 to 2017-18 is in-sample for its rebound term (ADR 0010), and the tuned tables there for their settings (ADR 0011). They may train later models, but no result may present them as out-of-sample.
- **xG's known misses (ADR 0010):** 3v3 overtime is under-predicted by about 1.6 goals per 100 shots, and shots from 0 to 10 feet are over-predicted by about 0.6.
- **Closing keywords:** a closing keyword followed by another issue's number, anywhere in a PR description, closes that issue on merge. That includes pasted plans; it is how #30 closed by mistake.
- **Replaying live dates (#109):** a replay leaves a date alone, with a warning, when its cached schedule lists a game there that is not final; on 2026-10-01 such a replay deleted the 2026-09-30 games. To rebuild such a date, replay the nightly's own window, whose schedule copy was fetched after the games ended.
- **Writing feature tables to R2:** run each command with `--r2` from a clean checkout of main, so the rows' `artifact_version` names the merged commit and doesn't end in `-dirty`. Their order is:
  1. `nhl xg`
  2. `nhl stints`
  3. `nhl team-strength`
  4. `nhl goalie-start`
  5. `nhl lineups` (it copies the goalie-start probabilities)
  6. `nhl goalie-effect`
  7. `nhl schedule-terms`

  `nhl player-seasons --r2` rebuilds `player_league_seasons` from the raw landing pages. It has no fitted model, so its place in the order doesn't matter.
- **Cloud sessions have no `gh`:** use the GitHub MCP tools, or the API through the session's proxy (`curl` with `Content-Type: application/json` for POST and PATCH).
- **Codex:**
  - It answers as a review with findings, a 👍 reaction, or a comment saying it found no major issues. Check all three.
  - Your own replies come back as events; skip them.
  - Reply to every finding with "Fixed in <sha>", "Follow-up #n" or "Won't fix: <reason>". A won't-fix on a P0 needs the owner first.
- **Merged branches left on GitHub:** the session's git proxy refuses branch deletes. Delete them from GitHub's branch page if you like.
- **Reference upkeep in 2026-27:** at a coaching change, end the old stint in `coaches.csv` and add the new one. Add a new venue name to `venues.csv`, and the building to `arenas.csv` if it is new. `nhl audit reference` flags both once the nightly ingest adds the game.
- **#67 (P5):** `EXPECTED_GAMES` has no 2026-27 entry, so the season's game count and the reference checks that need a finished season skip it. It matters from April 2027.

## Continuing from a terminal

1. `git pull` on `main`, then `uv sync`.
2. `uv run nhl status` compares this machine with R2. To catch up from nothing:
   1. `uv run nhl lake restore-raw`
   2. `uv run nhl ingest --seasons 20102011-20252026 --replay`
   3. `uv run nhl odds replay`, `uv run nhl odds sbr --replay` and `uv run nhl goalies replay`
   4. the feature commands in the order above
   5. the live season: `uv run nhl status` prints the ingest window and replays that bring 2026-27 up to R2

   None of them calls a paid endpoint.
3. `uv run nhl backtest` reruns the backtest in about half a minute. `uv run nhl gap-review` screens its gaps once #93 merges; until then it exists only on branch `phase-2/gate-1`. Commit code first: the run's version ends in `-dirty` when `src/`, `pyproject.toml` or `uv.lock` has uncommitted changes.
4. **Tuning:** `nhl team-strength --tune`, `nhl goalie-effect --tune`, `nhl schedule-terms --tune` and `nhl tune-b2` rerun their grids on the training seasons and log them to `reports/tuning/`. xG and the goalie-start model have no `--tune`. xG's rebound term per season was chosen once from four specifications on the training seasons (ADR 0010), and the goalie-start model has nothing tuned (ADR 0012). Frozen settings change only through a new ADR.
5. CLAUDE.md's workflow (branch per issue, Codex review budget, merge when ready) applies as before.
