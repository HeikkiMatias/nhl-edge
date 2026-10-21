# Phase 5 plan: paper-trading the frozen policy, and the dashboard (#14)

<!-- The approved phase 5 plan, kept in the repository so reviewers (Codex included) and fresh sessions can read it. Change it only with the owner's approval. -->

**Status:** approved by the owner on 2026-10-05, in plan mode. It is also posted on #14. The owner approved amendments on 2026-10-07, listed under "The owner's word needed".

**Issues:**
- **Task issues (P5 milestone):** 1 #162, 2 #163, 3 #164, 4 #21, 5 #165, 6 #166, 7 #154, 8 #167 and #168, 9 #169.
- **Added by the owner after approval:**
  - #170, #171 and #172, marked [priority]: live data readiness, immutable run bundles, and how live evidence is judged.
  - #173: post-game corrections against predictions, after #30.

  Each says to assess first, and not to delay the first valid paper ledger.

## Context

Phase 4 is done. Gate 3 is not met (ADR 0031): on history the blend adds no information beyond the recalibrated market. The owner decided that phase 5 paper-trades the frozen policy `policy-20261005-8ec5cf3` anyway, and that live 2026-27 is the remaining test. Live games count from **2026-10-06**. Any game without a logged pre-game prediction is a paper bet the live test can't use. A prediction reconstructed later is not evidence (handover).

Today, 2026-10-05:
- **Already running:** Pinnacle's 07:05 and 12:45 ET snapshots land every game day (`odds-snapshots.yml`, slots `morning` and `midday`). The goalie polls run. The nightly ingest adds played games.
- **Still missing:**
  - `nhl predict` is a stub.
  - No table has rows for games that haven't been played. `games`, and every table built from it, lists only final games.
  - No feature table refreshes nightly. Phase 3's tables stop at 2026-04-16 and the others at 2026-09-30.
  - No `predictions` or `bets` table exists in the lake, in Supabase or in a migration.
  - The live blend fit (ADR 0030) doesn't exist. `blend.fit` has no saved-fit path, and no file keeps per-game predictions.
  - `web/` is empty. Supabase has row-level security on with no policies. The raw cache has no copy outside R2.

The phase delivers §6's list: daily predictions with edge attribution, the paper ledger with Pinnacle and best EU prices, CLV against Pinnacle's fair closing proxy, the dashboard, row-level security, and a raw backup. Its gate is a CLV interval above zero on live games under the frozen policy, before any real stake.

## What this plan does not reopen

