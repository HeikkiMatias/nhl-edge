# Handover, 2026-09-29

Where the build stands at the end of the first phase 1 session, and how to pick it up on a fresh machine, such as a Claude Code cloud session. CLAUDE.md holds the rules. This file holds the state. Update or delete it when it goes stale.

## State

- **Lake (R2, 0.68 GB):** every regular-season game from 2010-11 to 2025-26 (19,152).
  - `games` and `schedule` (ADR 0003 and 0005), `players`
  - the per-game tables `shots`, `shifts`, `actual_lineups` and `shift_coverage` (#5, ADR 0004)
  - `odds_snapshots`, every stored Odds API snapshot from 2026-09-28 on, matched to NHL games (#20)
- **Raw cache (R2 `raw/`):** about 61,700 responses. The laptop's `data/raw/` is the second copy (`nhl lake sync-raw` and `restore-raw`, #27).
- **Nightly workflow (09:00 UTC):** ingest of the last 3 days, then the odds replay of the last 14 days, the contract test on a golden game (#6), and the R2 size check.
- **Odds snapshots:** five slots a day, into Supabase and raw R2.
- **Tests:** golden games in `tests/golden/` (owner-written only). Leakage tests per table in `tests/leakage/`.
- **Closed in P1:** #4, #5, #6, #20, #24, #25, #27, #29.

## Next: the rest of P1, in this order

1. **#8 Static reference files.** Arenas with coordinates and time zones, team code history, coach tenures.
   - Team codes: ATL moved to WPG in 2011-12, PHX became ARI in 2014-15, VGK joined in 2017-18, SEA in 2021-22, and ARI became UTA in 2024-25.
   - Sourced CSVs in the repo. Check that every `schedule.venue` maps to an arena and every 2010-26 team code maps.
2. **#26 Attendance limits by venue and date.** A sourced file next to #8's arenas. It covers 2020-21 US arenas with capped crowds and 2021-22 Canadian limits (about December 2021 to February 2022), with a per-game flag or capacity share derived from it.
3. **#7 SBR odds archive, 2010-11 to 2022-23.**
   - Download the free Excel files once, and keep them raw under `raw/sbr/` in R2 like every other source.
   - Map team names, and match to `game_id` through `schedule`.
4. **#9 Data audit report** into `reports/audit/`:
   - missing or duplicate games
   - SBR vig, open against close, and mapping errors
   - the shift coverage summary (`nhl audit shifts`)
   - live snapshot health from the lake's `odds_snapshots`
   - whether NHL pre-game data confirms starting goalies early enough (plan §10)
5. **#10 De-vig and B0/B1, the phase 1 gate.** The two regression cases moved from #6 are in the comment on #10. Its open question is whether to inspect 2022-23 at all.

## Keep an eye on

- **#20's done-when:** the opener's odds matched to games. After the first nightly run following 2026-09-29, the `nhl odds replay` line in its log should show the opener's events matched.
- **#28 (P2):** the strength source where `situationCode` drifts. It is needed before phase 2 uses `shots.strength`.
- **#30 (P2):** measure post-game corrections over two weeks of 2026-27 games, before the first B2 backtest.
- **#21 (P5):** the closing proxy should key on the Odds API event and its commence time, not `game_id` (leakage check on #20).

## Continuing in a cloud session

1. **Environment.** On claude.ai/code, select the cloud icon in the row above the message box, then **Add cloud environment**. The dialog holds the name, network access, environment variables and setup script.
   - **Environment variables**, one `KEY=value` per line with names as in `.env.example`: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_BUCKET`.
     - Anyone who uses the environment can read them, so give it its own revocable R2 token, scoped to this bucket.
     - Leave the Supabase and Odds API keys out: none of the remaining P1 issues writes to them.
   - **Network access:** **Full** is simplest, since #8 and #26 need web research. For something tighter, pick **Custom**, keep the default allowlist (PyPI, GitHub) and add `*.r2.cloudflarestorage.com`, `api-web.nhle.com`, `api.nhle.com` and `www.sportsbookreviewsonline.com`.
   - **Setup script:** none. Python 3.12 (Ubuntu 24.04), uv and `gh` are pre-installed. The SessionStart hook's `uv run` creates the virtualenv on the first start. If that times out, run `uv sync`.
   - **GitHub:** no token needed. `gh` authenticates through the session's GitHub proxy, provided the Claude GitHub App is installed on the repository. Leave **Auto-fix** off on PRs: it answers review comments itself, bypassing the review budget and the owner's calls on P0s.
2. **Data.** Issues #8 and #26 need no lake data. #7, #9 and #10 do. First run `uv run nhl lake restore-raw`, then `uv run nhl ingest --seasons 20102011-20252026 --replay` (no network, about 15 minutes) and `uv run nhl odds replay`. `uv run nhl status` should then say "up to date with R2".
3. **Start.** Use this prompt:

   > Read CLAUDE.md and docs/handover.md. Continue P1 with #8: start in plan mode, then branch, PR, review triage and merge as CLAUDE.md describes. Then #26, #7, #9 and #10 in that order. Stop and ask me before any ADR, any won't-fix on a P0, or anything outside an issue's scope.
