# Handover, 2026-10-07

This file says where the build stands after the cloud sessions of 2026-09-29 to 10-07, and how a fresh session picks it up. CLAUDE.md holds the rules; this file holds the state. Update it, or delete it, when it goes stale.

## Start here (a fresh session)

1. **Read** CLAUDE.md, this file and `docs/model-card.md`.
2. **Finish any waiting item whose date has come** (the next section). The SessionStart hook lists the open issues of the earliest milestone, P1 (#9, #42). They wait on the calendar, not on work, so don't start them early.
3. **Otherwise carry on with phase 5 (#14).** Its plan is approved and lives in `docs/plans/phase-5.md`. See "Phase 5: where it stands" below.
   - Phase 4 is done. Gate 3 is not met (ADR 0031): the blend adds no information beyond the recalibrated market on history. As the owner decided, phase 5 paper-trades the frozen policy anyway, and live 2026-27 is the remaining test.
   - Gate 1 (#79) is still open. It is a checkpoint, not a stop (ADR 0002).
   - **The clock runs:** the policy was frozen on 2026-10-05, and live games count from 2026-10-06. Every game without a logged pre-game prediction is a paper bet the live test can't use.

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

**Open P3 follow-ups.** None blocks phase 5. Take them when convenient, one PR each:
- #114: `nhl status` compares the stored time-on-ice reports with R2.
- #117: refetch the landing pages once a season.
- #120: two possible calibration gaps in lineup availability. They need intervals before they count as findings.
- #125: a debutant's NHLe in his debut season.
- #130: `nhl power-plays` should refuse games missing inputs.
- #131: schemas should check season against game_id.
- #132: B2's tuning check leaves out `goalie_starts`.
- #134: rate replacement skaters below RAPM's reference skater.

## Phase 4: done (#13)

Every deliverable merged under its own task issue:
- 2022-23's price checks (#66, ADR 0025);
- the blend's training folds (#138);
- the uncertainty score u (#139, ADR 0026);
- the market blend (#140, ADR 0027);
- selection and staking (#141, ADR 0028);
- the market move guard (#142, ADR 0029);
- E3 and attribution (#143);
- the development backtest and the freeze (#144, ADR 0030);
- 2022-23's one run (#145);
- gate 3 (#146, ADR 0031).

**The frozen policy, `policy-20261005-8ec5cf3`** (model card, "The freeze"):
- **The blend:** logit p = a + b_m·logit p_mkt + (b_x + b_u·u)·logit p_B3, fitted per whole-season fold on earlier folds' out-of-sample predictions.
  - Live bets use **E1's fit**, learned against SBR's close and fed Pinnacle's 12:45 ET price (ADR 0030).
- **u:** goalie doubt, availability doubt and rookie minutes, equally weighted. Live, its goalie doubt reads the goalie-start model, never a confirmation (ADR 0030).
- **Selection and staking:**
  - a hurdle of 2.5% expected return plus 1 point per standard deviation of u above average;
  - a quarter of Kelly divided by (1 + u⁺);
  - caps of 1.5% per bet and 5% per day;
  - 100 units a season.
- **Bets** are placed at 12:45 ET at Pinnacle's price, with the best EU book logged beside it.
- **The guard** skips a bet whose side fell more than 5.35 points between Pinnacle's 07:05 and 12:45 quotes.
- `betting/selection.py` holds `POLICY_VERSION`, `FROZEN_ON` and `LIVE_BLEND_EXPERIMENT`, and `tests/unit/test_freeze.py` fails if a frozen number changes.

**Gate 3 (ADR 0031): not met.** The pooled figures combine 2021-22 and 2022-23 (`reports/backtest/accepted.json`):
- **BLEND minus B1:** E1 -0.0002 [-0.0037, +0.0032] and E2 -0.0027 [-0.0072, +0.0019]. 2022-23 alone loses on both.
- **B3 minus B2:** -0.0068 [-0.0120, -0.0016] over three seasons: passes overall, with the 39-game lineup-change subset inconclusive.
- **The blend's calibration:** passes on intervals.
- **CLV against SBR's close:** -1.23% [-2.01%, -0.46%] over 625 bets. That fails for E2's fit at the opener; the live policy's E1 fit has no historical bets, so only live can judge it.
- **Gaps:** reviewed by hand, with one data error and no bug.

**2022-23 is spent.** Its one run is `market-validation-20261005-6ec331b`, claimed in R2 (`ledger/market_validation.txt`), beside the lake and in `reports/backtest/market_validation.txt`. A second run is refused.

**ADR 0026's revisit trigger fired.** 2022-23's fits put u's weight at +0.24 [+0.01, +0.48] (E1): the model is trusted more as doubt grows. The owner kept u frozen. Live evidence decides, and any change is a new policy version.

**Open follow-ups:** none blocks phase 5.
- #154 (P5): the E3 driver mostly marks favourite against underdog.
- #156 (P5): recompute the blend's probability in the gap screen.

## Phase 5: where it stands (#14)

**The plan** was approved by the owner on 2026-10-05 and is committed as `docs/plans/phase-5.md`, which is also posted on #14. Paste it into every phase 5 PR's description, as CLAUDE.md asks.
- It covers the daily pipeline, the live blend fit, `nhl predict` with a write-once paper ledger, the closing proxy, settlement and CLV, the live report, attribution, Supabase and the dashboard, and a raw backup.
- **The owner's rulings of 2026-10-05:**
  - 2022-23's predict-only rebuild for the live blend is not a second run.
  - Live B3 uses no confirmed starter.

**Order of work:**
1. **The owner's [priority] issues come first under CLAUDE.md:**
   - #170: live data readiness;
   - #171: immutable run bundles;
   - #172: how live evidence is judged, an ADR for the owner.

   Each asks for an assessment first, and says not to delay the first valid paper ledger. Fold #170's and #171's minimum checks into tasks 1 to 3 rather than ahead of them.
2. **Then the critical path to the first logged slate:**
   - task 1, #162: live targets and feature rows;
   - task 2, #163: the live blend fit;
   - task 3, #164: `nhl predict` and the ledger.
3. **Then** tasks 4 to 9, and #173 after #30.

**State on 2026-10-07:**
- **No live prediction is logged yet.** Every game from 2026-10-06 until the first logged slate is lost to the live test and is never reconstructed.
- **Task 2 is in review** on `phase-5/live-blend`: `nhl live blend-fit` and `live/blend_fit.py`. The dry run reproduces the one run's 2022-23 fold fits.
  - **After merge:** run `nhl live blend-fit --r2` once from a clean checkout of main, with the lake up to R2. Commit its `reports/live/` files. A second run is refused.
- **Task 1 hasn't started.** An agent began it on 2026-10-05, but it was cut off and left nothing. `phase-5/live-features` is an empty branch at main.
- **Reminders** for the dated items fire into this session on 2026-10-13 and 2026-10-22 at 10:30 UTC.

**Never change the frozen policy** to fit live results. A change is a new policy version with its own ADR and freeze date, and the CLV count restarts.

## The owner's standing decisions

- **Ask the owner first** before any ADR, any won't-fix on a P0, or anything outside an issue's scope. Explain choices in plain language, without jargon.
- **One PR per issue,** never combined.
- **Tuning (ADR 0011):** each component is tuned once on the training seasons and frozen. That covers team strength, goalie effects, schedule terms, B2's L2 penalty and RAPM. The penalty model and finishing tune nothing (ADR 0021, 0022): they reuse RAPM's and the goalie effect's frozen settings, and they measure their pulls and league figures from earlier data each season. Near-ties go to the steadier setting. The tuning runs' losses are not walk-forward evidence. Codex's P0s on #88 and #92 are won't-fix, by the owner's decision.
- **B2 (ADR 0013) and B3 (ADR 0023):**
  - Both train on the starters who played and predict by mixing over the goalie-start probabilities. B3 trains on the projected skaters, not the ones who dressed. Codex's P0s on #90, #92 and #133 are won't-fix, by the owner's decision.
  - B3 reuses B2's L2 of 100.
  - Gate 2's subsets stay as ADR 0023 defined them.
- **Seen seasons:**
  - The development seasons (2018-19, 2021-22) and the hockey validation seasons (2023-24, 2024-25) have been scored. 2025-26 and 2022-23's 342 priced games are spent. Nothing may be tuned on any of them.
  - A change to B2 or B3 now needs the owner and an ADR.
- **Gate 2 (ADR 0024):** B3 is phase 4's model against the market. B2 stays in every report as B3's reference.
- **Phase 4 (ADRs 0025 to 0031):**
  - The policy is frozen as `policy-20261005-8ec5cf3`. Live games count from 2026-10-06.
  - Live bets use E1's blend fit and Pinnacle's 12:45 ET price, and u never reads a confirmation (ADR 0030). Codex's P0s on #155 and #159, "closing odds in a tradable prediction" for E1's fit, are won't-fix by the owner's decision: the fit reads only earlier seasons' closes.
  - Gate 3 is not met, and phase 5 goes ahead regardless (ADR 0031).
  - u stays frozen although its revisit trigger fired: live evidence decides.
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
  - `nhl backtest` runs the walk-forward of B0 to B3, the three blends, the policy's bets and E3 on the development seasons. It writes `reports/backtest/summary.json`, `runs.csv`, `bets.csv`, `gaps.csv`, `gaps_b3.csv` and `gaps_blend.csv`.
  - `nhl backtest --seasons 20222023 --market-validation` was 2022-23's one run, and it is refused for good.
  - `reports/backtest/accepted.json` holds the frozen policy's figures and gate 3's verdict.
  - `nhl backtest --hockey-only` scores B2 and B3 on outcomes alone, without prices, as for 2023-24 and 2024-25 at gate 2.
  - `nhl audit gaps` explains each B3 gap for review. `--blend` does the same for the blend's gaps, each at its own prediction time.
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
- **Open, waiting:** #9 and #42 (P1); #30, #79 and #11 (P2). **Open, later:** phase 5's #14 and #162 to #173, with #21, #67, #121, #154 and #156 (P5); #15 (P6).

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
  - It doesn't always start on its own. If nothing has arrived about 15 minutes after a PR opens, comment `@codex review`. A "Something went wrong" reply is an error, not a review round: ask again.
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
3. `uv run nhl backtest` reruns the backtest, and `uv run nhl backtest --hockey-only --seasons 20232024,20242025` the hockey validation. Without `--seasons`, the hockey-only mode scores the development seasons. Commit code first: the run's version ends in `-dirty` when `src/`, `pyproject.toml` or `uv.lock` has uncommitted changes. Never include 2025-26, 2022-23 or live games unless the owner says so: 2025-26's and 2022-23's one runs are spent.
4. **Tuning:** `nhl team-strength`, `nhl goalie-effect`, `nhl schedule-terms` and `nhl rapm` take `--tune`. They and `nhl tune-b2` rerun their grids on the training seasons only, and log them to `reports/tuning/`. Frozen settings change only through a new ADR.
5. CLAUDE.md's workflow (a branch per issue, the Codex review budget, merging when ready) applies as before.