- **The frozen policy.** u, the blend's form, selection, staking, the guard and ADR 0030's live inputs (E1's fit, Pinnacle at 12:45, u never reading a confirmation).
  - Live code goes in a new package, `src/nhl_edge/live/`. It calls `betting/`, `market/blend.py`, `game/uncertainty.py` and `market/devig.py` unchanged, and `tests/unit/test_freeze.py` keeps pinning them.
  - A change to the policy would be a new policy version with its own ADR and freeze date, and the CLV count would restart. That includes refitting on live results.
- **u stays frozen,** although ADR 0026's revisit trigger fired. Live evidence decides.
- **Won't-fixes stand:** Codex's P0s on #88, #92 (ADR 0011 tuning), #90, #92, #133 (training on the starters who played) and #155, #159 (E1's fit reads earlier seasons' closes). If Codex repeats one, the reply cites the owner's decision.
- **Seen seasons:** no tuning on 2018-19, 2021-22, 2022-23, 2023-24, 2024-25 or 2025-26. No B2 or B3 change without an ADR.
- **The 16 games played 2026-09-29 to 10-01** are never counted, and no model result on them is read.

## The 2022-23 rebuild catch

ADR 0030 trains the live blend on the E1 out-of-sample predictions of every priced fold: 2018-19 to 2021-22, **plus 2022-23's 342 SBR-priced games**. Three things stand in the way:
1. **The per-game rows don't exist.** 2022-23's predictions were made only inside the one run (`market-validation-20261005-6ec331b`), which kept aggregates and bets but no per-game B2, B3 or u rows. The same holds for every other fold: no file keeps per-game E1 predictions.
2. **The backtest refuses the season.** `nhl backtest` refuses 2022-23 for good: the R2 ledger claim, `walk_forward.run`'s role check, and the hockey-only role check.
3. **The live season's training list leaves it out.** `seasons.blend_training_seasons` returns open seasons only. For 2026-27 it gives 2018-19 to 2021-22.

**The fix is a predict-only path for 2022-23's training rows** (task 2):
- **It predicts:** B2, B3 and u's parts for the 342 games, at E1's moments (puck drop, SBR's close as p_mkt). Each fold fit trains on results public before 2022-23's fold start, exactly as in the one run.
- **It never scores:** no log loss, calibration, gap, bet, CLV or report for 2022-23 is computed or written. The rows go only into the live fit, where each game's result is the training label, as every earlier fold's results are.
- **It is separate from `walk_forward.run`.** The backtest's refusals stay as they are, and `blend_training_seasons` keeps its backtest behaviour. The live fit names 2022-23 explicitly.
- **Tests:**
  - A leakage test that 2022-23's rows read only data public before their prediction times, and that its fits were trained before the fold start.
  - A test that the path's output has no score column and writes nothing under `reports/backtest/`.
- **A check that the rebuild matches the backtest:** the rows of 2018-19 to 2021-22 alone must reproduce the 2022-23 fold's E1 fit recorded by the one run (`reports/backtest/market-validation-20261005-6ec331b.json`: a −0.070, b_m 0.670, b_x 0.571, b_u 0.245, 4,533 games, and its u scale).
- **The owner's ruling (2026-10-05): this is not a second run.** It is recorded in task 2's PR and in the model card's freeze section.

## Tasks

Each task gets an issue in milestone P5, one branch `phase-5/<topic>` and one PR per CLAUDE.md. The session's assigned `claude/…` branch isn't used, because one branch can't hold several PRs. Tasks 1 to 3 are the critical path to the first logged slate. Tasks 1 and 2 can be built at the same time.

### 1. Live targets and their feature rows (`phase-5/live-features`)
- **A `slate` table** (with a pandera schema): the regular-season games scheduled for a game date, from the NHL schedule endpoint, raw-cached with `observed_utc` the fetch time. It adds a venue or neutral-site flag for `schedule_terms`, read from the pre-game boxscore if the schedule response lacks it.
- **Rows for scheduled games:** each feature builder's pure function takes the slate as targets beside the history `games`. The targets stay out of `games`, so the input checks don't flag missing boxscores. The builders:
  - `goalie_starts` (`lineup/goalie_start.py`);
  - `lineups` and `lineup_replacements` (`lineup/projection.py`, `lineup/minutes.py`);
  - `team_strength`, `goalie_effects` and `schedule_terms` (`features/`);
  - `player_ratings` and `rapm_terms` (`ratings/rapm.py`);
  - `expected_power_plays` (`ratings/penalty_rates.py`);
  - `goal_multipliers` (`ratings/finishing.py`).

  Target rows get the same as-of time as history's: the earlier of 10:00 ET and an hour before the start.
- **`nhl live features --date D [--r2]`** first brings the live season's played-game tables up to date (`xg`, `stints`, then the rest, in the handover's order). It then writes D's target rows. It runs in the nightly job after the ingest and replays, from a clean checkout of main.
- **Tests:**
  - **Equivalence:** a played game treated as a target must give the same row as the history path at the same as-of time. This shows live rows are the quantity B3 was trained and tested on.
  - **Leakage, one test per builder** in `tests/leakage/`: adding the game's own boxscore, later stints, later goalie starts or later results doesn't change a target row.

### 2. The live blend fit (`phase-5/live-blend`)
- **Training rows** (ADR 0030):
  - E1's out-of-sample B2 and B3 predictions, with u's parts, for 2018-19 to 2021-22, from the walk-forward's test and training-only folds;
  - 2022-23's 342 games through the predict-only path above.
- **One fit for the season:** u's scale (`uncertainty.fit_scale`), then `blend.fit` for BLEND and its twins BLEND_B2 and BLEND_MARKET, all through the frozen code. B1's E1 recalibration for 2026-27 is fitted on every earlier SBR close.
- **The artifact `blend-live-<yyyymmdd>-<shortsha>`:** weights, standard errors, the u scale (means, sds, u_sd), the training game count `train_cutoff`, the latest time any training row became public, which is before 2026-27's fold start, and the fold start beside it.
  - It is written once to R2, with the write-once claim pattern of `backtest/one_time.py`, and committed under `reports/live/`.
  - It is fitted once for the whole 2026-27 season, like any whole-season fold. Live games never refit it.
- **Checks:** the reproduction of the 2022-23 fold's fit above, the 2022-23 tests above, and a leakage test that every training row was known before the live fold start (hard rule 6).

### 3. `nhl predict` and the paper ledger (`phase-5/predict`)
- **Inputs at the decision time:**
  - today's slate and its feature rows (task 1);
  - Pinnacle's h2h and the best EU book from the stored morning and midday responses, parsed by `parse_odds`, with events matched to games as `nhl odds replay` matches them;
  - the season fits (below) and the live blend artifact (task 2).
- **The season fits:** B1, B2 and B3 for 2026-27, each trained on results public before the season's fold start. They are refit identically on each run, or cached, and every row carries its artifact version and `train_cutoff`.
- **Per game:**
  1. **A fresh decision quote first:** Pinnacle's midday quote must be at most 5 minutes old at the decision, `prediction_utc − last_update_utc` (the owner's rulings, 2026-10-07). The decision instant is fixed when the run starts, about two minutes after the 12:45 snapshot, so the computing time after it doesn't count. Otherwise the game gets a "no fresh price" row and no prediction or bet. An ADR records the limit before the first live run.
  2. B0 is Pinnacle's 12:45 price de-vigged (`market/devig.py`), and B1 its recalibration.
  3. B2 and B3, both mixing over the goalie-start model's likely starters as on history. **No confirmed starter feeds B3 live** (the owner's ruling, 2026-10-05). The blend was fitted on B3 without confirmations, against a close that knew the starters. The ruling is recorded in ADR 0030 and the model card's freeze section (PR #178), before any live prediction, so the live evidence is judged against it. #42's report can still inform a later policy version.
  4. u's parts from the goalie-start model, never a confirmation, then u and u_sd on the live scale.
  5. The blend and its twins.
  6. The frozen `selection.select`.
  7. The frozen `guard.guard` on `guard.live_moves`, Pinnacle's 07:05 against its 12:45 quote on the decision day. Events are mapped to games, since `live_moves` keys on `event_id`.
  8. The frozen `staking.fractions` times the bankroll: 100 units at the season's start plus the profit of earlier logged bets whose results were public before the decision. Bets settle on the full game, OT and shootout included.
