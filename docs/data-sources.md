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
| [Daily Faceoff](https://www.dailyfaceoff.com/), [RotoWire](https://www.rotowire.com/hockey/starting-goalies.php) | Projected lines, power play units, starting goalie status | Web pages only, no API; check terms before automating | Optional manual reference only; not part of the automated pipeline |
| Static files you create | Arena coordinates and time zones, team abbreviation history, coach tenures | One-off CSVs in the repo | Travel, rest and coach-change features |

## Endpoints

```text
NHL API (api-web.nhle.com)
  GET /v1/schedule/{YYYY-MM-DD}
  GET /v1/gamecenter/{gameId}/play-by-play
  GET /v1/gamecenter/{gameId}/boxscore
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
| `play-by-play`, `boxscore`, `shiftcharts` | `{season}/{game_id}` | always (fetched only for final games) |
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

Lake tables live in `data/lake/<table>/`, and with `--r2` they are mirrored to `lake/<table>/` in R2. `games` is partitioned as `season=S/game_date=D/part-0.parquet`. An ingest makes its final games the whole content of the window's dates: it replaces their partitions and deletes a partition that no longer has games, locally and in R2. `players` is one file. `--supabase` upserts the games into Supabase `games` and ends with one small read, which keeps the free project from pausing.

`.github/workflows/ingest-nightly.yml` runs `nhl ingest --recent 3 --r2 --supabase` at 09:00 UTC (04:00 or 05:00 ET, when every game of the night is final), then `nhl lake size --max-gb 8`, which fails the run above 8 GB. R2 is free up to 10 GB and has no spending cap. The three-day lookback picks up a game that was not final yet and a missed night. The earlier days come back from R2, not the NHL API. Older gaps are caught up with a manual run over a date range.

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
- The Odds API historical endpoint is paid. The `guard-bash` hook blocks it unless Claude Code starts with `ALLOW_PAID_ODDS=1`.
- MoneyPuck data is free for non-commercial use with attribution and must not be scraped. It is a sanity check, not a backtest input.
- Daily Faceoff and RotoWire are manual references only, not part of the automated pipeline.
