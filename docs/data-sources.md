# Data sources

The fixed list of sources and endpoints. Work from this file instead of guessing URLs, and add new sources with the `add-data-source` skill. Copied from docs/plan.md section 3 on 2026-09-28.

## Sources

| Source | What it gives | Access and limits | Role in the model |
| --- | --- | --- | --- |
| [NHL API](https://github.com/Zmalski/NHL-API-Reference) (api-web.nhle.com, api.nhle.com/stats/rest) | Schedule, play-by-play with shot coordinates, boxscores, shift charts, rosters, player bios and career stats, draft picks, team prospects | Free, unofficial and undocumented, no published rate limit; throttle to about 1 request per second and cache every response | Backbone: shifts for RAPM, shots for xG, rosters, schedule for rest and travel |
| [MoneyPuck data](https://moneypuck.com/data.htm) | Shot-level data with xG for 2007 to 2026, skater, goalie, team and line stats by season and game, player bios | Free CSV downloads for non-commercial use, attribution required, no scraping | Sanity check for the simple in-house xG model and player stats; not a backtest input, since its xG was likely fitted across many seasons |
| [The Odds API](https://the-odds-api.com/) | Live NHL moneyline, puck line and totals from EU books including Pinnacle; props per event | Free: 500 credits a month. Paid from $30 a month for 20,000 credits. Historical odds from 2020, paid plans only, 10 credits per market per region ([docs](https://the-odds-api.com/liveapi/guides/v4/)) | Live odds snapshots from day one on the free tier; historical backfill deferred to v2 |
| [SBR odds archive](https://www.sportsbookreviewsonline.com/scoresoddsarchives/nhl/nhloddsarchives.htm) | Opening and closing moneylines, puck lines and totals, 2007-08 to 2022-23 | Free Excel files, no longer updated, book source not stated | Opening lines for the historical tradable test (E2), closes for the information test (E1); quality unknown until the phase 1 audit |
| [Elite Prospects API](https://developer.eliteprospects.com/) | Player stats across 900+ leagues including Liiga, SHL, CHL and AHL, draft and transfer history | Free Explorer tier: 1,000 calls a month, 10 per minute; current season free, history is a one-off purchase | Prospect and non-NHL stats for NHLe priors, used sparingly |
| NHLe research ([Bacon](https://towardsdatascience.com/nhl-equivalency-and-prospect-projection-models-building-the-nhl-equivalency-model-part-2-6f275a45e22/), [HockeyStats](https://hockeystats.com/methodology/nhle)) | Published league translation factors and method | Free articles | Starting coefficients for offensive priors only, refit later on your own data |
| [Daily Faceoff](https://www.dailyfaceoff.com/starting-goalies) | Projected lines, power play units, starting goalie status with report time and source | Web pages only; robots.txt allows them and disallows `/api/`; no terms of use found (2026-09-29) | Starting goalies logged at every goalie poll (#48), compared with the NHL's in the audit; other pages manual reference only |
| [RotoWire](https://www.rotowire.com/hockey/starting-goalies.php) | Projected lines and starting goalies | Terms forbid scraping and any automated access without written consent | Manual reference only; never automated |
| Static files you create | Arena coordinates and time zones, team abbreviation history, coach tenures | One-off CSVs in the repo | Travel, rest and coach-change features |

## Endpoints

```text
NHL API (api-web.nhle.com)
  GET /v1/schedule/{YYYY-MM-DD}
  GET /v1/gamecenter/{gameId}/play-by-play
  GET /v1/gamecenter/{gameId}/boxscore
  GET /v1/gamecenter/{gameId}/landing     # pre-game goalie poll only
  GET /v1/gamecenter/{gameId}/right-rail  # pre-game goalie poll only

Daily Faceoff (www.dailyfaceoff.com), HTML page, never /api/
  GET /starting-goalies/{YYYY-MM-DD}      # US Eastern date
  GET /v1/roster/{team}/{season}          # season as 20252026
  GET /v1/player/{playerId}/landing       # bio + career stats by league
  GET /v1/player/{playerId}/game-log/{season}/{gameType}
  GET /v1/draft/picks/{season}/all
  GET /v1/prospects/{team}

NHL stats API (api.nhle.com/stats/rest)
  GET /en/shiftcharts?cayenneExp=gameId={gameId}

The Odds API (api.the-odds-api.com)
  GET /v4/sports/icehockey_nhl/odds?regions=eu&markets=h2h,spreads,totals&oddsFormat=decimal
  GET /v4/historical/sports/icehockey_nhl/odds?date={ISO8601}   # paid plans
```

## Access rules

- Throttle the NHL API to about 1 request per second and cache every response. Raw responses are stored untouched as JSON under `data/raw/` locally and under the `raw/` prefix in the R2 lake, so parser bugs can be fixed and replayed without calling the API again.
- The NHL API is unofficial and changes without notice. A nightly contract test on one golden game catches schema drift.
- The NHL API has no end-of-game time. A result counts as public at 10:00 UTC the morning after its game date, and at least six hours after its start (`games.observed_utc`, ADR 0003).
- Shift chart coverage varies for older seasons. Phase 1 checks coverage per season before RAPM depends on it.
- The Odds API free tier has 500 credits a month. A call costs 1 credit per market per region and returns every game. The slot plan uses about 300 credits a month. Store `last_update` with every quote.
- Many EU books (13 of 20 on 2026-09-28, among them Marathonbet, Unibet, Betclic and 1xBet) quote the 3-way regulation line under the Odds API `h2h` key, with a `Draw` outcome. The parser stores those quotes as `h2h_3_way`, so `h2h` only holds the two-way moneyline including OT and the shootout. Pinnacle, Betsson and NordicBet quote the two-way line.

## NHL ingest

`nhl ingest` fills the lake's `games` and `players` tables, caches each game's raw feeds and parses them into the per-game tables below. A window is `--seasons 20102011-20252026` (a range or a comma list), `--start D --end D`, or `--recent N` (the last N US Eastern game dates up to yesterday). For each week of the window, it fetches the schedule and keeps the final (`OFF`) regular-season games. It fetches play-by-play, boxscore and shift chart for each of them and parses them (`--no-feeds` skips both and leaves the per-game tables alone), then the roster of every team that played, then the landing page of every player on those rosters or in those boxscores who is not yet in `players`. A season window first fetches the schedule at February 15 of its second year to read `regularSeasonStartDate` and `regularSeasonEndDate`. Games that are not final are skipped with a warning, and a later run picks them up. Playoffs are out of scope for v1.

One throttle at about 1 request per second covers both NHL hosts. Timeouts, 429 and 5xx are retried 3 times with backoff of 5, 10 and 20 s (longer when the API sends `Retry-After`); a 404 is not retried.

Raw responses go to `data/raw/nhl/<kind>/<entity>/<fetch stamp>.json.gz`, with a `.meta.json` sidecar (URL, status, fetch time, attempts), and with `--r2` they are mirrored to `raw/nhl/` in R2. A response counts only once its sidecar exists; the body is written first, so a body alone is an interrupted write. When a response is missing locally and `--r2` is set, the newest complete copy under R2 `raw/` is downloaded instead, so the nightly runner reuses what earlier runs stored. The newest copy is reused when its rule allows it, so a stopped backfill restarts where it left off:

| Kind | Entity | Cached copy reused when |
| --- | --- | --- |
| `schedule` | `{date}` (a week from that date) | every regular-season game on the days used is final; the season-bounds probe once it was fetched after the season ended |
| `play-by-play`, `boxscore` | `{season}/{game_id}` | always (fetched only for final games) |
| `shiftcharts` | `{season}/{game_id}` | two teams each have valid shifts of this game adding up to at least four players' full period in each of periods 1 to 3, or it was fetched 3 days or more after the game. The NHL sometimes publishes a chart late or cut short, so the nightly lookback refetches it. The 3 days match the lookback (`--recent 3`, which a test ties together), and after that a gap counts as the source's. |
| `roster` | `{season}/{team}` | it was fetched after the team's last ingested game was observed |
| `player-landing` | `{player_id}` | always (bio and draft facts do not change) |

Two cached responses reflect later knowledge, so neither may feed a point-in-time input:
- A past season's roster is an after-the-fact view. It only finds player ids for `players` and must never feed a lineup (hard rule 9).
  - Up to 2022-23 it lists everyone who played for the team that season (31 to 44 players).
  - From 2023-24 on it holds only a current-style roster (17 to 30). ARI 2023-24 is empty, since the franchise moved.
  - The boxscores fill these gaps, which is why player ids come from both.
- A boxscore fetched years later includes post-game stat corrections. It may be used only after its game's `observed_utc`.

The landing page's `position` is today's, so it stays out of `players`. The boxscore's position code is today's too: Brent Burns is listed `D` in the forwards group through his 2013-14 season at forward. The group a player is listed in (forwards, defense, goalies) is his role in that game, and `actual_lineups.role` comes from it.

`--replay` reads only the local raw cache, never the network, and fails on a miss. It re-parses every player, so a parser fix reaches old rows. The odds job's schedule check always fetches fresh.

**Raw cache copies.** R2 `raw/` is the primary copy, and the laptop's `data/raw/` is the second copy outside R2 that plan §10 asks for. The raw cache holds responses that cannot be fetched again if the API changes.
- The nightly ingest and the odds snapshots run on GitHub Actions and write only to R2. `nhl lake restore-raw` downloads every complete response missing locally, and never overwrites a local file. Run it weekly and before any replay, so the laptop keeps up.
- A response cached without `--r2`, or whose upload failed, stays local until `nhl lake sync-raw` uploads it.
- Both commands take `--prefix` (such as `odds/`) and skip incomplete responses (a body without its sidecar) on either side.

Lake tables live in `data/lake/<table>/`, and with `--r2` they are mirrored to `lake/<table>/` in R2. `games` is partitioned as `season=S/game_date=D/part-0.parquet`. So is `schedule`, which holds the pre-game facts of the same games (teams, start, venue, neutral site, limited attendance) and no result columns. A game's schedule is public 24 hours before its start (ADR 0005), and its result the morning after (ADR 0003). Features read `schedule` through `schedule_known_at`, which returns the games whose result is already public plus the games being predicted. Other games stay hidden, because the table holds only games that were played: a missing or delayed game would reveal how it turned out. A game re-timed at short notice gets a later public time (`SCHEDULE_PUBLIC_OVERRIDES`; the Lake Tahoe game PHI at BOS, 2021-02-21). Anything about outcomes reads `games` through `results_known_at`. An ingest makes its final games the whole content of the window's dates: it replaces their partitions and deletes a partition that no longer has games, locally and in R2. `players` is one file. `--supabase` upserts the games into Supabase `games` and ends with one small read, which keeps the free project from pausing.

`.github/workflows/ingest-nightly.yml` runs `nhl ingest --recent 3 --r2 --supabase` at 09:00 UTC (04:00 or 05:00 ET, when every game of the night is final). Then comes the contract test (`tests/contract/`): one golden game from `tests/golden/`, fetched live, must parse into exactly the tables its frozen copy gives, or the run fails. Last is `nhl lake size --max-gb 8`, which fails the run above 8 GB. R2 is free up to 10 GB and has no spending cap. The three-day lookback picks up a game that was not final yet and a missed night. The earlier days come back from R2, not the NHL API. Older gaps are caught up with a manual run over a date range.

Regular-season games per season, which a season window checks against:

| Seasons | Games | Note |
| --- | --- | --- |
| 2010-11, 2011-12 | 1,230 | 30 teams |
| 2012-13 | 720 | lockout, 48 games |
| 2013-14 to 2016-17 | 1,230 | |
| 2017-18, 2018-19 | 1,271 | VGK joins |
| 2019-20 | 1,082 | paused on 2020-03-11, the rest cancelled |
| 2020-21 | 868 | 56 games, limited attendance |
| 2021-22 to 2025-26 | 1,312 | SEA joins |

The 16 seasons come to 19,152 games. With the feeds this is about 64,000 requests, roughly 18 hours at 1 request per second, and about 0.65 GB gzipped in R2.

## Per-game tables

`nhl ingest` parses each final game's three feeds into four lake tables, partitioned like `games` (`season=S/game_date=D/`) and replaced a whole game date at a time. `--replay` rebuilds them from the raw cache. The parsers are in `src/nhl_edge/ingest/` (`shots.py`, `shifts.py`, `lineups.py`, `shift_coverage.py`), and the schemas in `lake/schemas.py` document every column.

| Table | From | Grain | What it holds |
| --- | --- | --- | --- |
| `shots` | play-by-play | one unblocked attempt | shots on goal, missed shots and goals in periods 1 to 4, with time, shooter, goalie, coordinates, shot type and strength |
| `shifts` | shift chart | one player shift | team, period and start and end in elapsed game seconds |
| `actual_lineups` | boxscore | one dressed player | role (F, D, G), sweater number, starting goalie and time on ice |
| `shift_coverage` | all three | one game | how far the shift chart can be trusted (below) |

Every row counts as public at 10:00 UTC the morning after its game date, like the game's result (ADR 0003). A game's own shots, shifts and lineup never feed a prediction for it, and backtest lineups come only from earlier games' boxscores (hard rule 9). The backfilled feeds were fetched years after the games and include post-game corrections, which live does not see. The tables leave out scoring credits, but corrections can still change kept values: a goal's scorer, a shot record, time on ice. ADR 0004 accepts this small look-ahead, and #30 measures it.

Conventions:
- Times are elapsed game seconds: (period − 1) × 1200 plus the period clock. Overtime is period 4 and lasts 300 seconds. The shootout is not play and has no rows.
- `situationCode` has four digits: away goalie in net, away skaters, home skaters, home goalie in net. A pulled goalie shows as 6 skaters. `shots` turns it into the shooting team's view: `skaters_for`, `skaters_against`, `strength` (such as `5v4`) and `is_empty_net`. It is missing for 22 shots in two games (2010020124, 2013020971), which leaves their strength null.
- Penalty shots are 1 skater against 0 with the defending goalie in. `shots` keeps and flags them (`is_penalty_shot`).
- Coordinates are rink feet, turned so the shooting team attacks the net at x = +89 (y turns with x). Before 2019-20 the feed does not say which end a team attacks. The direction is inferred per team and period from the median x of its offensive-zone shots, and it agreed with `homeTeamDefendingSide` in all 954 team-periods checked (2019-20 onward). A few old plays have a zone code that contradicts their coordinates.
- A player is on the ice for an event at second t when `start_s < t <= end_s`.
- Boxscores flag one starting goalie per team in every game from 2010-11 on. Teams dress 17 to 21 players.

Shift chart rows the parser leaves out, counted in `shift_coverage`:

| Count | Rows | Seen in |
| --- | --- | --- |
| `dropped_rows` | zero-length shifts, shootout (period 5) rows, and repeats of a kept shift (same player, period and times, under the same or another shift number) | blank-ended `00:00` placeholders in 174 games of 2019-20; 2022020041 lists 14 shifts twice; many 2023-24 charts repeat a shift under the next shift numbers |
| `foreign_rows` | teams not in the game | 2021020513 (WSH at NYI) lists every shift twice and STL and MIN shifts besides |
| `bad_rows` | malformed times, shifts outside their period, a shift number repeated with other times | |

The API returns an empty shift chart for 57 games, 2024021235 to 2024021291 (2025-04-08 to 2025-04-15). A live request on 2026-09-29 still gave none, so those games have no shifts and count as incomplete. No other game from 2010-11 on has an empty chart. Those copies were fetched long after their games, so they count as settled and are not fetched again.

A game's chart is `complete` when it has no bad rows and every dressed player's shifts add up to his boxscore time on ice within 60 seconds (a missing boxscore time counts as not adding up). At every unblocked shot except penalty shots, the players on the ice by the chart are also compared with `situationCode`, and `skater_mismatches` and `goalie_mismatches` count where they differ. RAPM drops the stints that contradict the strength state. `nhl audit shifts` prints the per-season summary, which is reviewed before RAPM depends on the charts.

## Odds snapshots

`nhl odds snapshot` runs from `.github/workflows/odds-snapshots.yml`. Slots are set in US Eastern time. The workflow has one cron line per slot for each UTC offset, and the CLI maps the line that fired to a slot for the current offset, so nothing changes by hand when DST starts or ends. A run first checks the NHL schedule and makes no Odds API call when the slot has no regular-season or playoff game.

| Slot | ET | UTC in EDT / EST | Markets | Runs when |
| --- | --- | --- | --- | --- |
| morning | 07:05 | 11:05 / 12:05 | h2h, spreads, totals | a game today (ET) has not started |
| midday | 12:45 | 16:45 / 17:45 | h2h, spreads, totals | a game today (ET) has not started |
| pre7 | 18:45 | 22:45 / 23:45 | h2h, totals | a game starts within 90 minutes |
| pre8 | 19:45 | 23:45 / 00:45 | h2h | a game starts within 90 minutes |
| pre10 | 21:45 | 01:45 / 02:45 | h2h | a game starts within 90 minutes |

At most 10 credits a game day. Each response is stored raw as `data/raw/odds/<date>/<snapshot>_<slot>_<regions>.json.gz` with a `.meta.json` sidecar (parameters without the key, credit headers), mirrored to `raw/odds/` in R2. Supabase `odds_snapshots` gets only pre-game quotes for games starting within 36 hours of the snapshot; the raw files keep every listed game for the lake. Games starting before 18:45 ET (weekend matinees) get the midday snapshot as their last pre-game quote.
**Odds history in the lake.** `nhl odds replay` parses the stored raw snapshots into the lake's `odds_snapshots` table, partitioned by snapshot date (UTC). It never calls the Odds API.
- Each quote keeps its snapshot time, so `available_at` filters the history as it does the live window. `h2h` (two-way, full game) and `h2h_3_way` (regulation) stay apart.
- Only pre-game quotes go into the history, as in Supabase. The Odds API also lists games under way, with live prices that move with the score, and the replay report counts those it leaves out. The raw responses keep everything.
- Each event gets the NHL `game_id` and `game_type` of the schedule listing with the same home and away teams whose start is nearest its commence time, within 12 hours. The two sources can differ by minutes (MTL at TOR on 2026-09-29: 23:00 UTC by the NHL, 23:10 by the Odds API).
- The listings come from every raw NHL schedule response in the cache, since the odds job stores the schedule it checks on every run. Every listing counts, so an event priced before a postponement keeps the game it was priced for.
- An event no listing covers yet, such as a game more than a week ahead at the time, keeps null ids until a later replay.
- `game_id` and `game_type` are keys, not observed facts. Anything joined through them still goes through its own point-in-time selector. A closing proxy keys on the event and its commence time, because quotes priced before a postponement carry the rescheduled game's id.
- The nightly workflow runs `nhl odds replay --recent 14 --r2`: it restores the window's snapshots and schedules from R2, replays the last 14 days and mirrors the table. The report lists matches by game type and every unmatched event.
- The Odds API historical endpoint is paid. The `guard-bash` hook blocks it unless Claude Code starts with `ALLOW_PAID_ODDS=1`.
- MoneyPuck data is free for non-commercial use with attribution and must not be scraped. It is a sanity check, not a backtest input.
- Daily Faceoff's starting-goalies page is fetched at the goalie polls only (#48), about one request a date per run; its other pages stay manual references. RotoWire's terms forbid automated access, so it is never fetched.

## Pre-game goalies

`nhl goalies poll` records what the NHL API says about each team's starting goalie before puck drop (#42), so the audit (#9, plan section 10) can tell whether and how early starters are confirmed. The ingest reads boxscores only after a game is final, and a pre-game state cannot be fetched after the fact, so this poll is the only record of it.

- **When:** at every odds slot (07:05, 12:45, 18:45, 19:45 and 21:45 ET), as a step in `.github/workflows/odds-snapshots.yml` before the snapshot; it uses the odds cron lines and adds no runs. Those polls cover every regular-season or playoff game starting within 18 hours. `.github/workflows/pregame-goalies.yml` adds a poll at :50 of every hour from 12:50 to 02:50 UTC (15 runs a day) with `--within 75`, so every game starting between 09:00 and 23:00 ET, such as a 09:00 ET start in Europe, also gets one 10 to 70 minutes before its start. The slot poll runs before the odds snapshot, so its goalie rows are observed before the prices of the same slot.
- **What:** for each game in the window that has not started, `GET /v1/gamecenter/{gameId}/boxscore`, `/landing` and `/right-rail`, always fetched fresh (three requests a game, throttled as every NHL call).
- **Raw:** `nhl/pregame-boxscore/<ET game date>/<game_id>/<fetch stamp>`, `nhl/pregame-landing/...` and `nhl/pregame-right-rail/...`, mirrored to R2. They are kept apart from the ingest's `nhl/boxscore/`, which reuses the newest cached boxscore of a game: a pre-game copy there would stand in for the final one. They cannot be fetched again, so `nhl status` compares the whole key sets of each with R2 (`nhl lake sync-raw --prefix nhl/pregame-` or `restore-raw` brings them in step).
- **Signal:** the boxscore's `playerByGameStats.<side>.goalies[].starter` flag, the same one `actual_lineups.starting_goalie` reads after the game. On 2026-09-29, seven to nine hours before FLA at CAR, the boxscore had no `playerByGameStats`, the right-rail had no goalie field, and the landing listed each team's goalies with season stats (`matchup.goalieComparison`) but flagged no starter. When, or whether, the flag appears before puck drop is what the poll measures. The landing and right-rail copies are kept so a later check can look for other signals.
- **Table:** `nhl goalies replay` rebuilds the lake's `pregame_goalies` from the raw boxscores: one row per poll, game and team, with the goalies listed, how many carry the starter flag, and `starter_id` when exactly one does. `observed_utc` is the fetch time, when that state was public; a response fetched at or after the scheduled start gives no rows. The nightly workflow runs `nhl goalies replay --recent 4 --r2`: today, which the evening slots poll ahead, and the three game dates before it.

### Daily Faceoff starting goalies

The same `nhl goalies poll` also fetches Daily Faceoff's starting-goalies page for each US Eastern date that has a game in the poll window (#48; `--no-daily-faceoff` skips it), so the audit can compare how early and how accurately each source names the starter. On 2026-09-29 at 12:10 UTC, Daily Faceoff already listed Tristan Jarry as EDM's confirmed starter for that night, reported the day before, while the NHL flagged no one.

- **Raw:** each page untouched (HTML) as `dailyfaceoff/starting-goalies/<ET date>/<fetch stamp>`, mirrored to R2 and compared in full with R2 by `nhl status`.
- **Parsed from** the JSON in the page's `__NEXT_DATA__` script (`props.pageProps.data`): per game `dateGmt` and, per side, the team name, goalie name and Daily Faceoff id, status (`NewsStrengthName`, such as Likely or Confirmed), `NewsCreatedAt` and `NewsSourceUrl`.
- **Table:** `nhl goalies replay` also rebuilds the lake's `dailyfaceoff_goalies`: one row per page fetch, game and team with the goalie, status, `reported_utc` and source. `observed_utc` is the fetch time, not the report time, since a status can change. Games that had started at the fetch give no rows. Goalies stay names; the audit matches them to NHL player ids. There is no NHL `game_id`; `start_utc` and `team` identify the game.

## Reference files

Hand-compiled CSVs in `src/nhl_edge/reference/`, loaded and validated by `nhl_edge.reference` (schemas in `lake/schemas.py`). They cover the lake's seasons, 2010-11 on, and were compiled on 2026-09-29. `nhl audit reference` checks them against every game in the lake's `games` table and lists each problem.

| File | One row per | Columns | Source |
| --- | --- | --- | --- |
| `teams.csv` | team code (triCode) | franchise_id, name, first_season, last_season, predecessor | NHL records API `franchise` and `team` |
| `arenas.csv` | building | name, city, country, latitude, longitude, tz (IANA), source | Coordinates from the Wikipedia article in `source`. Zones by city |
| `venues.csv` | venue name the NHL API gives | arena_id | The lake's `games`, and the 2026-27 schedule |
| `home_arenas.csv` | team, arena and first season | last_season, primary | The lake's `games` |
| `coaches.csv` | head coach's stint with a team | team, first_game, last_game, coach, note | NHL records API `coach-franchise-records`, split by hand where noted |
| `attendance_limits.csv` | limit on spectators at one arena over a date range | first_date, last_date, capacity_share, limit, announced, source, ended_announced, ended_source | The 2020-21 and 2021-22 NHL season articles on Wikipedia and the news sources they cite, one per row in `source`. Seat counts from the NHL records API `team` |

- **Team codes.** ATL became WPG in 2011-12, PHX became ARI in 2014-15, and ARI became UTA in 2024-25. `predecessor` links the codes of one team.
  - The NHL counts Utah as a new franchise (`franchise_id` 40), but the Coyotes' players and staff moved there, so UTA's predecessor is ARI.
  - `first_season` is 20102011 for a code already in use then. `last_season` is empty while a code is in use.
- **Arenas.**
  - `arena_id` is the slug of the latest name the NHL API gave the building, and `venues.csv` maps every name to it. For example, Pepsi Center and Ball Arena both map to `ball_arena`, and Hartwall Areena, Hartwall Arena and Veikkaus Arena to `veikkaus_arena`.
  - Neutral-site games (European games, outdoor games, Lake Tahoe) have their own arenas.
  - The NHL records API gives coordinates for current arenas, but it lists Little Caesars Arena at Joe Louis Arena's, 1.8 km away. Every other current arena the API locates agrees with Wikipedia within 0.5 km.
  - Each zone gives the same UTC offsets as the NHL schedule's `venueTimezone` for every 2026-27 game.
- **Home arenas.** A team has one primary home arena per season, its base for travel: the arena of its first home game that season, public with the schedule before the season starts. NYI split its home games between Barclays Center and the Nassau Coliseum in 2018-19 and 2019-20, so those seasons list both. Barclays is primary in 2018-19 and the Coliseum in 2019-20.
- **Coach tenures.**
  - The NHL record has one row per coach and franchise, with his first and last regular-season game. Where a coach had two stints with a team (Ruff with BUF, Sutter with CGY, Hitchcock with DAL, and others), the stints are split along the tenures between them.
  - Every stint since 2010-11 matches the NHL's count of games coached, except where a note explains:
    - two TBL games in March 2013 the NHL credits to no coach
    - NJD 2014-15, when Scott Stevens, Adam Oates and general manager Lou Lamoriello shared the bench
  - A stint runs through a change of code: Tippett from PHX to ARI, Tourigny from ARI to UTA.
  - New coaches for 2026-27 (EDM, LAK, TOR, VAN, VGK) start at their team's first game. Their hire dates are in the note.
- **Point in time.**
  - Team codes, arenas, venues and home arenas are known seasons ahead, so they carry no `observed_utc`. A feature learns a game's venue from `schedule`, public a day before the game (ADR 0005).
  - A coach's stint is different: its end is future information while it runs. Features read tenures through `coaches_known_at`:
    - A stint counts as known from 10:00 UTC the morning after its first game, when that game's feeds show who coached (ADR 0003 and 0004).
    - Its `last_game` becomes known only once the next stint is.
    - The `note` stays out, since it was written with hindsight.
  - This is conservative: most coaching changes are announced a day or more before the new coach's first game.
- **Attendance limits.** `games.limited_attendance` marks the 2020-21 season as a whole. `attendance_limits.csv` has each arena's limits by date:
  - 2020-21: every arena from the season's first day, 2021-01-13, to its last, 2021-05-19. Canadian arenas and Lake Tahoe had no spectators. Arizona, Dallas and Florida admitted crowds from the start. The other US arenas opened from their own dates, from Nashville on January 26 to Chicago on May 9. Playoff-only changes are left out.
  - 2021-22: the Omicron limits in Canada, from mid-December 2021 to March 2022. Ontario went from 50% to 1,000 spectators to closed doors to 500 to 50%. Quebec went from closed doors to 50%. Manitoba went from 50% to 250 to 50%. Alberta and BC stayed at 50%.
  - `capacity_share` is the share of seats open (0 means no spectators). A head-count limit is divided by the arena's seats, and `limit` keeps the wording.
  - `announced` is the date of the source reporting the limit. It is empty only for a limit in force from the first day of 2020-21, known before the season.
  - A limit ends either when the next one starts the day after, or when it is lifted. For a lift before its season ended, `ended_announced` and `ended_source` date and cite the lift's announcement.
  - Where a team played below the legal limit by choice, `limit` says so. The Maple Leafs played behind closed doors under Ontario's 1,000 cap, and the Jets admitted no one under Manitoba's 250 until at least January 11, 2022.
- **Capacity share per game.** Features read shares through `capacity_share(schedule, prediction_utc, predicting)`:
  - It reads only the schedule rows `schedule_known_at` returns: games whose result is public, and the games being predicted once their schedule is public (ADR 0005). A game's venue and date stay hidden until then.
  - A source counts as public from 10:00 UTC the day after its date, the rule ADR 0003 sets for results.
  - A game takes the latest limit at its arena that started by its date and was public by the prediction.
  - That limit applies while it runs, and after its last day too while what ended it is not yet public.
  - So a game-day prediction reads Nashville's first game with fans (January 26, 2021, reported the next day) as empty, and Montreal's game closed on the day itself (December 16, 2021) as full.
  - A prediction the day before Tampa Bay's May 7, 2021 game sees 3,800 fans, and one that morning sees 4,200.
- **Upkeep.** When a limit is imposed, changed or lifted, add or close its row with the sources' dates. At a coaching change, end the old stint at its last game and add the new one from its first game. When the NHL uses a new venue name, add it to `venues.csv` (and the building to `arenas.csv` if new). Then run `nhl audit reference`.