- **What is logged:**
  - **Every slate game gets a row:** its prediction, or why it has none: started before the decision, no Pinnacle midday price, no fresh price, or a missing input.
  - **Every prediction row:** p_b0, p_b1, p_b2, p_b3, p_blend and the two twins, u's parts, u and u_sd, the prices used with their `last_update`, `policy_version`, the artifact versions and `prediction_utc`.
  - **Every bet row:** the side, Pinnacle's price and `last_update`, the best EU book and its price and `last_update`, ev, hurdle, stake, and the guard's fields.
- **Write-once proof that the log is pre-game:**
  - Each date is written to R2 as `ledger/live/<date>.parquet` with `IfNoneMatch="*"`, so it can't be rewritten. R2's server timestamp and the Actions run log date it.
  - The command refuses to write if `prediction_utc` is at or after any predicted game's start.
  - The lake's `predictions` and `paper_bets` tables (schemas in `lake/schemas.py`) are rebuilt from those files.
- **The decision time** is the run's clock once the midday snapshot is stored, about 12:47 ET. It is one decision time per day (`staking.check_days`). **No late runs or late snapshots** (the owner's rulings, 2026-10-07): the midday snapshot's `snapshot_utc` and the decision must both fall between 12:45 and 13:15 ET, the scheduled slot plus 30 minutes. A fallback snapshot hours late keeps the `midday` label (`resolve_slot`), so the check reads the time, not the label. Outside the window the day is logged as skipped, and it is never reconstructed.
- **The workflow:** a `predict` job in `odds-snapshots.yml`, midday slot only, with R2 secrets and a timeout of about 30 minutes.
  - It runs after the snapshot job whatever that job's outcome (`needs: snapshot` with `if: always()`). A failed goalie poll in the same job doesn't skip it.
  - When the midday odds are missing, it logs the day as skipped. The Cloudflare timer is unchanged. Plan §8's "predict-daily 16:00 UTC" gets updated.
