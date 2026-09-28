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

- Throttle the NHL API to about 1 request per second and cache every response. Raw responses are stored untouched as JSON under `raw/`, so parser bugs can be fixed and replayed without calling the API again.
- The NHL API is unofficial and changes without notice. A nightly contract test on one golden game catches schema drift.
- Shift chart coverage varies for older seasons. Phase 1 checks coverage per season before RAPM depends on it.
- The Odds API free tier has 500 credits a month. A call costs 1 credit per market per region and returns every game. The slot plan uses about 300 credits a month. Store `last_update` with every quote.
- The Odds API historical endpoint is paid. The `guard-bash` hook blocks it unless Claude Code starts with `ALLOW_PAID_ODDS=1`.
- MoneyPuck data is free for non-commercial use with attribution and must not be scraped. It is a sanity check, not a backtest input.
- Daily Faceoff and RotoWire are manual references only, not part of the automated pipeline.
