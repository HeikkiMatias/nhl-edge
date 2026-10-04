# Handover, 2026-10-04

This file says where the build stands after the cloud sessions of 2026-09-29 to 10-04, and how a fresh session picks it up. CLAUDE.md holds the rules; this file holds the state. Update it, or delete it, when it goes stale.

## Start here (a fresh session)

1. **Read** CLAUDE.md, this file and `docs/model-card.md`.
2. **Finish any waiting item whose date has come** (the next section). The SessionStart hook lists the open issues of the earliest milestone, P1 (#9, #42). They wait on the calendar, not on work, so don't start them early.
3. **Otherwise start phase 4 (#13), in plan mode.** See "Phase 4: where to start" below.
   - Phase 3 is done: #12 is closed, and ADR 0024 records gate 2's verdict.
   - Gate 1 (#79) is still open. It is a checkpoint, not a stop (ADR 0002), so phase 4 doesn't wait for it.

## Waiting items: when and how

| Issue | Ready when | Then |
| --- | --- | --- |
| #9 and #42, the committed audit report | Two weeks of goalie polls from 2026-09-29: about **2026-10-13**. | First bring this machine's lake up to R2 with the commands `nhl status` prints. The report reads only the local lake, and it joins the polls to 2026-27's `games` and `actual_lineups`. Then run `nhl audit report`, commit it to `reports/audit/`, and review it as the comment on #9 says: every problem gets an issue in a milestone, and a dropped season or source gets an ADR. #42 closes with the report's answer on the NHL's starter flag (on the first night it was set for no team, while Daily Faceoff listed most starters as Confirmed). #9 closes with the reviewed report. |
| #30, post-game corrections | Two weeks of rechecked 2026-27 games: the games of 2026-09-29 to about 10-12, each rechecked 7 days later, so about **2026-10-22**. The nightly ingest runs `nhl recheck --recent 3 --r2`. | Run `nhl audit report` and review its corrections section on #30. If corrections change more than scorer credit on more than a few games a season, reopen ADR 0004 with the owner. Then close #30. |
| #79, gate 1 (draft PR #93, branch `phase-2/gate-1`) | #30 is reviewed. | **ADR and checks:** write ADR 0014 with the owner, from the outline in the comment on #79. If #30's corrections change any table its report compares, rerun `/leakage-check` and `/run-backtest`. B2 or B3 reads every one of those tables through its features: `shots`, `shifts`, `actual_lineups`, `shift_coverage`, `strength_time`, `penalties` and `faceoffs`. Penalties feed B3's expected power plays, and faceoffs its stints' zone starts. **Merging:** first merge main into the branch, taking main's `docs/handover.md` and model card over the branch's versions. **Docs:** then write the model card's gate 1 entry on top of main's version. Mark #93 ready (Codex reviews it then), triage and merge. Then close #11, phase 2's summary issue. |

### Gate 1 as it stands

- **The evidence:** `backtest-20261001-388bb85` on 2018-19 and 2021-22.
  - B2 minus B1 is +0.0112 [+0.0044, +0.0185] on E1 and +0.0078 [+0.0013, +0.0146] on E2.
  - B2 is under-confident: its calibration slope is 1.35 [1.09, 1.62].
  - The goalie-start Brier score is 0.415 [0.404, 0.426].
- **The gap review is done** (hard rule 8; in PR #93, `reports/backtest/gap-review.md`). It found no bug.
- **Already decided by the owner:** B2 is not recalibrated before phase 4. Its findings for phase 4 are on #13.

## Phase 3: done (#12)

Every deliverable merged under its own task issue:
- penalties and faceoffs (#96);
- stints (#97, ADR 0015);
- the 57 empty 2024-25 shift charts (#68);
- `player_league_seasons` (#98, ADR 0016);
- lineup availability (#99, ADR 0017);
- ice time and power-play units (#100, ADR 0018);
- RAPM (#101, ADR 0019), with priors (#102, ADR 0020) and tuning (#103, ADR 0011);
- penalty rates and expected power plays (#104, ADR 0021);
- finishing and goalie conversion (#105, ADR 0022);
- B3 (#106, ADR 0023) and gate 2 (#107, ADR 0024).

**B3** is B2's logistic model with one input, Δĝ, in place of team strength and goalie effects. Δĝ is the home team's expected goals less the away team's, built from the projected lineups. B3 minus B2, paired log loss with a 95% weekly block bootstrap interval:

| Test | Run | B3 minus B2 |
| --- | --- | --- |
| Development 2018-19 and 2021-22, E1 | `backtest-20261003-b1a7b04` | -0.0079 [-0.0130, -0.0028] |
| Hockey validation 2023-24 and 2024-25 | `backtest-hockey-20261003-93d0f92` | -0.0078 [-0.0130, -0.0027] |
| One-time test 2025-26 | `backtest-hockey-20261003-82fbada` | -0.0030 [-0.0094, +0.0034] |

- **The verdict (ADR 0024):** gate 2's wording is not met on 2025-26, but B3 goes forward as the model phase 4 compares with the market. B3 minus B2 stays in every report (hard rule 3), and live 2026-27 is B3's next independent evidence.
- **B3 against the market:** on the development seasons, B3 minus B1 is +0.0033 [-0.0013, +0.0077] on E1 and -0.0000 [-0.0047, +0.0043] on E2. Level with the market isn't an edge. The blend is what must add information.
- **Calibration moves between seasons:** the slope is 1.34 on the development seasons, 0.94 on hockey validation and 0.55 on 2025-26. An inputs-only check of 2025-26 found no data shift (model card).
- **The one-time test is spent.** `nhl backtest --hockey-only --one-time-test` is refused for good. Records of the run sit in R2 (`ledger/one_time_test.txt`), beside the lake and in `reports/backtest/one_time_test.txt`.
- **The gap review** (`reports/gaps/b3-gap-review.md`, tool `nhl audit gaps`) found no data error in 92 games read without results.

**Open P3 follow-ups.** None blocks phase 4. Take them when convenient, one PR each:
- #114: `nhl status` compares the stored time-on-ice reports with R2.
- #117: refetch the landing pages once a season.
- #120: two possible calibration gaps in lineup availability. They need intervals before they count as findings.
- #125: a debutant's NHLe in his debut season.
- #130: `nhl power-plays` should refuse games missing inputs.
- #131: schemas should check season against game_id.
- #132: B2's tuning check leaves out `goalie_starts`.
- #134: rate replacement skaters below RAPM's reference skater.

## Phase 4: where to start (#13)

Phase 4 builds the market blend, bet selection and the full backtest, and ends at **gate 3**: the §1 criteria pass, and the selection policy is frozen before live games. Like phases 2 and 3, it starts with a plan the owner approves, not with code.

1. **Enter plan mode and read:**
   - #13: its deliverables, and its findings log, with 12 items from phase 2 and 3 from gate 2;
   - #66: what to do about 2022-23;
   - docs/plan.md §1 (criteria), §5 ("Uncertainty", "Expected return and CLV", "Validation design"), §6, §10 ("Market move guard", "Edge attribution") and §11 ("Staking rules");
   - ADRs 0008, 0013, 0023 and 0024, and the model card.
2. **Write the phase plan.** It must bring these choices to the owner; each modeling choice gets an ADR, with the owner's go-ahead first:
   - **2022-23's role (#66),** to settle first, since it sets what the full backtest covers. SBR prices only 342 of its 1,312 games (26%). The options are:
     - score those 342 games alone;
     - buy the historical odds (about $90, a paid endpoint, so only with the owner's word);
     - drop 2022-23 as a market test and leave it to live 2026-27.

     Before any of its games is scored, run the audit's SBR price checks on the 342 games. Today the audit skips them: `price_seasons()` in `audit/sbr.py` reads only the training and development roles, so `nhl audit report` gives 2022-23's join counts alone. Opening its price checks is part of #66's task.
   - **What the blend trains on.** Hard rule 6 allows only out-of-sample predictions from earlier folds, so the 2018-19 fold has no earlier fold to learn from (#13, finding 7). The plan must say which earlier predictions the blend may use, and which development seasons it is scored on.
   - **Which models go in.** B3 is the model the blend uses (ADR 0024), and B2 stays as B3's reference (hard rule 3). B2 and B3 are under-confident on the development seasons, and B3's calibration moves between seasons (#13, finding 1, and gate 2's notes). The plan must also say whether the blend's own weights correct that, or a per-fold recalibration fitted only on earlier folds. Never retune B2 or B3 on the development seasons.
   - **The uncertainty score u** (§5): an unconfirmed goalie, availability doubts and the share of rookie ice time. `goalie_starts.p_start`, `lineups.p_available` and `lineup_replacements` already hold the inputs.
   - **Selection and staking** (§5, §11): the expected return at the executable price (hard rule 4), at least 2.5% and more when u is high. A quarter of Kelly, capped at 1.5% of bankroll per bet and 5% per day, with one bet per game.
   - **The market move guard** (§10): its threshold set on the development seasons from SBR open against close, then frozen.
   - **E3 on history:** which price counts as taken (E2's opener) and which as the closing proxy (SBR's close), since live snapshots exist only from 2026-09-28.
   - **Attribution** (§10): report separately the games where the model and the market pick different favourites, early-season games, and each season (#13, findings 4, 6 and 11).
3. **After approval:**
   - break #13 into task issues in the P4 milestone and link them from #13 in a comment;
   - paste the approved plan into every phase 4 PR (branches `phase-4/<topic>`).

   Never write a closing keyword followed by another issue's number in a pasted plan, since GitHub acts on it.
4. **Mind the clock.** Live 2026-27 is the only untouched market test, and a model is judged only on games after its freeze date (plan §5). Each week before the policy freezes is a week of live games the test can't use. Phase 5's paper trading also needs the live season's feature tables, which stop at 2026-04-16 (phase 3's) or 2026-09-30 (the others).

## The owner's standing decisions

- **Ask the owner first** before any ADR, any won't-fix on a P0, or anything outside an issue's scope. Explain choices in plain language, without jargon.
- **One PR per issue,** never combined.
- **Tuning (ADR 0011):** each component is tuned once on the training seasons and frozen. That covers team strength, goalie effects, schedule terms, B2's L2 penalty and RAPM. The penalty model and finishing tune nothing (ADR 0021, 0022): they reuse RAPM's and the goalie effect's frozen settings, and they measure their pulls and league figures from earlier data each season. Near-ties go to the steadier setting. The tuning runs' losses are not walk-forward evidence. Codex's P0s on #88 and #92 are won't-fix, by the owner's decision.
- **B2 (ADR 0013) and B3 (ADR 0023):**
  - Both train on the starters who played and predict by mixing over the goalie-start probabilities. B3 trains on the projected skaters, not the ones who dressed. Codex's P0s on #90, #92 and #133 are won't-fix, by the owner's decision.
  - B3 reuses B2's L2 of 100.
  - Gate 2's subsets stay as ADR 0023 defined them.
- **Seen seasons:**
  - The development seasons (2018-19, 2021-22) and the hockey validation seasons (2023-24, 2024-25) have been scored, and 2025-26 is spent. Nothing may be tuned on them.
  - A change to B2 or B3 now needs the owner and an ADR.
- **Gate 2 (ADR 0024):** B3 is phase 4's model against the market. B2 stays in every report as B3's reference.
- **Phase 4 notes** go on #13 as they turn up.
- **Data:**
  - **Stints (ADR 0015):** drop only impossible on-ice counts.
  - **Time-on-ice reports (#68):** fetched once, for the 57 games only, and never again (NHL.com's terms).
  - **Season totals (ADR 0016):** the 2026 copies' goals and assists count as public at the season's end, corrections included.
- **Lineups (ADR 0017, 0018):** the specifications are fixed, with nothing tuned. A change to their inputs, window or total needs a new ADR (#120).

## State

- **Lake (R2):** every regular-season game from 2010-11 to 2025-26 (19,152), plus 2026-27's as the nightly ingest adds them.
  - **Raw facts:**
    - `games` and `schedule` (ADR 0003, 0005), `players`;
    - the per-game tables `shots`, `shifts`, `actual_lineups`, `shift_coverage`, `strength_time`, `penalties` and `faceoffs` (#5, #72, #96, ADR 0004, 0009).
  - **Phase 2's features,** through 2026-09-30:
    - `shot_xg` (ADR 0010);
    - `team_strength`, `goalie_effects` and `schedule_terms` (ADR 0011);
    - `goalie_starts` (ADR 0012).
  - **Phase 3's tables,** through 2025-26 (the last rows are 2026-04-16):
    - `stints`, through 2026-09-30;
    - `player_league_seasons`;
    - `lineups` and `lineup_replacements` (`lineup-20261002-6297f8c`);
    - `player_ratings` and `rapm_terms` (`rapm-20261003-86a736f`);
    - `penalty_rates` and `expected_power_plays` (`power-plays-20261003-86a736f`);
    - `finishing` and `goal_multipliers` (`finishing-20261003-f87acde`).

    The live season's rows wait for phase 5. The SessionStart hook lists the tables that stop before today as "behind", and that is expected.
  - **Tuning cutoff:** the tuned tables' rows of 2011-12 to 2017-18 count as known only from 2018-04-09 10:00 UTC.
  - **Market and goalie data:**
    - `odds_snapshots` holds every Odds API snapshot from 2026-09-28 on (#20).
    - `sbr_odds` holds SBR's openers and closes from 2010-11 to 2022-11-27 (#7, 133,594 prices).
    - `pregame_goalies` and `dailyfaceoff_goalies` hold the goalie polls since 2026-09-29 (#43, #48).
- **Reference files** (`src/nhl_edge/reference/`, #8, #26): team codes, arenas, venue names, home arenas, coaches and attendance limits. `nhl audit reference` checks them. Features read them through `coaches_known_at` and `capacity_share`.
- **SBR archive** (#7, #52, #56, ADR 0006, 0007):
  - It is HTML only and stops on 2022-11-27. That gives 2022-23 342 of its 1,312 games (#66).
  - E2 reads the opener at 10:00 US Eastern on the game date.
  - `reference/sbr_suspect_openers.csv` lists 40 likely wrong openers, and its `bad_close` column marks 3 bad closes (#64).
- **Backtest:**
  - `nhl backtest` runs the walk-forward of B0 to B3 on the development seasons. It writes `reports/backtest/summary.json`, `runs.csv`, `gaps.csv` and `gaps_b3.csv`.
  - `nhl backtest --hockey-only` scores B2 and B3 on outcomes alone, without prices, as for 2023-24 and 2024-25 at gate 2.
  - `nhl audit gaps` explains each B3 gap for review.
  - **The baselines:**
    - **B0** is the de-vigged market (ADR 0008).
    - **B1** is its per-fold recalibration. B1 doesn't beat B0: B0 minus B1 is +0.0004 [-0.0005, +0.0013] on E1.
  - The model card has every figure.
- **Jobs:**
  - **What runs:** GitHub Actions runs the nightly ingest at 09:00 UTC, then `nhl recheck`. It also runs five odds slots a day, and the goalie polls hourly at :50 from 12:50 to 02:50 UTC (docs/data-sources.md, "When jobs run").
  - **The timer:** the Cloudflare Worker `nhl-edge-timer` (`infra/timer`, #53) dispatches them on time. `TIMER_ACTIVE` is `true`, so GitHub's own late schedule skips.
  - **If dispatches stop:**
    - Run `gh variable set TIMER_ACTIVE --body false`, then `npx wrangler@4 tail` in `infra/timer`.
    - A 401 is a bad or expired token (it expires around 2027-09). A 403 or 404 means the token lacks Actions read and write.
    - `npx wrangler@4 secret put GITHUB_TOKEN` stores a new token.
    - Start the goalie polls by hand meanwhile, but never an odds slot: it spends Odds API credits.
- **Open, waiting:** #9 and #42 (P1); #30, #79 and #11 (P2). **Open, later:** #13 and #66 (P4); #14, #21, #67 and #121 (P5); #15 (P6).

## Keep an eye on

- **Missed pre-game polls can't be redone.** If the timer stops, a late poll loses that game's data for #9 and #42.
- **In-sample training seasons:** the training seasons are in-sample for xG's rebound term (ADR 0010) and for every tuned setting (ADR 0011). They may train later models, but no result may present them as out-of-sample.
- **Closing keywords:** a closing keyword followed by another issue's number, anywhere in a PR description, closes that issue on merge. That includes pasted plans.
- **Replaying live dates (#109):** a replay leaves a date alone, with a warning, when its cached schedule lists a game there that isn't final. To rebuild such a date, replay the nightly's own window.
- **Writing feature tables to R2:** run each command with `--r2` from a clean checkout of main, so the rows' `artifact_version` names the merged commit and doesn't end in `-dirty`. Later commands read the tables of earlier ones, so keep this order:
  1. `nhl xg`
  2. `nhl stints`
  3. `nhl team-strength`
  4. `nhl goalie-start`
  5. `nhl lineups` (it copies the goalie-start probabilities, and reads stints for ice time)
  6. `nhl goalie-effect`
  7. `nhl schedule-terms`
  8. `nhl player-seasons` (no fitted model, so it can run any time before `nhl rapm`)
  9. `nhl rapm`
  10. `nhl power-plays`
  11. `nhl finishing`
- **GitHub from a cloud session:** `gh api` works through the session's proxy, but other `gh` commands don't. The GitHub MCP tools work too.
  - The proxy refuses branch deletes, so merged branches stay on GitHub. Delete them from GitHub's branch page if you like.
- **Codex:**
  - It answers as a review with findings, a 👍 reaction, or a comment saying it found no major issues. Check all three.
  - Reply to every finding with "Fixed in <sha>", "Follow-up #n" or "Won't fix: <reason>". A won't-fix on a P0 needs the owner first.
- **Reference upkeep in 2026-27:** at a coaching change, end the old stint in `coaches.csv` and add the new one. Add a new venue name to `venues.csv`, and the building to `arenas.csv` if it is new (docs/data-sources.md). `nhl audit reference` flags both.
- **#67 (P5):** `EXPECTED_GAMES` has no 2026-27 entry. It matters from April 2027.

## Continuing from a terminal

1. `git pull` on `main`, then `uv sync`.
2. `uv run nhl status` compares this machine with R2. To catch up from nothing:
   1. `uv run nhl lake restore-raw` (about 0.5 GB)
   2. `uv run nhl ingest --seasons 20102011-20252026 --replay`
   3. `uv run nhl odds replay`, `uv run nhl odds sbr --replay` and `uv run nhl goalies replay`
   4. the feature commands in the order above
   5. for the live season, the ingest window and replays that `uv run nhl status` prints

   None of them calls a paid endpoint.
3. `uv run nhl backtest` reruns the backtest, and `uv run nhl backtest --hockey-only --seasons 20232024,20242025` the hockey validation. Without `--seasons`, the hockey-only mode scores the development seasons. Commit code first: the run's version ends in `-dirty` when `src/`, `pyproject.toml` or `uv.lock` has uncommitted changes. Never include 2025-26 or live games unless the owner says so.
4. **Tuning:** `nhl team-strength`, `nhl goalie-effect`, `nhl schedule-terms` and `nhl rapm` take `--tune`. They and `nhl tune-b2` rerun their grids on the training seasons only, and log them to `reports/tuning/`. Frozen settings change only through a new ADR.
5. CLAUDE.md's workflow (a branch per issue, the Codex review budget, merging when ready) applies as before.