- **Tests:**
  - Leakage: later snapshots, later boxscores, goalie polls after the decision, or results change nothing.
  - The write-once refusal.
  - The freeze test's numbers are untouched. A new freeze test changes a starter confirmation made *before* the decision and checks that live p_b3 and u don't move (ADR 0030).
  - The time window and the freshness limit: a late snapshot, a late run and a stale quote are each refused or skipped.
  - A `--dry-run` that writes only locally.
- **Before going live:** a dry run on the next slate, with its output read for sanity (coverage, prices matched, stakes within caps). It reads no results.

### 4. The closing proxy (#21)
- **A freshness threshold** for the closing quotes, fixed with the owner in the same ADR as task 3's decision-quote limit, **before the first counted prediction**. It is chosen from quote ages alone, never from live closes or CLV, since it decides which bets enter the CLV gate. It starts from task 3's 5 minutes.
- **`is_closing_proxy` for h2h:** derived in the odds replay itself, since the replay rebuilds the lake from raw and would wipe a flag stored apart. Supabase is upserted on `ODDS_KEY`.
- **The lead-time report** in the audit. Matinees get only the midday snapshot.

### 5. Settlement and CLV (`phase-5/settle`)
- **Nightly, after results are public:** each paper bet's win and profit on the full game, its CLV = o_taken · p_fair_close − 1 against Pinnacle's proxy (stale quotes excluded), and its fair move.
- **Reuse `backtest/e3.closing_value`** by passing it a closes frame. Its SBR path stays identical.

### 6. The live report and the daily slate (`phase-5/live-report`)
- **`nhl live report`**, committed weekly under `reports/live/`, with weekly block bootstrap intervals throughout (hard rule 7):
  - CLV against Pinnacle's proxy (the gate);
  - BLEND minus B1 on live E2;
  - B3 minus B2 (hard rule 3) and BLEND minus BLEND_B2;
  - calibration;
  - every gap above 8 points listed for hand review (hard rule 8);
  - the 20% drawdown check, which triggers a review of data and code, never a model change;
  - ADR 0030's revisit checks: how often live u falls outside its training range, and whether Pinnacle at 12:45 sits nearer SBR's opener or close, by B0's log loss.
- **Profit** is reported once a season only (§11).
- **The `daily-slate` skill** is filled in to run `nhl predict` output and list flags and lineup gaps.

### 7. Edge attribution (#154), then attribution in the daily slate
This is reporting only: nothing in the policy reads it.

### 8. Supabase ledger, row-level security and the dashboard (`phase-5/supabase-ledger`, then `phase-5/dashboard`)
- **Migrations** for `predictions` and `paper_bets`:
  - insert-only, with a server-side `created_at default now()`;
  - a check that `prediction_utc < start_utc`;
  - the service role alone may update the settlement columns.

  They are backfilled from the R2 log.
- **Row-level security:** select policies for the owner's signed-in user only, the service role key server-side only, and anon still sees nothing.
- **The dashboard:** Next.js in `web/`, deployed on Vercel. It shows the day's slate, the ledger, cumulative CLV with its interval, and calibration.

### 9. A raw backup outside R2 (`phase-5/raw-backup`)
A second copy of `raw/` (about 1.1 GB), refreshed weekly. The owner picks where it goes when the task starts.

**Later, not on phase 5's path:**
- #156, the blend gap screen.
- #67, `EXPECTED_GAMES`, before April 2027. Live code must not call `nhl backtest`'s count check for 2026-27 meanwhile.
- #121, injury sources. They are live-only, and any use needs an ADR.

## Timing: what the first days cost

