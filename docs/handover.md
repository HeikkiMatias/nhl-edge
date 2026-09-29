# Handover, 2026-09-29

Where the build stands at the end of the second phase 1 session, and how to pick it up on a fresh machine, such as a Claude Code cloud session. CLAUDE.md holds the rules. This file holds the state. Update or delete it when it goes stale.

## State

- **Lake (R2, 0.68 GB):** every regular-season game from 2010-11 to 2025-26 (19,152).
  - `games` and `schedule` (ADR 0003 and 0005), `players`
  - the per-game tables `shots`, `shifts`, `actual_lineups` and `shift_coverage` (#5, ADR 0004)
  - `odds_snapshots`, every stored Odds API snapshot from 2026-09-28 on, matched to NHL games (#20)
- **Reference files (`src/nhl_edge/reference/`, #8 and #26):** team codes, 85 arenas with coordinates and time zones, every NHL venue name, home arenas by season, head coach stints, and attendance limits for 2020-21 and the 2021-22 Omicron months in Canada.
  - Sources and conventions are in docs/data-sources.md, "Reference files".
  - `nhl audit reference` checks the files against every game in `games`: 0 problems over all 19,152.
  - Features read coach stints through `coaches_known_at` and attendance through `capacity_share`, never the CSVs. Both are point in time, and tests in `tests/leakage/` hold them to it.
- **Raw cache (R2 `raw/`):** about 61,700 responses. The laptop's `data/raw/` is the second copy (`nhl lake sync-raw` and `restore-raw`, #27).
- **Nightly workflow (09:00 UTC):** ingest of the last 3 days, then the odds replay of the last 14 days, the contract test on a golden game (#6), and the R2 size check.
- **Odds snapshots:** five slots a day, into Supabase and raw R2.
- **Tests:** golden games in `tests/golden/` (owner-written only). Leakage tests per table in `tests/leakage/`.
- **Closed in P1:** #4, #5, #6, #8, #20, #24, #25, #26, #27, #29.

## Next: the rest of P1, in this order

1. **#7 SBR odds archive, 2010-11 to 2022-23.**
   - The archive is at https://www.sportsbookreviewsonline.com/scoresoddsarchives/nhl/nhloddsarchives.htm. The site returns 404 unless the request sends a browser User-Agent.
   - Each season has its own page, such as `/scoresoddsarchives/nhl-odds-2010-11/`. 2020-21 is `nhl-odds-2021`.
   - Not checked yet: whether those pages still offer the Excel files or only HTML tables. Check first.
   - Download once, and keep the files raw under `raw/sbr/` in R2 like every other source.
   - Map team names, and match to `game_id` through `schedule`.
   - The issue's open question, the observed_utc convention for opening lines, needs an ADR. Ask the owner first.
2. **#9 Data audit report** into `reports/audit/`:
   - missing or duplicate games
   - SBR vig, open against close, and mapping errors
   - the shift coverage summary (`nhl audit shifts`)
   - the reference files (`nhl audit reference`)
   - live snapshot health from the lake's `odds_snapshots`
   - whether NHL pre-game data confirms starting goalies early enough (plan §10)
3. **#10 De-vig and B0/B1, the phase 1 gate.** The two regression cases moved from #6 are in the comment on #10. Its open question is whether to inspect 2022-23 at all.

## Keep an eye on

- **#20's done-when:** the opener's odds matched to games. After the first nightly run following 2026-09-29, the `nhl odds replay` line in its log should show the opener's events matched.
- **#28 (P2):** the strength source where `situationCode` drifts. It is needed before phase 2 uses `shots.strength`.
- **#30 (P2):** measure post-game corrections over two weeks of 2026-27 games, before the first B2 backtest.
- **#21 (P5):** the closing proxy should key on the Odds API event and its commence time, not `game_id` (leakage check on #20).
- **Reference upkeep in 2026-27:**
  - At a coaching change, end the old stint at its last game in `coaches.csv` and add the new one.
  - When the NHL uses a new venue name, add it to `venues.csv`, and the building to `arenas.csv` if it is new.
  - `nhl audit reference` flags both once the nightly ingest adds the game.
  - The five new 2026-27 coaches and the season's five new venue names are already in.
- **`EXPECTED_GAMES` has no 2026-27 entry.** The NHL's 2026-27 schedule has 1,344 regular-season games (84 per team). Without the entry, two checks quietly skip 2026-27: a season ingest's game count, and the reference checks that need a finished season. Add it with the count the schedule holds when the season ends.

## Continuing in a cloud session

1. **Environment.** On claude.ai/code, select the cloud icon in the row above the message box, then **Add cloud environment**. The dialog holds the name, network access, environment variables and setup script.
   - **Environment variables**, one `KEY=value` per line with names as in `.env.example`: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_BUCKET`.
     - Anyone who uses the environment can read them, so give it its own revocable R2 token, scoped to this bucket.
     - Leave the Supabase and Odds API keys out: none of the remaining P1 issues writes to them.
   - **Network access:** **Full** is simplest. For something tighter, pick **Custom**, keep the default allowlist (it has `pypi.org` and `files.pythonhosted.org`, which `uv sync` downloads from, and GitHub) and add `*.r2.cloudflarestorage.com`, `api-web.nhle.com`, `api.nhle.com` and `www.sportsbookreviewsonline.com`.
     - In a cloud session, Wikipedia's API and Wikidata's query service rate-limit requests. Plain Wikipedia article pages, the NHL records API (`records.nhl.com/site/api`) and web search work.
   - **Setup script:** none. Python 3.12 (Ubuntu 24.04), uv and `gh` are pre-installed. The SessionStart hook's `uv run` creates the virtualenv on the first start. If that times out, run `uv sync`.
   - **GitHub:** no token needed. `gh` authenticates through the session's GitHub proxy, provided the Claude GitHub App is installed on the repository. Leave **Auto-fix** off on PRs: it answers review comments itself, bypassing the review budget and the owner's calls on P0s.
2. **Data.** #7, #9 and #10 need the lake.
   - First run `uv run nhl lake restore-raw` (about 1.1 GB), then `uv run nhl ingest --seasons 20102011-20252026 --replay` (no network) and `uv run nhl odds replay`. Together they took about 80 minutes in a cloud session. Run them in the background and work meanwhile.
   - `uv run nhl status` should then say "up to date with R2".
   - A lake table can also be read straight from R2 into memory (`list_keys` and `get_object`), which is enough for a check over `games` or `schedule`.
3. **Start.** Use this prompt:

   > Read CLAUDE.md and docs/handover.md. Continue P1 with #7: start in plan mode, then branch, PR, review triage and merge as CLAUDE.md describes. Then #9 and #10 in that order. Stop and ask me before any ADR, any won't-fix on a P0, or anything outside an issue's scope.
