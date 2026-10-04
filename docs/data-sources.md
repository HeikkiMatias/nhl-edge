# Data sources

The fixed list of sources and endpoints. Work from this file instead of guessing URLs, and add new sources with the `add-data-source` skill. Copied from docs/plan.md section 3 on 2026-09-28.

## Sources

| Source | What it gives | Access and limits | Role in the model |
| --- | --- | --- | --- |
| [NHL API](https://github.com/Zmalski/NHL-API-Reference) (api-web.nhle.com, api.nhle.com/stats/rest) | Schedule, play-by-play with shot coordinates, boxscores, shift charts, rosters, player bios and career stats, draft picks, team prospects | Free, unofficial and undocumented, no published rate limit; throttle to about 1 request per second and cache every response | Backbone: shifts for RAPM, shots for xG, rosters, schedule for rest and travel |
| [NHL time-on-ice reports](https://www.nhl.com/scores/htmlreports/20242025/TH021235.HTM) (www.nhl.com/scores/htmlreports) | One HTML page per game and team (TH home, TV visitors): each player's sweater number and name, then every shift with its number, period, start and end, duration and a goal or penalty mark | robots.txt allows `/scores/htmlreports`. NHL.com's terms of service (updated 2025-10-29) forbid "unauthorized spidering, scraping, or harvesting of content or information, or use any other unauthorized automated means to compile information". The owner decided on 2026-10-02 to fetch them once, for #68 only: 114 pages (57 games, home and visitor), throttled, kept in the raw store and R2, never fetched again | Shifts of the 57 games of 2024-25 whose shift chart is empty (#68), so their `shift_coverage`, `strength_time`, chart skater counts and RAPM stints work as for any other game |
| [MoneyPuck data](https://moneypuck.com/data.htm) | Shot-level data with xG for 2007 to 2026, skater, goalie, team and line stats by season and game, player bios | Free CSV downloads for non-commercial use, attribution required, no scraping | Sanity check for the simple in-house xG model and player stats; not a backtest input, since its xG was likely fitted across many seasons |
| [The Odds API](https://the-odds-api.com/) | Live NHL moneyline, puck line and totals from EU books including Pinnacle; props per event | Free: 500 credits a month. Paid from $30 a month for 20,000 credits. Historical odds from 2020, paid plans only, 10 credits per market per region ([docs](https://the-odds-api.com/liveapi/guides/v4/)) | Live odds snapshots from day one on the free tier; historical backfill deferred to v2 |
| [SBR odds archive](https://www.sportsbookreviewsonline.com/scoresoddsarchives/nhl/nhloddsarchives.htm) | Opening and closing moneylines, puck lines and totals, 2007-08 to 2022-23 (2022-23 stops on 2022-11-27) | Free HTML tables (the Excel files are gone), no longer updated, book source not stated; answers 404 without a browser User-Agent | Opening lines for the historical tradable test (E2), closes for the information test (E1); quality unknown until the phase 1 audit |
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
  GET /v1/roster/{team}/{season}          # season as 20252026
  GET /v1/player/{playerId}/landing       # bio + career stats by league
  GET /v1/player/{playerId}/game-log/{season}/{gameType}
  GET /v1/draft/picks/{season}/all
  GET /v1/prospects/{team}

Daily Faceoff (www.dailyfaceoff.com), HTML page, never /api/
  GET /starting-goalies/{YYYY-MM-DD}      # US Eastern date

NHL stats API (api.nhle.com/stats/rest)
  GET /en/shiftcharts?cayenneExp=gameId={gameId}

NHL time-on-ice reports (www.nhl.com, browser User-Agent), once for #68's 57 games, nhl toi-reports only
  GET /scores/htmlreports/{season}/TH{last 6 digits of gameId}.HTM   # home team, e.g. 20242025/TH021235.HTM
  GET /scores/htmlreports/{season}/TV{last 6 digits of gameId}.HTM   # visitors

The Odds API (api.the-odds-api.com)
  GET /v4/sports/icehockey_nhl/odds?regions=eu&markets=h2h,spreads,totals&oddsFormat=decimal
  GET /v4/historical/sports/icehockey_nhl/odds?date={ISO8601}   # paid plans

SBR odds archive (www.sportsbookreviewsonline.com, browser User-Agent required)
  GET /scoresoddsarchives/nhl-odds-{2010-11 .. 2022-23}/      # 2020-21 is nhl-odds-2021
```

## Access rules

- Throttle the NHL API to about 1 request per second and cache every response. Raw responses are stored untouched as JSON under `data/raw/` locally and under the `raw/` prefix in the R2 lake, so parser bugs can be fixed and replayed without calling the API again.
- NHL.com's terms forbid unauthorized automated harvesting of its pages. Its time-on-ice reports are fetched only under the owner's one-time decision of 2026-10-02 (#68): `nhl toi-reports` refuses any game outside 2024021235 to 2024021291 before it makes a request, and a stored page is never fetched again. Any other page or game needs a new decision.
- The NHL API is unofficial and changes without notice. A nightly contract test on one golden game catches schema drift.
- The NHL API has no end-of-game time. A result counts as public at 10:00 UTC the morning after its game date, and at least six hours after its start (`games.observed_utc`, ADR 0003).
- Shift chart coverage varies for older seasons. Phase 1 checks coverage per season before RAPM depends on it.
- The Odds API free tier has 500 credits a month. A call costs 1 credit per market per region and returns every game. The slot plan uses about 300 credits a month. Store `last_update` with every quote.
- Many EU books (13 of 20 on 2026-09-28, among them Marathonbet, Unibet, Betclic and 1xBet) quote the 3-way regulation line under the Odds API `h2h` key, with a `Draw` outcome. The parser stores those quotes as `h2h_3_way`, so `h2h` only holds the two-way moneyline including OT and the shootout. Pinnacle, Betsson and NordicBet quote the two-way line.
- A book sometimes posts a market at a decimal price of 1.0, which pays nothing back: GTbets quoted two games' `h2h` at 1.0 on both sides on 2026-09-30. The parser skips a book's market priced at 1.0 or less on any side, the snapshot's log line counts them, and the raw copy keeps them (#83).

## When jobs run

GitHub Actions runs every job, but GitHub's own `schedule` started this repo's runs 3 to 6 hours late in September 2026, while a `workflow_dispatch` starts within seconds (#50). So the Cloudflare Worker in `infra/timer` fires every five minutes and dispatches the workflows due at that minute (`infra/timer/src/schedule.js`):

- `odds-snapshots.yml` at each odds slot in US Eastern time, DST included: 07:05 (morning), 12:45 (midday), 18:45 (pre7), 19:45 (pre8) and 21:45 (pre10), with the slot as input.
- `pregame-goalies.yml` at :50 of every hour from 12:50 to 02:50 UTC.
- `ingest-nightly.yml` at 09:00 UTC. After the ingest, it runs `nhl recheck --recent 3 --r2` (#30, below).

The workflows keep their GitHub cron lines as a fallback. While the repository variable `TIMER_ACTIVE` is `true`, a scheduled run skips its job, so only the timer's dispatches run; set it to `false` to fall back to the late GitHub schedule, for example when the timer's token has expired. `infra/timer/README.md` covers setup. A failed dispatch shows as a failed cron event in the Cloudflare dashboard. Any workflow can also be started by hand from the Actions tab.

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
| `player-landing` | `{player_id}` | always (bio and draft facts do not change). Its career lines (`player_league_seasons`, below) stop at the fetch, so a later season needs the page fetched again. |
| `toi-home`, `toi-visitor` | `{season}/{game_id}` | always. Only `nhl toi-reports` fetches them (#68, below), and only for the 57 games whose shift chart is empty. The HTML is stored untouched like the JSON. |

Two cached responses reflect later knowledge, so neither may feed a point-in-time input:
- A past season's roster is an after-the-fact view. It only finds player ids for `players` and must never feed a lineup (hard rule 9).
  - Up to 2022-23 it lists everyone who played for the team that season (31 to 44 players).
  - From 2023-24 on it holds only a current-style roster (17 to 30). ARI 2023-24 is empty, since the franchise moved.
  - The boxscores fill these gaps, which is why player ids come from both.
- A boxscore fetched years later includes post-game stat corrections. It may be used only after its game's `observed_utc`.

The landing page's `position` is today's, so it stays out of `players`. The boxscore's position code is today's too: Brent Burns is listed `D` in the forwards group through his 2013-14 season at forward. The group a player is listed in (forwards, defense, goalies) is his role in that game, and `actual_lineups.role` comes from it.

`--replay` reads only the local raw cache, never the network, and fails on a miss. It re-parses every player, so a parser fix reaches old rows. A replay leaves a date as it is, and warns, when the schedule copy it reads lists a game there that is not final: an earlier run with a fresher schedule may already have written that game, and rewriting the date would delete it (#109). The odds job's schedule check always fetches fresh.

**Raw cache copies.** R2 `raw/` is the primary copy, and the laptop's `data/raw/` is the second copy outside R2 that plan §10 asks for. The raw cache holds responses that cannot be fetched again if the API changes.
- The nightly ingest and the odds snapshots run on GitHub Actions and write only to R2. `nhl lake restore-raw` downloads every complete response missing locally, and never overwrites a local file. Run it weekly and before any replay, so the laptop keeps up.
- A response cached without `--r2`, or whose upload failed, stays local until `nhl lake sync-raw` uploads it.
- Both commands take `--prefix` (such as `odds/`) and skip incomplete responses (a body without its sidecar) on either side.

Lake tables live in `data/lake/<table>/`, and with `--r2` they are mirrored to `lake/<table>/` in R2. A pull from R2 (`Lake.pull`) downloads, in parallel, only the files whose MD5 differs from the ETag R2 lists, and can be limited to some seasons. `games` is partitioned as `season=S/game_date=D/part-0.parquet`. So is `schedule`, which holds the pre-game facts of the same games (teams, start, venue, neutral site, limited attendance) and no result columns. A game's schedule is public 24 hours before its start (ADR 0005), and its result the morning after (ADR 0003). Features read `schedule` through `schedule_known_at`, which returns the games whose result is already public plus the games being predicted. Other games stay hidden, because the table holds only games that were played: a missing or delayed game would reveal how it turned out. A game re-timed at short notice gets a later public time (`SCHEDULE_PUBLIC_OVERRIDES`; the Lake Tahoe game PHI at BOS, 2021-02-21). Anything about outcomes reads `games` through `results_known_at`. An ingest makes its final games the whole content of the window's dates: it replaces their partitions and deletes a partition that no longer has games, locally and in R2. `players` is one file. `--supabase` upserts the games into Supabase `games` and ends with one small read, which keeps the free project from pausing.

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

`nhl ingest` parses each final game's three feeds into seven lake tables, partitioned like `games` (`season=S/game_date=D/`) and replaced a whole game date at a time. `--replay` rebuilds them from the raw cache. The parsers are in `src/nhl_edge/ingest/` (`shots.py`, `shifts.py`, `toi_reports.py`, `lineups.py`, `shift_coverage.py`, `strength_time.py`, `plays.py`), and the schemas in `lake/schemas.py` document every column.

| Table | From | Grain | What it holds |
| --- | --- | --- | --- |
| `shots` | play-by-play | one unblocked attempt | shots on goal, missed shots and goals in periods 1 to 4, with time, shooter, goalie, coordinates, shot type and strength |
| `shifts` | shift chart, or the time-on-ice reports where the chart is empty (below) | one player shift | team, period and start and end in elapsed game seconds |
| `actual_lineups` | boxscore | one dressed player | role (F, D, G), sweater number, starting goalie and time on ice |
| `shift_coverage` | all three | one game | how far the shift chart can be trusted (below) |
| `strength_time` | play-by-play and shift chart | one team, game and strength state | seconds at each strength state (such as `5v4`), with each net manned or empty (below) |
| `penalties` | play-by-play | one penalty | the penalized team, the players who took it, drew it and served it, its type and minutes (below) |
| `faceoffs` | play-by-play | one faceoff | winning team, winner, loser and the zone from the home team's side (below) |

Every row counts as public at 10:00 UTC the morning after its game date, like the game's result (ADR 0003). A game's own shots, shifts and lineup never feed a prediction for it, and backtest lineups come only from earlier games' boxscores (hard rule 9). The backfilled feeds were fetched years after the games and include post-game corrections, which live does not see. The tables leave out scoring credits, but corrections can still change kept values: a goal's scorer, a shot record, time on ice. ADR 0004 accepts this small look-ahead, and #30 measures it.

Measuring corrections (#30):
- `nhl recheck` fetches a live-season game's three feeds again once the play-by-play the tables read is a week old. It picks games by when the tables' copy was fetched, the stamp that ends `actual_lineups`' `raw_key`, not by game date, so a game ingested nights late is still rechecked a week after its copy. The nightly run covers copies fetched 7 to 10 days before it, so two missed nights are caught up.
- The copies go under `nhl/<kind>-recheck/<season>/<game_id>/` in the raw cache. The tables read the newest copy under `nhl/<kind>/`, so they never parse a recheck, and a replay builds the same tables before and after one (`tests/leakage/test_corrections.py`).
- `nhl audit report`'s "Post-game corrections" section parses both copies the way the ingest does and compares them row by row, per table and field. A moved goal scorer is the correction ADR 0004 accepts as small, so any other difference is listed as a problem. The section also lists 2026-27 games due for a recheck that have none.

Conventions:
- Times are elapsed game seconds: (period − 1) × 1200 plus the period clock. Overtime is period 4 and lasts 300 seconds. The shootout is not play and has no rows.
- `situationCode` has four digits: away goalie in net, away skaters, home skaters, home goalie in net. A pulled goalie shows as 6 skaters. `shots` turns it into the shooting team's view: `skaters_for`, `skaters_against`, `strength` (such as `5v4`) and `is_empty_net`. It is missing for 22 shots in two games (2010020124, 2013020971), which leaves their strength null.
- In some 2019-20 and 2020-21 games, `situationCode` stays a skater off after a penalty for the rest of the game (#28). So when a game's shift chart is complete, each shot takes its skater counts from the chart instead (ADR 0009), when the chart puts 3 to 6 skaters of each team on the ice. Penalty shots keep `situationCode`. `is_empty_net` and the raw `situation_code` always stay with `situationCode`, and `strength_source` says which source each shot's counts came from.
- Penalty shots are 1 skater against 0 with the defending goalie in. `shots` keeps and flags them (`is_penalty_shot`).
- Coordinates are rink feet, turned so the shooting team attacks the net at x = +89 (y turns with x). Before 2019-20 the feed does not say which end a team attacks. The direction is inferred per team and period from the median x of its offensive-zone shots, and it agreed with `homeTeamDefendingSide` in all 954 team-periods checked (2019-20 onward). A few old plays have a zone code that contradicts their coordinates.
- Each shot carries the play logged just before it in the same period, for xG's rebound and rush flags (#73, ADR 0010): `prev_event_type`, `prev_seconds`, `prev_by_shooting_team` and `prev_zone`, from the shooting team's side. The zone comes from the play's x and the shooting team's attack direction, past the blue line at x = ±25, or from `zoneCode` without coordinates. A blocked shot is logged under the team that took it, but its `zoneCode` is nearly always from the blocker's side. On 160 games of the training seasons, coordinates and `zoneCode` agreed for every other kind of play, but for about 1% of hits (ADR 0010).
- A player is on the ice for an event at second t when `start_s < t <= end_s`.
- Boxscores flag one starting goalie per team in every game from 2010-11 on. Teams dress 17 to 21 players.

`strength_time` (#72) gives the seconds each team spent at each strength state, for per-60 rates and expected power-play opportunities:
- **Timeline.** Every play carries a `situationCode`, and plays come every few seconds. Each stretch of play between consecutive plays takes the skater counts at the first of them, and a period's first play's counts stand from the period's start. Plays are ordered by game time, since old feeds log a few out of order.
- **Counts.** They follow ADR 0009, as shots' do: from the complete shift chart, with 3 to 6 skaters a team, otherwise from `situationCode`. Whether a net is empty comes from `situationCode`, so a few seconds a game can show six skaters with the goalie in, around delayed penalties and line changes.
- **What is left out.** Penalty shots and the shootout. A penalty that expires between two plays counts until the next play.
- **Checks.** Each team's seconds add up to `game_seconds`, the game's length with overtime and without the shootout, and the two teams mirror each other. The audit report's "Strength time" section lists every game that does not add up or has no rows.

Shift chart rows the parser leaves out, counted in `shift_coverage`:

| Count | Rows | Seen in |
| --- | --- | --- |
| `dropped_rows` | zero-length shifts, shootout (period 5) rows, and repeats of a kept shift (same player, period and times, under the same or another shift number) | blank-ended `00:00` placeholders in 174 games of 2019-20; 2022020041 lists 14 shifts twice; many 2023-24 charts repeat a shift under the next shift numbers |
| `foreign_rows` | teams not in the game | 2021020513 (WSH at NYI) lists every shift twice and STL and MIN shifts besides |
| `bad_rows` | malformed times, shifts outside their period, a shift number repeated with other times | |

The API returns an empty shift chart for 57 games, 2024021235 to 2024021291 (2025-04-08 to 2025-04-15). A live request on 2026-09-29 still gave none. One other game from 2010-11 on gives no shifts, 2013020971, whose chart holds a goal marker and nothing else; it stays without shifts (#68 covers only the 57). Those copies were fetched long after their games, so they count as settled and are not fetched again. Their shifts come from the NHL's time-on-ice reports instead (#68, below).

A game's chart is `complete` when it has no bad rows and every dressed player's shifts add up to his boxscore time on ice within 60 seconds (a missing boxscore time counts as not adding up). At every unblocked shot except penalty shots, the players on the ice by the chart are also compared with `situationCode`, and `skater_mismatches` and `goalie_mismatches` count where they differ. RAPM drops the stints that contradict the strength state. `nhl audit shifts` prints the per-season summary, which is reviewed before RAPM depends on the charts.

`penalties` and `faceoffs` (#96) are for the player layer (#12): each player's penalties taken and drawn give B3's expected power plays, and the faceoff that opens a stint gives RAPM its zone start. Checked on every game of the open seasons, 2010-11 to 2021-22: each of the 110,753 penalties in play has a team of the game and a time, 2,953 name no one who took it and 9,336 no one who drew it, and each of the 819,251 faceoffs names both players and a zone.
- **Penalties.** `team` is the penalized team, the play's `eventOwnerTeamId`. `committed_by` is the player penalized, `drawn_by` the player fouled, and `served_by` the player who sat in the box for someone else: a bench minor, a goalie's penalty, some majors. Each is null where the feed names no one, such as a head coach's game misconduct. `type_code` is MIN (a double minor is a MIN of 4 minutes), MAJ, MIS, GAM, MAT, BEN or PS. A penalty shot's 0 minutes and a misconduct's 10 put no team short-handed. A new type code fails the schema, which stops the ingest rather than pass unread. The two penalties of 2010-11 to 2021-22 logged in a shootout (2013021096, 2017020755) are left out.
- **Faceoffs.** `winning_team` is the play's `eventOwnerTeamId`. The feed's `zoneCode` is from the winner's side, so `zone` flips O and D when the away team won and gives the home team's side. On every game of 2019-20 to 2021-22, where plays say which end the home team defends, the coordinates of 150,074 of the 150,175 faceoffs away from center ice agree with their zone. All but one of the other 101 fill two whole games (2019020249, 2019020256) and a period of 2020020175, where the side or the coordinates are flipped throughout.
- **Checks.** The audit report's "Penalties and faceoffs" section lists games without faceoffs, and the team-games whose penalties with a player do not add up to the penalty minutes of its players in the boxscore. Over the 31,288 team-games of 2010-11 to 2021-22, 20 do not: 5 by 10 minutes (misconducts, two of them logged in a shootout), 11 with a minor more in the boxscore, 3 with one fewer, and 1 by 5. A bench minor counts toward no player in the boxscore, so the check leaves out penalties without a player.


### Time-on-ice reports

The NHL's time-on-ice reports (TH for the home team, TV for the visitors) list the same shifts as the shift chart, and they exist for the 57 games of 2024-25 whose chart is empty (#68). NHL.com's terms forbid unauthorized automated harvesting, so the owner allowed one fetch of those 114 pages (2026-10-02, Sources above).
- **Fetch.** `nhl toi-reports [--seasons 20242025] [--r2]` selects the season's games whose `shift_coverage` row has no shift rows, or has them from the reports already, and prints how many. It fetches each game's two pages through the NHL client's throttle, with a browser User-Agent, under `nhl/toi-home/` and `nhl/toi-visitor/` in the raw store (mirrored to R2 with `--r2`). A stored page is always reused, so a rerun makes no request. It refuses every game outside 2024021235 to 2024021291 before any request. It reads each page to check it is the right game and side, and prints the players and shifts found. It is not part of any workflow.
- **Ingest.** When a game's chart gives no shifts and both of its reports are in the raw cache, `nhl ingest` (and `--replay`) builds the game's shifts from them; it never fetches a report. The rest runs as on a chart: `shift_coverage` with the same completeness check, the chart's skater counts in `shots` and `strength_time` when complete (ADR 0009), and so `stints`. Only `raw_key` shows the source: each shift's is its own report's, and the game's `shift_coverage` row takes the home report's. The rows are public at 10:00 UTC the morning after the game, like the chart's, however late the pages were fetched. The window's summary line counts the games whose shifts came from reports.
- **Parsing** (`ingest/toi_reports.py`). Each player's heading starts with his sweater number ("4 BYRAM, BOWEN"), which the game's boxscore (`actual_lineups`) maps to his player id for that team. A shift's start and end are "elapsed / remaining" period clocks, and the elapsed one gives `start_s` and `end_s` in game seconds, overtime as period 4. The rows then go through the chart's own checks, so `dropped_rows` and `bad_rows` mean the same.
- **Problems.** These also count as bad rows, which make the game incomplete: a clock whose two halves do not add up to the period, a period other than 1 to 3 or OT, and every shift of a player whose number is not in his team's boxscore (or is there twice). The ingest also prints a warning naming that player. A page of another game or team stops the ingest.
- **Timing.** The rows count as public at 10:00 UTC the morning after the game, as every per-game table does (ADR 0004): the NHL posts these pages during and right after the game. They are fetched about 18 months later, so they may carry post-game corrections, the backfill risk ADR 0004 accepts and #30 measures. A live game with an empty chart would have no reports read, so these 57 games are the only ones whose backtest shifts have no live counterpart.
- **Checked** on the pages of 2024021235, 2024021260 and 2024021291 against their boxscores: every number maps to a player, every clock reads, and each player's shifts add up exactly to his boxscore time on ice, so all three games are complete.

## Player league seasons

`player_league_seasons` (#98) holds each player's season lines in every league, from the `seasonTotals` array of his landing page (`GET /v1/player/{playerId}/landing`), for the NHLe offensive priors (#102).
- **Source:** the newest cached landing page of every player in `players`, read from the raw cache; no request is made. The ingest fetches a player's page once, when he first shows up, and always reuses it (above), so a season played after the fetch needs his page fetched again. The backfill fetched every page in September 2026.
- **Grain:** one row per player, season, `leagueAbbrev` and game type. A player with several teams in a league-season has one line per team, and they are summed; `teams` counts them. Only the regular season (2) and playoffs (3) are kept: the cache's other lines are 83 lines of the 2004 World Cup of Hockey (6 and 7). `games_played`, `goals` and `assists` are null when any line summed lacks them, since a partial sum would read as a whole season. Most goalie lines (about 10,600) have games played only.
- **League:** `league_abbrev` keeps the page's name, and `league` trims it, puts it in upper case and maps the known variants of one league to one name (`LEAGUE_VARIANTS` in `ingest/player_seasons.py`, with a comment per mapping). The NHL API names many leagues one way up to about 2015-16 and another way after: Sweden and SHL, Finland and Liiga, Swiss, NLA and NL, the NCAA conferences and NCAA, WC-A and WC, W-Cup and WCup. Predecessor leagues stay apart: Russia's Superleague is not the KHL. Of the 933 names in the cache, 35 are mapped, and upper case merges one more (MtJHL), leaving 897 leagues.
- **One league under two names:** on 2026-10-02, 124 player-season-game types had lines of one league under two names, all from 2009-10 to 2018-19, so none in a held-out season. In 37 of them both names list the same teams, and in 26 of those the games played match too, so they look like one season listed twice. They stay apart in the table, which keys on `league_abbrev`; a reader adding up by `league` must check them. The audit report counts them.
- **Age:** `age_at_season` is the age in whole years on September 15 of the season's first year, the NHL draft cutoff, from `players.birth_date`.
- **Timing:** a season's lines count as public on July 1 (00:00 UTC) after it (`observed_utc`), when nearly every league's season and the NHL playoffs are over (`SEASON_LINES_PUBLIC` in `lake/schemas.py`). The fetch time would hide all history from the backtest. A line that was not yet public when its page was fetched is a partial season and is dropped (on 2026-10-02, 12 lines of 2026-27 and 2 JPL-Pro lines of 2025-26, public only from October 1, 2026).
  - Pages fetched long after a season may carry later corrections to its goals and assists. ADR 0016 accepts that risk for these season totals, as ADR 0004 does for the per-game feeds, which still leave assists out. #117's yearly refetch will measure it.
  - Some leagues can end after that July 1, so their lines count as public on October 1 of the season's second year instead (`LATE_LEAGUES`). October 1 comes before every NHL season but 2026-27 starts, and none of these lines matters to an NHL player's prior then. The Australian league (`AIHL`, earlier `Australia`; 25 lines) is played from April to September, and the NHL's season label does not say which calendar year; October 1 is late enough for either. The World Cup of Hockey (`WCup`, once `W-Cup`; 230 lines of game type 2) is played in August and September, and the 2004 tournament's lines are labelled 2003-04. The Brick Invitational (231 lines), a tournament for 10-year-olds, is played in early July and labelled with the season before, as its players' ages show. The Olympic qualification (`OGQ`, 131 lines) is not labelled the same way each cycle: the August 2021 final round is 2021-22, the August 2025 one 2024-25. JPL-Pro (36 lines, and 2 of 2025-26 not yet public) is a summer pro-am league whose label is not known.
  - As a precaution, a few small events whose dates or labels are not known also wait until October 1: `Exhib.` (48 lines, 1995-96 to 2012-13), the IIHF development camp, `Oly-Q`, and youth tournaments whose players' ages fit either a spring event or a summer one labelled with the season before (`OGC-16`, `QGC-16`, `WCCC-16`, `WSI U12` to `WSI U16`).
  - Two NHL seasons ended after July 1: the 2020 playoffs on September 28, 2020 and the 2021 ones on July 7, 2021. Every line of 2019-20 counts as public from October 1, 2020, and every line of 2020-21 from July 9, 2021 (`LATE_SEASONS`), whatever its league, so a season's lines become public together.
  - The 2022 World Juniors were stopped in December 2021 and replayed in August 2022 under the 2021-22 label (74 lines, many of 7 games). Their lines count as public from October 1, 2022 (`LATE_LEAGUE_SEASONS`). That season's Division I A games were played in December 2021.
  - Remaining risk: not every league's dates were checked. Three leakage checks looked for events played after July 1 under the label of the season before. They found those above and confirmed that others, such as the Hlinka Gretzky Cup and the USA Hockey summer festivals, are labelled with the season that starts after them, so July 1 of the next year is safe.
  - **First game.** Every player in the table reached the NHL, so that a player has rows is hindsight before his first NHL game: a full backfill would tell an earlier fold who is going to make it. So a player's rows are observed no earlier than his first boxscore in the lake became public (`first_boxscore_utc`, the earliest `observed_utc` of his `actual_lineups` rows), and a player without a boxscore yet has none (24 roster-only players and 547 lines on 2026-10-02). `known_at` at a fold start therefore shows only players who had played by then, which is what the priors' population rule asks (#102). The lake starts in 2010-11, so a player who debuted earlier counts from his first game in it, which is conservative. On 2026-10-02 this moved 62,140 rows later than their season's public date.
- **Command:** `nhl player-seasons [--r2]` rebuilds the whole table, partitioned by season (`season=S/part-0.parquet`), and deletes any partition the rebuild no longer has. It prints the players read, the lines kept and left out, and the rows written. It also names the players without a page or without a boxscore, and those with a boxscore who are not in `players` because their page could not be fetched (one on 2026-10-02, a 2026-27 debutant). They are reported, not an error, and the audit report lists them too. It refuses to run with no players, no boxscores or no pages, rather than empty the table. It also refuses when the boxscores are incomplete, since a partial copy would date debuts too late: a season short of its games (`EXPECTED_GAMES`), or a game without a boxscore. With `--r2` it first restores the landing pages and pulls `players`, `games` and `actual_lineups` from R2, then mirrors the table. It is not part of any workflow. The audit report's "Player league seasons" section gives, per season, the players with lines and the lines in the eight leagues with the most of them, counts only, without the one-time test season and the live seasons, or the players who debuted in them, whose earlier lines would show it. It lists players without a page, and players whose `first_boxscore_utc` is no longer their first boxscore in `actual_lineups`, which calls for a rebuild.
- **Built on 2026-10-02:** 3,259 players and pages, none missing, 24 without a boxscore yet; 116,469 team lines kept, and 83 of other game types, 14 not yet public at the fetch and 547 of players without a boxscore left out; 109,167 rows over 42 seasons, 1983-84 to 2024-25, leaving out the one-time test season as the audit report does.

## Fitted tables

`shot_xg` (#73, ADR 0010) is not parsed from a feed but fitted: each scored shot's expected goals, from the xG model of its season. `nhl xg` fits one model per season from 2011-12 on, on every earlier season's shots public before the season's first game. It writes the table, partitioned like `shots`, and a calibration report to `reports/xg/<version>.md`. Each row carries its model's `train_cutoff` and `artifact_version`, and the shot's own `observed_utc`. Penalty shots, shots at an empty net and shots without coordinates get no row. `nhl xg` refuses to fit while a season it reads is short of its games, or has games without shots or not yet replayed with the `prev_` columns.

`stints` (#97, ADR 0015) cuts every game with a complete shift chart into stints, RAPM's rows: the stretches of a period with the same players on the ice and the same score.
- **Cuts:** every shift start and end, from the shift chart only (ADR 0009 trusts a complete chart over `situationCode`), and every goal. A stint holds the moments t with `start_s` < t <= `end_s`, as a shift does. Two stretches in a row with the same players and score are one stint, and a stretch with nobody recorded on the ice is a stint RAPM leaves out.
- **Per stint:** each team's skaters (player ids) and goalie (null when its net is empty), the strength from the home side (`6v5` with the home goalie pulled), the score at its start, the zone of a faceoff at its start from `faceoffs` (null for a change on the fly), and each team's xG and goals. Penalty shots stay out of the xG and goals, but count in the score. xG comes from `shot_xg`, with its `xg_version` and `xg_train_cutoff`, and is null in a season without xG, such as 2010-11.
- **Left out of RAPM:** a stint with a team of fewer than 3 or more than 6 skaters, or with two goalies, keeps a `drop_reason` and stays in the table, so the audit can say what was left out.
- **Timing:** public with the game's feeds, at 10:00 UTC the morning after (ADR 0004).
- **Commands:** `nhl stints` writes the table, a season at a time; run it after `nhl xg`. It refuses while a game has no `shift_coverage` row or a season from 2011-12 on has no xG, and refuses a season whose xG comes from more than one model, from a model not fitted before the season's first game, or misses a shot the model scores. `features.stints.player_seconds` gives each skater's seconds at 5v5, on the power play, short-handed and otherwise per game. The audit report's stints section gives each season's games with stints, and for the open seasons the stints left out and why and the share of playing time and xG RAPM keeps. Goals cut stints, so a held-out season shows only its chart coverage.

`team_strength` (#74, ADR 0011) holds each game's rolling team strength ΔS from 2011-12 on: the home team's expected goal margin over the away team.
- **Inputs:** 5v5 xG for and against per minute, power-play xG for and penalty-kill xG against per minute, and power-play and penalty-kill minutes per game. They come from `shot_xg`, `shots` and `strength_time`.
- **Memory and shrinkage:** each team's figures are decayed by games played and shrunk toward the league. The settings come from the tuning grid and are frozen: a half-life of 80 games and a pull worth 40 games.
- **Timing:** a game is rated as of 10:00 US Eastern on its date, or an hour before its start if earlier, from team-games public before then (`as_of_utc`).
- **Renamed teams:** a team's history follows its line through code changes (PHX, ARI, UTA; ATL, WPG).
- **Goalie in net:** shots count only with the shooting team's own goalie in net.
- **`train_cutoff`:** the last result the tuning run read (2018-04-09 10:00 UTC). Ratings of 2011-12 to 2017-18 are in-sample for the settings by design, so their `observed_utc` is the cutoff, and no fold starting before it can read them.
- **Commands:** `nhl team-strength` writes the table, and `--tune` reruns the grid on the training seasons and logs it to `reports/tuning/`. It refuses while a game it needs has no xG or no strength time.

`schedule_terms` (#77, ADR 0011) holds each game's schedule terms and its season's home edge from 2011-12 on.
- **Per side:** `rest_days` since the team's last game this season (capped at 4, and a season's first game takes the cap) and `back_to_back`. Also `travel_km` and `tz_shift`, the great-circle distance and the change in UTC offset (positive going east) from the arena of that game, or from the team's primary home arena (`home_arenas.csv`) for a season's first game. Arenas come from `venues.csv` and `arenas.csv`.
- **Per game:** `neutral_site`, and `capacity_share`, the open-seat share as announced by the as-of time (`attendance_limits.csv`, #26).
- **Home edge:** `home_win_rate` is the season's full-game home win rate in its non-neutral games public by the as-of time (`season_games` of them), pulled toward the three seasons before with a weight worth `prior_games` games, and `h_s` is its log-odds. The weight was tuned on the training seasons and frozen at 1,600 games. All six candidates tied, and the leader was also the steadiest, on the grid's edge (`reports/tuning/schedule-terms-20261001-89564c5.md`).
- **Timing:** as `team_strength`. A team's last game counts once its result is public (ADR 0003), as `schedule_known_at` reads the schedule (ADR 0005). The one game re-timed on the day, PHI at BOS at Lake Tahoe (2020020290), is rated just after its moved schedule became public at 20:00 UTC. That is still before E2, which waits for the same schedule (20:00:01 UTC), and before its start. Rows of 2011-12 to 2017-18 count as known only from the tuning cutoff, 2018-04-09 10:00 UTC (`observed_utc`).
- **Commands:** `nhl schedule-terms` writes the table, and `--tune` reruns the grid. It refuses while a season is short of games, a game's venue has no arena, or a rated team-season has no primary home arena.

`goalie_effects` (#75, ADR 0011) holds, for every `goalie_starts` candidate, the goals he is expected to save above an average goalie over the game.
- **Effect per shot:** his xG against minus goals against per unblocked shot faced (the shots `shot_xg` scores), decayed by his own games and shrunk toward zero. His history follows him from team to team.
- **Expected shots:** the average of the opponent's unblocked shots per game and his team's allowed per game, each shrunk toward the league with team strength's frozen settings. `goals_saved` is the effect times them, and ΔG for a pair of goalies is the home one's `goals_saved` minus the away one's.
- **Settings:** tuned on the training seasons by the ΔG expected under the goalie-start probabilities, then frozen: a half-life of 160 of the goalie's games and a pull worth 4,000 shots. Thirteen of the 16 settings tied, and the rule took the steadiest, on the grid's corner (`reports/tuning/goalie-effect-20261001-dc79a24.md`).
- **Timing and `train_cutoff`:** as `team_strength`. Effects of 2011-12 to 2017-18 count as known only from the tuning cutoff, 2018-04-09 10:00 UTC.
- **Commands:** `nhl goalie-effect` writes the table, and `--tune` reruns the grid on the training seasons and logs it to `reports/tuning/`. It refuses while a game it needs has no xG or no goalie-start probabilities.

`goalie_starts` (#76, ADR 0012) holds, for every team-game from 2011-12 on, the probability that each candidate goalie starts it (`p_start`, adding up to 1 per team-game).
- **Candidates:** the goalies who dressed for the team in its last 10 games public before the as-of time, followed through its line of team codes. A team with no earlier game has no rows.
- **Model:** a conditional logit on each candidate's share of recent and season starts, his last start, run of starts, back-to-back, rest and whether he dressed last game, all from `actual_lineups` of earlier games (hard rule 9). One model per season, fitted on earlier seasons' starters public before the season's first as-of time; `train_cutoff` is the last of them.
- **Timing:** as `team_strength`: 10:00 US Eastern on the game date, or an hour before its start if earlier, which is the rows' `observed_utc`.
- **Commands:** `nhl goalie-start` writes the table and a report to `reports/goalie-start/<version>.md`: per season, the Brier score, top-pick accuracy and the share of starters who were not candidates, beside the share of the last ten starts as a reference. It refuses while a season is short of its games or a team-game has no flagged starter.

`lineups` (#99, ADR 0017) holds, for every team-game from 2011-12 on, each candidate skater's probability of dressing (`p_available`), his expected minutes and power-play unit (#100, ADR 0018), and each candidate goalie's start probability (`p_start`).
- **Candidates:** the skaters who dressed for the team in its last 10 games public before the as-of time, followed through its line of team codes. A player whose latest public game was for another team drops out. `role` is F or D as the boxscore listed him in his latest game for the team. A team with no earlier game has no rows.
- **Model:** a logistic regression on whether he dressed last game, his share of the 10, games since he last dressed, an early exit last game (under half his average ice time in his other games among the 10), his run of straight games and his role, plus, in the team's first game of a season, "dressed last game" and the share again. All come from `actual_lineups` of earlier games (hard rule 9). One model per season, fitted on earlier seasons' boxscores public before the season's first as-of time; `train_cutoff` is the last of them.
- **Total:** each team-game's probabilities are shifted by one common amount on the log-odds scale, so that they add up to 18 less the expected newcomers. These are dressed skaters who were not candidates, averaged over the same earlier team-games: about 0.28 in a team's other games and 5.0 in its first of a season. So the skaters add up to at most 18. A team-game with no more candidates than its total gives each 1 (one in the training seasons: CAR's first game of 2011-12, 17 candidates).
- **Minutes (#100, ADR 0018):** each skater's expected minutes at 5v5, on the power play and on the penalty kill (`exp_5v5`, `exp_pp`, `exp_pk`).
  - **His minutes if he dresses:** a decayed average of his earlier games for the team that have stints, with a half-life of 10 of his games, from games public strictly before the as-of time. It is pulled toward his role's average, as if that were c more games. c is the game-to-game variance of one player's minutes over the variance between players' averages, about 1 to 2 games.
  - **Expected minutes:** times `p_available`, then scaled so that a team-game's candidates of a role and its replacement skaters add up to the league's skater-minutes of that role in that state (about 144 for forwards and 96 for defensemen at 5v5).
  - **From the season before:** the role averages, pulls, totals and newcomer minutes. `train_cutoff` is the later of that season's last game and the availability model's cutoff.
  - **Power-play unit 1** (`pp_unit`) is the five with the most expected power-play minutes.
- **Replacement skaters:** `lineup_replacements` holds, per team-game and role, the slots the candidates leave short of 12 forwards and 6 defensemen (`count`), and their minutes in all, each at a newcomer's average minutes the season before (one average for a team's first game of a season, one for its other games).
- **Goalies:** their rows are copied from `goalie_starts` with its `train_cutoff` and `artifact_version`, so a goalie row's version starts `goalie-start-` and a skater row's `lineup-`. Goalies have no minutes.
- **Timing:** as `team_strength`: 10:00 US Eastern on the game date, or an hour before its start if earlier, which is the rows' `observed_utc`.
- **Commands:** `nhl lineups` writes the table and a report to `reports/lineups/<version>.md`: per season, the Brier score over each team-game's skaters (a newcomer counts as a candidate at probability 0) beside "dressed last game" as a reference, and the share of dressed skaters who were not candidates. The report also gives the ice time against "last game's minutes": the 5v5 minutes MAE per dressed candidate, and the power-play unit accuracy (the share of the actual top five by power-play minutes the projection named), each with weekly block bootstrap intervals and paired differences, and each season's figures from the season before. The development and held-out seasons show only counts until gate 2. Run it after `nhl goalie-start` and `nhl stints`. It refuses while a season is short of its games, a team-game has no skaters in its boxscore, a team-game it rates has no goalie-start probabilities, or a season it rates has no stints the season before. `--r2` mirrors both tables.
## Odds snapshots

`nhl odds snapshot` runs from `.github/workflows/odds-snapshots.yml`, dispatched at each slot with the slot's name (see When jobs run). Slots are set in US Eastern time, so nothing changes by hand when DST starts or ends. The fallback cron has one line per slot for each UTC offset, and the CLI maps the line that fired to a slot for the current offset. A run first checks the NHL schedule and makes no Odds API call when the slot has no regular-season or playoff game.

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

- **When:** at every odds slot (07:05, 12:45, 18:45, 19:45 and 21:45 ET), as a step in `.github/workflows/odds-snapshots.yml` before the snapshot, so it adds no runs. Those polls cover every regular-season or playoff game starting within 18 hours. `.github/workflows/pregame-goalies.yml` adds a poll at :50 of every hour from 12:50 to 02:50 UTC (15 runs a day) with `--within 75`, so every game starting between 09:00 and 23:00 ET, such as a 09:00 ET start in Europe, also gets one 10 to 70 minutes before its start. The slot poll runs before the odds snapshot, so its goalie rows are observed before the prices of the same slot.
- **What:** for each game in the window that has not started, `GET /v1/gamecenter/{gameId}/boxscore`, `/landing` and `/right-rail`, always fetched fresh (three requests a game, throttled as every NHL call).
- **Raw:** `nhl/pregame-boxscore/<ET game date>/<game_id>/<fetch stamp>`, `nhl/pregame-landing/...` and `nhl/pregame-right-rail/...`, mirrored to R2. They are kept apart from the ingest's `nhl/boxscore/`, which reuses the newest cached boxscore of a game: a pre-game copy there would stand in for the final one. They cannot be fetched again, so `nhl status` compares the whole key sets of each with R2 (`nhl lake sync-raw --prefix nhl/pregame-` or `restore-raw` brings them in step).
- **Signal:** the boxscore's `playerByGameStats.<side>.goalies[].starter` flag, the same one `actual_lineups.starting_goalie` reads after the game. On 2026-09-29, seven to nine hours before FLA at CAR, the boxscore had no `playerByGameStats`, the right-rail had no goalie field, and the landing listed each team's goalies with season stats (`matchup.goalieComparison`) but flagged no starter. When, or whether, the flag appears before puck drop is what the poll measures. The landing and right-rail copies are kept so a later check can look for other signals.
- **Table:** `nhl goalies replay` rebuilds the lake's `pregame_goalies` from the raw boxscores: one row per poll, game and team, with the goalies listed, how many carry the starter flag, and `starter_id` when exactly one does. `observed_utc` is the fetch time, when that state was public; a response fetched at or after the scheduled start gives no rows. The nightly workflow runs `nhl goalies replay --recent 4 --r2`: today, which the evening slots poll ahead, and the three game dates before it.

### Daily Faceoff starting goalies

The same `nhl goalies poll` also fetches Daily Faceoff's starting-goalies page for each US Eastern date that has a game in the poll window (#48; `--no-daily-faceoff` skips it), so the audit can compare how early and how accurately each source names the starter. On 2026-09-29 at 12:10 UTC, Daily Faceoff already listed Tristan Jarry as EDM's confirmed starter for that night, reported the day before, while the NHL flagged no one.

- **Raw:** each page untouched (HTML) as `dailyfaceoff/starting-goalies/<ET date>/<fetch stamp>`, mirrored to R2 and compared in full with R2 by `nhl status`.
- **Parsed from** the JSON in the page's `__NEXT_DATA__` script (`props.pageProps.data`): per game `dateGmt` and, per side, the team name, goalie name and Daily Faceoff id, status (`NewsStrengthName`, such as Likely or Confirmed), `NewsCreatedAt` and `NewsSourceUrl`.
- **Table:** `nhl goalies replay` also rebuilds the lake's `dailyfaceoff_goalies`: one row per page fetch, game and team with the goalie, status, `reported_utc` and source. `observed_utc` is the fetch time, not the report time, since a status can change. Games that had started at the fetch give no rows. Goalies stay names; the audit matches them to NHL player ids. There is no NHL `game_id`; `start_utc` and `team` identify the game.

## SBR odds archive

`nhl odds sbr` imports 2010-11 to 2022-23 into the lake's `sbr_odds` table, one partition per season (#7).
- Each season is one HTML page. The site answers 404 unless the request sends a browser User-Agent, so `ingest/sbr.py` sends one. Pages are stored untouched under `sbr/<season>/` in the raw store, mirrored to `raw/sbr/` in R2, and never fetched again; `--replay` parses the stored pages only. With `--r2` the command first restores the raw pages from R2 and pulls the imported seasons' `schedule` and `games` partitions, then mirrors the table (#52: one season takes seconds when the schedule is already local, and all 13 about 90 seconds on an empty machine).
- A game is two rows. The table keeps the opening and closing moneyline (`h2h`), the closing puck line from 2014-15 (`spreads`) and the opening and closing total. The first row's total price is the over: when it is the favourite, the over lands 53% of the time against 47% otherwise (2010-11 to 2021-22). A price shown as `NL` or blank is left out with the other side's.
- SBR's final score includes OT and the shootout. The moneyline is the full-game line, the same market as the Odds API's `h2h`.
- Rows match NHL regular-season games through `schedule`, on the game date and the pair of teams. The schedule says which team is home, since SBR lists neutral-site games as N and N and once lists H before V. Playoff rows are counted and left out.
- Times (ADR 0006): SBR gives none, so `observed_utc` is `start_utc` for every price and `known_at` shows no SBR price before its game starts. `assumed_available_utc` is E2's assumption of when a price could be bet: the opener at 10:00 US Eastern on the game date (never before the game's schedule is public, never after the start), the close at the start. Only E2 reads it, through `assumed_available_at`, which also drops games already started.
- Join on 2026-09-29: every regular-season game of 2010-11 to 2021-22 has an SBR row (100%). 2022-23 has 342 of 1,312 (26.1%), because the archive stops on 2022-11-27. 14 playoff games of 2020-21, played before the regular season ended, show as unmatched, and 5 games have a final score that disagrees with the NHL's.
- 2022-23's prices (#66, ADR 0025): phase 1 left them unread. Phase 4 opened them to the audit's price checks on 2026-10-04, before any of the season's games is scored. They show the same vig as 2018-19 to 2021-22 (opening median 4.1%, closing 2.4%), no move over 15 points, no puck-line conflict and no opener outside E2's bounds (ADR 0007). The 342 games are scored once, after phase 4's freeze (#145).
- Suspect openers (#56): `reference/sbr_suspect_openers.csv` lists 40 games of 2010-11 to 2022-23 whose opening moneyline is likely wrong (2022-23's 342 priced games, checked from phase 4 on, add none: #66, ADR 0025), with the prices, de-vigged probabilities and closing puck line as evidence. `ingest/sbr_suspect.py` rebuilds it from `sbr_odds` (`uv run python -m nhl_edge.ingest.sbr_suspect`) and loads it (`load_suspect_openers`). There are four criteria: 33 games move more than 15 points from open to close; 8 open outside a 0.15 to 0.85 home probability, while no close does (SBR's page prints them so, such as EDM -1010 / MIN +705); 15 have their sides swapped, meaning a clear favourite at both ends, but opposite teams, and the swapped opener within 5 points of the close (in all 13 with a puck line, the closing puck line agrees with the close); and 3 openers sum below 100%. One more column, `bad_close`, is not a criterion: it marks the 3 listed games whose close, not their opener, is the likely error, because the opener and the closing puck line agree against the close (2015020761, 2015020769 and 2015020783, #64). B1 still fits on those closes. They are 3 of the 9,370 games in the 2018-19 fold's fit, and the owner kept the fit as it is on 2026-09-30. The flags read the close, so each row is observed at the start and the list is hindsight, never a model input. Whether E2 excludes, fixes or keeps these openers is the owner's decision.

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