The first decision is 2026-10-06 at 12:45 ET (16:45 UTC). Tasks 1 to 3, each with CI and a Codex round, realistically put the first logged slate at **about 10-07 to 10-09**. Games from 10-06 until then are lost to the live test, and are not reconstructed.
- To keep the loss small, tasks 1 and 2 are built in parallel.
- Tasks 4 to 9 cost no games if they come later: the prices, polls and results they read are already logged.

## The daily schedule (EDT; from 2026-11-01 the odds slots keep their ET times, while the nightly stays at 09:00 UTC)

| ET | UTC (EDT) | Job |
| --- | --- | --- |
| 05:00 (04:00 EST) | 09:00 | Nightly: ingest, recheck, odds and goalie replays, then `nhl live features --date today`, settlement (task 5), and the closing proxy (task 4) |
| 07:05 | 11:05 | Morning snapshot (the guard's reference) |
| 12:45 | 16:45 | Midday snapshot, then the `predict` job logs the day's predictions and paper bets |
| 18:45 to 21:45 | 22:45 to 01:45 | Pre-game snapshots (the closing proxy) |

**Actions minutes:** the repo is private, with 2,000 free minutes a month. The new nightly steps and the predict job may add 600 to 900 minutes. I'll measure them in the first week and tell you before the limit is near.

## Dated items alongside

- **2026-10-13 (#9, #42):**
  1. Bring the local lake up to R2 with the commands `nhl status` prints.
  2. Run `nhl audit report` and commit it to `reports/audit/`.
  3. Review it as the comment on #9 says: an issue in a milestone for each problem, and an ADR for any dropped season or source.
  4. Answer #42 on the NHL's starter flag.
- **2026-10-22 (#30):** review the corrections section of `nhl audit report`. If corrections change more than scorer credit on more than a few games a season, reopen ADR 0004 with the owner.
- **Then gate 1 (#79, draft PR #93):**
  1. ADR 0014 with the owner, from the outline on #79.
  2. `/leakage-check` and `/run-backtest` if #30's corrections touch a table B2 or B3 reads.
  3. Merge main into the branch, taking main's handover and model card.
  4. Write the model card's gate 1 entry, mark the PR ready, triage Codex, merge.
  5. #11 is done then.
- **Reminders:** after this plan is approved, I'll schedule one-shot reminders into this session for 2026-10-13 and 2026-10-22, 10:30 UTC, after the nightly and after results go public.

## The owner's word needed

**Already given, 2026-10-05:**
- 2022-23's predict-only path is not a second run.
- Live B3 uses no confirmed starter.

**Given 2026-10-07, on Codex's review of PR #178:**
- The live B3 ruling is recorded in ADR 0030, under the same policy version, before any live prediction.
- A run refuses to decide more than 30 minutes after the midday snapshot.
- Anchored to the scheduled slot: the midday snapshot and the decision must both fall between 12:45 and 13:15 ET.
- Pinnacle's decision quote must be at most 5 minutes old, or the game gets a "no fresh price" row.
- That age is measured at the decision (`prediction_utc − last_update_utc`), with the decision instant fixed at the run's start.
- The closing proxy's freshness threshold is fixed in the same ADR, before the first counted prediction, from quote ages alone.

**Still to come:**
1. **Task 4's freshness threshold:** an ADR, when that task starts.
2. **Task 8:** apply the Supabase migrations and create the Vercel project and its variables. This session has no Supabase credentials.
3. **Task 9:** where the backup goes.

## Verification

- **Each PR** runs `uv run ruff check . && uv run pyright` and `uv run pytest -m "not slow"`, plus the slow tests it touches. `/leakage-check` runs on tasks 1 to 3 and 5. Codex is triaged under the review budget.
- **Task 1:** the equivalence test of target rows against history's rows.
- **Task 2:** the reproduction of the 2022-23 fold's fit from the one run's JSON.
- **Task 3:** a `--dry-run` on a real slate before the first live run, then the first live run's log read back from R2. The claim must refuse a second write.
- **Afterwards:** the first week's live report shows every slate game with a prediction or a reason. Bets carry Pinnacle's and the best EU book's `last_update`, and after task 5 every settled bet has its CLV.
