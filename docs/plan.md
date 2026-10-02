# NHL Edge Model: Build Plan

Sep 28, 2026 · @Heikki Mattila

## 1. Goal, experiments and success criteria

The goal is a moneyline model that adds information to the market. It is tested in three separately defined experiments against two market baselines, and the player layer must also beat a simpler team-and-goalie model.

| Experiment | Market input | What it establishes |
| --- | --- | --- |
| E1 Information test | De-vigged closing probability | Whether the model adds information beyond the close |
| E2 Tradable prediction | De-vigged price available at the prediction time: SBR opening line historically, live snapshots from October 2026 | Whether the system could have identified a bet at that moment |
| E3 Closing line value | Price taken, compared afterwards with the closing proxy | Whether the prices taken look favorable after the fact |

Every experiment compares four models. B0 is the raw de-vigged market. B1 is a recalibrated market-only model, a logistic regression on the market's log-odds fitted chronologically, which corrects biases such as favorite-longshot without any hockey information. B2 is the team-and-goalie model, and B3 adds the player layer. A model that beats B0 but not B1 is only correcting market calibration.

| Criterion | Measure | Pass |
| --- | --- | --- |
| Adds information | Paired per-game log-loss difference against B1, pooled over test seasons, 95% interval from a weekly block bootstrap | Interval below zero, and no test season losing more than the pooled gain |
| Player layer earns its place | Same paired test, B3 against B2, overall and on games after trades, injuries and lineup changes | Interval below zero overall |
| Calibrated | Calibration intercept and slope with 95% intervals | Intervals include 0 and 1 |
| Favorable prices | Average CLV against the Pinnacle closing proxy under a frozen selection policy, stale quotes excluded | 95% interval above zero |
| Large disagreements | Probability gaps above 8 percentage points | Each reviewed by hand; no quota |

The criteria use intervals because the samples are small. With 1,300 games in ten calibration bins, a correctly calibrated model still shows about 8.6 percentage points of noise either way in each bin.

V1 bets the moneyline only. Non-goals for v1 are totals and puck line bets (their odds are still logged), player chemistry and line interaction terms, live in-game betting, other leagues, automated bet placement and a public UI.

Five principles run through every phase. The market is the baseline to beat, in its recalibrated form. All data is point-in-time: every input has a source observation time and every fitted component a training cutoff before the prediction. Every rating is shrunk toward a prior. Simple models come first, and complexity must earn its place on held-out games. Odds, predictions and bets are logged from day one, because that history cannot be recreated later.

## 2. Architecture, stack and repo layout

The model is a Python batch pipeline that writes to a parquet lake, with Supabase holding only what the dashboard and bet ledger need. Start a fresh repo and a fresh Supabase project. The current app is retired, which frees its free-tier project slot and keeps the old Poisson code out.

&#91;embedded content: pipeline · 12 stages, data to decision\]

Data moves left to right and down. The market blend is the only path to a bet, so the model disagreeing with the market on its own never triggers a stake.

| Layer | Choice | Reason |
| --- | --- | --- |
| Language and env | Python 3.12, uv | One lockfile, fast installs locally and in CI |
| Data processing | Polars, DuckDB | Fast on a laptop, SQL directly over parquet |
| Lake | Parquet on Cloudflare R2 | Free tier covers 10 GB-month with free egress ([R2 pricing](https://developers.cloudflare.com/r2/pricing/)) |
| Serving database | Supabase Postgres | Already familiar; free plan has 500 MB and pauses after 1 week idle, so the daily job keeps it awake ([Supabase pricing](https://supabase.com/pricing)) |
| Models | scikit-learn, statsmodels, scipy | Regularized logistic and ridge regression cover v1 |
| Validation and tests | pandera, pytest | Schema checks on every table, fixtures for known games |
| Code quality | ruff, pyright, pre-commit | Same checks locally, in hooks and in CI |
| Entry point | Typer CLI: `nhl ingest`, `nhl rate`, `nhl predict`, `nhl backtest`, `nhl bets` | One interface for you, Claude Code and GitHub Actions |
| Scheduling | GitHub Actions, dispatched on time by a Cloudflare Worker cron | No server to run; GitHub's own cron started runs 3 to 6 hours late (#50) |
| Dashboard | Next.js on Vercel, reading Supabase | Phase 9 only |

```text
nhl-edge/
├── CLAUDE.md
├── .claude/
│   ├── settings.json        # permissions and hooks
│   ├── hooks/               # hook scripts
│   ├── agents/              # subagent definitions
│   └── skills/              # project skills
├── .github/workflows/       # ci, ingest, odds, backtest, claude
├── docs/
│   ├── decisions/           # one ADR per modeling choice
│   ├── data-sources.md      # endpoints, limits, terms
│   └── model-card.md        # current model, metrics, known weaknesses
├── src/nhl_edge/
│   ├── ingest/              # nhl_api, odds, prospects, lineups
│   ├── lake/                # R2 io, partitioning, schemas
│   ├── features/            # xg, shifts, rest, travel
│   ├── ratings/             # team baseline, rapm, goalie, priors
│   ├── lineup/              # projection, aggregation
│   ├── game/                # win_prob, score_model (only if earned)
│   ├── market/              # devig, recalibration, blend, clv
│   ├── betting/             # selection, kelly, ledger
│   ├── backtest/            # walk_forward, reports
│   └── cli.py
├── tests/
│   ├── unit/
│   ├── golden/              # frozen fixtures for known games
│   └── leakage/             # point-in-time tests
├── notebooks/               # exploration only, never imported
├── web/                     # Next.js dashboard, phase 9
└── pyproject.toml
```

## 3. Data and API plan

Version 1 runs entirely on free sources. Paid data, mainly historical odds after 2022-23, is deferred to v2 and only if v1 shows an edge.

| Source | What it gives | Access and limits | Role in the model |
| --- | --- | --- | --- |
| [NHL API](https://github.com/Zmalski/NHL-API-Reference) (api-web.nhle.com, api.nhle.com/stats/rest) | Schedule, play-by-play with shot coordinates, boxscores, shift charts, rosters, player bios and career stats, draft picks, team prospects | Free, unofficial and undocumented, no published rate limit; throttle to about 1 request per second and cache every response | Backbone: shifts for RAPM, shots for xG, rosters, schedule for rest and travel |
| [MoneyPuck data](https://moneypuck.com/data.htm) | Shot-level data with xG for 2007 to 2026, skater, goalie, team and line stats by season and game, player bios | Free CSV downloads for non-commercial use, attribution required, no scraping | Sanity check for the simple in-house xG model and player stats; not a backtest input, since its xG was likely fitted across many seasons |
| [The Odds API](https://the-odds-api.com/) | Live NHL moneyline, puck line and totals from EU books including Pinnacle; props per event | Free: 500 credits a month. Paid from $30 a month for 20,000 credits. Historical odds from 2020, paid plans only, 10 credits per market per region ([docs](https://the-odds-api.com/liveapi/guides/v4/)) | Live odds snapshots from day one on the free tier; historical backfill deferred to v2 |
| [SBR odds archive](https://www.sportsbookreviewsonline.com/scoresoddsarchives/nhl/nhloddsarchives.htm) | Opening and closing moneylines, puck lines and totals, 2007-08 to 2022-23 | Free Excel files, no longer updated, book source not stated | Opening lines for the historical tradable test (E2), closes for the information test (E1); quality unknown until the phase 1 audit |
| [Elite Prospects API](https://developer.eliteprospects.com/) | Player stats across 900+ leagues including Liiga, SHL, CHL and AHL, draft and transfer history | Free Explorer tier: 1,000 calls a month, 10 per minute; current season free, history is a one-off purchase | Prospect and non-NHL stats for NHLe priors, used sparingly |
| NHLe research ([Bacon](https://towardsdatascience.com/nhl-equivalency-and-prospect-projection-models-building-the-nhl-equivalency-model-part-2-6f275a45e22/), [HockeyStats](https://hockeystats.com/methodology/nhle)) | Published league translation factors and method | Free articles | Starting coefficients for offensive priors only, refit later on your own data |
| [Daily Faceoff](https://www.dailyfaceoff.com/), [RotoWire](https://www.rotowire.com/hockey/starting-goalies.php) | Projected lines, power play units, starting goalie status | Web pages only, no API; RotoWire's terms forbid automated access, Daily Faceoff has none found (checked 2026-09-29) | Daily Faceoff starting goalies logged with the NHL pre-game poll (#48); everything else manual reference only |
| Static files you create | Arena coordinates and time zones, team abbreviation history, coach tenures | One-off CSVs in the repo | Travel, rest and coach-change features |

Odds budget. The free Odds API tier covers live collection when credits are split by time slot. A call costs 1 credit per market in one region and returns every game, so two full snapshots a day (moneyline, puck line and totals, 6 credits), one moneyline and totals snapshot before the 7pm ET starts (2 credits) and two moneyline-only snapshots before the later starts (2 credits) use about 300 credits a month, leaving room for reruns inside the 500 limit. V1 bets only the moneyline, but totals and puck line are still logged because that history cannot be recreated later. Every quote also stores the Odds API last\_update beside the collection time, so stale or suspended prices can be excluded, and the last fresh pre-game snapshot is treated as a closing proxy, never as the true close.

The endpoints below go into `docs/data-sources.md` so Claude Code works from a fixed list instead of guessing URLs.

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

Shift chart coverage for older seasons varies, so phase 1 includes a coverage check per season before RAPM depends on it.

## 4. Data model

Sixteen tables cover v1. Every row records when its underlying fact became public (`observed_utc`), and every output of a fitted component records that component's `train_cutoff` and `artifact_version`. A prediction may only use rows observed before the prediction time and components trained only on earlier data. An `as_of` date alone is not enough: a rating dated January 2019 still leaks if its xG model or shrinkage was fitted on later seasons.

| Table | Grain | Key columns | Stored in |
| --- | --- | --- | --- |
| `games` | One per game | game\_id, season, start\_utc, home, away, final score, decided\_in (REG, OT, SO), flags (neutral site, no fans) | Lake, Supabase |
| `schedule` | One per game | game\_id, season, start\_utc, home, away, venue, neutral\_site, limited\_attendance; no result columns, public 24 hours before the start (ADR 0005) | Lake |
| `shots` | One per unblocked shot attempt | game\_id, event\_idx, period, seconds, x, y, shot\_type, strength, shooter\_id, goalie\_id, is\_goal, is\_empty\_net | Lake |
| `shifts` | One per player shift | game\_id, player\_id, team, period, start\_s, end\_s | Lake |
| `actual_lineups` | Game, player | team, role (F, D, G), sweater\_number, starting\_goalie, toi\_s, observed\_utc; from the boxscore, the only source for backtest lineups | Lake |
| `shift_coverage` | One per game | shift rows kept and dropped, players whose shifts do not match their time on ice, on-ice counts that contradict the strength state, complete | Lake |
| `stints` | One per unchanged on-ice group | game\_id, stint\_id, seconds, home\_skaters, away\_skaters, strength, score\_state, zone\_start, xgf, xga, xg\_version | Lake (RAPM input) |
| `players` | One per player | player\_id, name, birth\_date, position, shoots, draft\_year, draft\_overall | Lake, Supabase |
| `player_league_seasons` | Player, season, league, game type | league\_abbrev, league, teams, games\_played, goals, assists, age\_at\_season, observed\_utc (July 1 after the season; October 1 for the Australian league, the World Cup of Hockey and the Brick Invitational; the end of the late 2019-20 and 2020-21 seasons and of the 2022 World Juniors) | Lake (NHLe input) |
| `team_ratings` | Team, as\_of | ev\_off, ev\_def, pp, pk, sd, train\_cutoff, artifact\_version | Lake; latest in Supabase |
| `player_ratings` | Player, as\_of, component | component (ev\_off, ev\_def, pp, pk, finishing, penalties), mean, sd, train\_cutoff, artifact\_version | Lake; latest in Supabase |
| `goalie_ratings` | Goalie, as\_of | saves\_above\_expected\_per\_shot, sd, train\_cutoff, artifact\_version | Lake; latest in Supabase |
| `lineups` | Game, team, as\_of, player | projected from earlier `actual_lineups`: source (projected, actual), p\_available, expected\_toi by strength, pp\_unit, p\_start for goalies, observed\_utc | Lake, Supabase |
| `odds_snapshots` | Snapshot, game, book, market, side | snapshot\_utc, last\_update\_utc, line, price\_decimal, is\_closing\_proxy | Supabase live; lake for history |
| `predictions` | Game, predicted\_utc, model | market\_price\_used, p\_b1, p\_b2, p\_b3, p\_blend, uncertainty\_score, goalie\_confirmed, artifact versions | Supabase |
| `bets` | One per bet (paper or real) | placed\_utc, side, price\_pinnacle, price\_best\_eu, best\_eu\_book, stake\_units, p\_blend, prob\_gap, exp\_return, close\_proxy\_pinnacle, clv, result, pnl\_units | Supabase |

The lake is partitioned by `season` and `game_date`. Raw API responses are stored untouched as JSON under `raw/`, so any parser bug can be fixed and replayed without calling the API again.

## 5. Model specification and validation

Both hockey models turn team strength into a win probability through the same regularized logistic model, so B2 and B3 differ only in how strength is measured. Home ice, finishing and goaltending each live in exactly one place, which prevents double counting.

### Baseline: team and goalie model (B2)

B2 predicts the full-game home win probability directly, overtime and shootout included, with no player information.

```latex
P(\text{home win}) = \sigma\left(\beta_0 + h_s + \beta_1\,\Delta S + \beta_2\,\Delta G + \beta_3\,\Delta R\right)
```

ΔS is the difference in rolling team strength: 5v5 xG for and against per 60, plus power play and penalty kill xG rates times expected opportunities, decayed over time and shrunk toward league average. ΔG is the difference in goalie effect: each goalie's shrunk goals saved above expected per unblocked shot, times the unblocked shots the opponent is expected to take. ΔR covers rest, back-to-backs and travel, and h\_s is a season-level home term that absorbs eras such as 2020-21 without fans. Coefficients use L2 regularization and past games only.

With an unconfirmed starter, the probability is a mix over goalie scenarios, weighted by the goalie-start model.

```latex
P(\text{home win}) = \sum_{k} \pi_k \, P(\text{home win} \mid \text{goalie pair } k)
```

### Player layer (B3)

B3 replaces both ΔS and ΔG with the expected goal difference built from the projected lineups, inside the same logistic model. Home team 5v5 expected goals:

```latex
\hat{x}^{5v5}_{H} = T^{5v5}\left(\mu_s + \sum_{i \in H} \frac{t_i}{T^{5v5}}\, o_i - \sum_{j \in A} \frac{t_j}{T^{5v5}}\, d_j\right)
```

μ\_s is the season's league 5v5 xG rate per minute, o\_i and d\_j are RAPM offense and defense coefficients per minute, and t\_i is projected 5v5 minutes. Projected minutes must sum to five skaters times the team's expected 5v5 minutes, so the weights always sum to 5.

Power play expected goals are expected opportunities times average power play length times the PP unit's xG rate, reduced by the opposing PK unit. Expected opportunities come from the penalty-drawn and penalty-taken rates of both projected lineups.

```latex
\hat{x}^{PP}_{H} = \hat{n}^{PP}_{H}\,\bar{\ell}\,\left(r^{PP}_{H} - k^{PK}_{A}\right)
```

Goals follow from expected goals through two multipliers, both shrunk toward 1: finishing of the projected shooters, and the opposing goalie's per-shot conversion effect.

```latex
\hat{g}_{H} = \left(\hat{x}^{5v5}_{H} + \hat{x}^{PP}_{H} + \hat{x}^{SH}_{H}\right)\,\phi_{H}\,\gamma_{A}
```

Three rules prevent double counting. Home ice lives only in h\_s, and the RAPM home term only de-biases the ratings. The goalie enters B3 only through γ. The team residual and coaching effect are phase 6 add-ons, fitted only on what B3 leaves unexplained.

RAPM uses stint-length weighting and separate models per strength state. Its shrinkage, decay and aging settings are tuned on log loss of future games, never on in-sample fit or year-to-year stability. Priors come from age curves and draft slot. NHLe translations inform offensive priors only; defense and penalty kill start from a position-level prior with wide uncertainty.

### Score model (phase 6, only if earned)

A Poisson score model is kept only if it beats the logistic model on held-out moneylines. Its base rates exclude empty-net goals, which are modeled separately, and its moneyline comes from:

```latex
P(\text{home win}) = P(\text{home leads after 60}) + P(\text{tie after 60})\,P(\text{home wins OT or SO} \mid \text{tie})
```

The overtime and shootout part is fitted on 2015-16 onward, the 3-on-3 era. Playoffs stay out of v1.

### Uncertainty

Simulation alone does not pull a prediction toward the market, so uncertainty is handled in three explicit places. First, the average probability mixes goalie and availability scenarios by their fitted probabilities. Second, the market blend lets the model's weight depend on an uncertainty score u, built from an unconfirmed goalie, availability doubts and the share of rookie ice time.

```latex
\operatorname{logit} p_{\text{blend}} = a + b_m \operatorname{logit} p_{\text{mkt}} + \left(b_x + b_u u\right) \operatorname{logit} p_{\text{model}}
```

The blend is fitted separately for E1 (p\_mkt is the close) and E2 (p\_mkt is the price at prediction time). Third, a higher u raises the required expected return and lowers the stake. Player-level simulation with correlated rating errors is left out of v1.

### Expected return and CLV

Bets are selected on expected return at the executable decimal price o, never on the probability gap. The stake is a quarter of Kelly.

```latex
\text{EV} = p_{\text{blend}}\,o - 1 \qquad f = \frac{1}{4}\cdot\frac{\text{EV}}{o - 1}
```

A 52.5% blend against a 50% fair price looks like a 2.5-point gap, but at odds of 1.90 its expected return is -0.25%. CLV uses the fair Pinnacle closing proxy: the last snapshot before puck drop whose last\_update is fresh.

```latex
\text{CLV} = o_{\text{taken}}\; p^{\text{fair}}_{\text{close}} - 1
```

### Validation design

Every season is predicted walk-forward by models trained only on earlier data. The table sets which seasons may influence design decisions.

| Seasons | Role |
| --- | --- |
| 2010-11 to 2017-18 | Training history only |
| 2019-20 and 2020-21 | Training only, flagged (bubble, no fans) |
| 2018-19 and 2021-22 | Development: tuning plus market experiments E1 and E2 |
| 2022-23 | Market validation; once inspected, it counts as development evidence |
| 2023-24 and 2024-25 | Hockey-only validation against outcomes (B2 versus B3), no free odds |
| 2025-26 | One-time hockey-only test at gate 2 |
| Live 2026-27 onward | The only untouched market test; a revised model is judged only on games after its freeze date |

The backtest refits every fitted component per fold, from xG to the blend, using only data before the fold. The blend trains only on out-of-sample predictions from earlier folds. Intervals come from a weekly block bootstrap, since games in the same week share conditions.

Lineup quality is scored on its own: availability and goalie starts by Brier score, 5v5 ice time by mean absolute error, and power play units by accuracy. Backtest lineups use only earlier boxscores, because free sources do not timestamp historical roster moves. The selection policy is frozen before live evaluation, and any change restarts the CLV count.

## 6. Project plan

Seven phases and three gates. Gate 1 gives the first verdict: whether a simple team-and-goalie model adds anything to the recalibrated market. The player layer is built after that and must beat the baseline at gate 2. Real money waits until live paper trading passes.

Gate 1 is a checkpoint, not a stop. The player layer is the hypothesis being tested, so it gets built either way, but a baseline that adds nothing lowers expectations for the rest. Phase 6 add-ons run alongside paper trading, which is mostly automated.

| Phase | Deliverables | Exit gate | Status |
| --- | --- | --- | --- |
| 0 Setup | Repo, uv, CLAUDE.md, AGENTS.md, hooks, skills, subagents, CI, new Supabase project (old app retired), R2 bucket | CI green, `nhl --help` runs, hooks fire on a test edit | Not started |
| 1 Data, odds logging, audit | NHL API ingest from 2010-11 with raw cache; SBR import; data audit (missing games, vig distribution, open versus close consistency, team mapping); live odds logging with last\_update; market baselines B0 and B1 | Audit report reviewed; B0 and B1 log loss per season; snapshots landing five times a day | Not started |
| 2 Team and goalie baseline | Simple xG trained chronologically; rolling team strength with decay; goalie per-shot effect; goalie-start model; home and rest terms; regularized logistic B2; E1 and E2 historical tests | Gate 1: B2 versus B1 paired log loss with interval, pooled and per season | Not started |
| 3 Player layer | Stint builder; RAPM tuned on future games; priors (age, draft, NHLe for offense only); lineup projection with measured quality; expected goals into the same logistic (B3) | Gate 2: B3 beats B2 on future games, overall and after trades, injuries and lineup changes, including the one-time 2025-26 test | Not started |
| 4 Market blend and backtest | Chronological blend with uncertainty term; selection on expected return; frozen policy; full E1, E2 and E3 backtest on development seasons and 2022-23 | Gate 3: section 1 criteria pass and the policy is frozen before live games | Not started |
| 5 Paper trading and dashboard | Daily predictions with edge attribution; ledger; CLV against the closing proxy; dashboard; row-level security; raw cache backup | CLV interval above zero on live games under the frozen policy before any real stake | Not started |
| 6 Add-ons | Score model, team residual and coaching, custom xG, richer priors; totals and props in v2 | Each kept only if it improves held-out forecasts | Not started |

## 7. Claude Code setup

Claude Code gets four layers: a short CLAUDE.md with the hard rules, four subagents for focused work and review, six skills for repeatable procedures, and hooks that enforce what must never be left to judgment. File locations follow the current docs for [skills](https://code.claude.com/docs/en/skills) and [hooks](https://code.claude.com/docs/en/hooks).

### CLAUDE.md

Keep it under about 100 lines. It is loaded into every session, so it holds rules and commands, not explanations.

```markdown
# NHL Edge Model

Moneyline model that must add information beyond a recalibrated market.
Read docs/plan.md section 5 for the model specification, docs/model-card.md for current state,
and docs/decisions/ before changing a modeling choice.

## Commands
- `uv sync` install, `uv run nhl --help` CLI
- `uv run pytest -m "not slow"` fast tests, `uv run pytest` full suite
- `uv run ruff check . && uv run pyright` lint and types
- `uv run nhl backtest` walk-forward backtest on development seasons

## Hard rules
1. Point-in-time only. Every input has observed_utc before the prediction time, and every fitted
   component has train_cutoff before the fold start. The backtest refits everything per fold.
   Every new feature gets a test in tests/leakage/.
2. The moneyline settles on the full game including OT and shootout. Never compare a regulation
   probability with a moneyline price.
3. De-vig only with market/devig.py. Every model is compared with B1 (recalibrated market), and
   the player layer (B3) with the team-and-goalie model (B2).
4. Select bets on expected return at the executable price (p * odds - 1), never on probability gap.
5. The last fresh pre-game snapshot is a closing proxy. Store last_update with every quote.
6. The blend trains only on out-of-sample predictions from earlier folds.
7. Report metrics with intervals from a weekly block bootstrap. No pass or fail on point estimates.
8. Probability gaps above 8 points get a manual review. Never tune a model to make them disappear.
9. Backtest lineups use only earlier boxscores. Never use a game's own lineup to predict it.
10. Never modify data/raw/ or tests/golden/. Never call paid endpoints unless the prompt says so.

## Conventions
- Python 3.12, Polars in src/, type hints everywhere, one pandera schema per table in lake/schemas.py
- All times UTC; seasons as 20252026; team codes as NHL API triCode
- Artifact version string: <component>-<yyyymmdd>-<shortsha>, stored with train_cutoff on every output
- One modeling change per PR, with the backtest delta and its interval in the PR description

## Workflow
- Start each phase in plan mode and paste the approved plan into the PR description
- After changing features or models: /leakage-check, then /run-backtest
- Record modeling decisions with /adr
```

### Subagents

Subagents live in `.claude/agents/<name>.md`. Two of them build, two of them only review, and the reviewers have no edit tools so they cannot quietly fix what they should be reporting.

| Agent | Job | Tools | When |
| --- | --- | --- | --- |
| `data-engineer` | API clients, parsers, lake layout, schemas, ingest jobs | Read, Edit, Write, Bash, Grep, Glob | Phases 1 and 5 |
| `modeler` | Simple xG, team baseline, RAPM, priors, win-probability model, market blend | Read, Edit, Write, Bash, Grep, Glob | Phases 2 to 4 and 6 |
| `leakage-auditor` | Point-in-time review of any feature, join or backtest change | Read, Grep, Glob, Bash | Before every modeling merge |
| `backtest-reviewer` | Checks backtest reports against section 1 criteria, hunts for suspicious edges | Read, Grep, Glob, Bash | After every backtest |

```markdown
---
name: leakage-auditor
description: Read-only reviewer for look-ahead leakage. Use after any change to features, ratings,
  lineups, the blend or backtest code, and before merging a modeling PR.
tools: Read, Grep, Glob, Bash
model: opus
---
You audit an NHL betting model for look-ahead leakage. You never edit files.

For every changed feature, join, fitted component or aggregation, answer one question: at the
moment a bet could be placed (the odds snapshot time, not puck drop), was every input already
public, and was every fitted component trained only on earlier data?

Check in particular:
- joins without an observed_utc filter, and rolling windows that include the current game
- fitted components (xG, ratings, priors, shrinkage, aging curves, hyperparameters) whose
  train_cutoff is after the fold start, or that are not refit per fold
- the blend trained on in-sample predictions or on later folds
- backtest lineups or starting goalies taken from the game itself instead of earlier boxscores
- closing odds used as an input to a tradable (E2) prediction
- design decisions tuned on 2022-23 or 2025-26 without being logged as development use

Run `uv run pytest tests/leakage -q`. Report each finding as file:line, what leaks, and the fix.
End with PASS or FAIL.
```

### Skills

Skills live in `.claude/skills/<name>/SKILL.md` and appear as slash commands. The `nhl-domain` skill is reference material that Claude loads on its own whenever it touches odds or game outcomes, which targets exactly the regulation versus full-game and vig mistakes you hit before.

| Skill | Kind | What it does |
| --- | --- | --- |
| `nhl-domain` | Reference, auto-loaded | Market settlement rules, de-vig methods (multiplicative, power, Shin), CLV formula, OT and shootout structure, empty-net behavior, known pitfalls |
| `add-data-source` | Procedure | Adds a source end to end: docs entry, raw cache, parser, schema, fixture, tests |
| `leakage-check` | Procedure, forks into `leakage-auditor` | Runs the auditor on the current diff |
| `run-backtest` | Procedure | Runs the walk-forward backtest, compares with baseline and last accepted model, updates the model card |
| `adr` | Procedure | Writes a decision record from a template into docs/decisions/ |
| `daily-slate` | Procedure | Runs today's predictions and lists edges, flags and lineup gaps for manual review, with each edge attributed to goalie, players, rest or team residual |

```markdown
---
name: run-backtest
description: Run the walk-forward backtest for B0 to B3 and the blend, compare with the last accepted
  model, and update the model card. Use after any model or feature change.
allowed-tools: Bash(uv run nhl backtest *) Bash(uv run pytest *)
---
1. Run `uv run pytest tests/leakage -q`. Stop if it fails.
2. Run `uv run nhl backtest --seasons $ARGUMENTS --out reports/backtest/` (default: development
   seasons). Never include 2025-26 or live games unless I say so explicitly.
3. From reports/backtest/summary.json, report with 95% weekly block bootstrap intervals: paired
   log-loss difference of B2 and B3 against B1 and of B3 against B2, pooled and per season;
   calibration intercept and slope; E3 CLV under the frozen policy; count of gaps above 8 points.
4. Compare with reports/backtest/accepted.json and flag any season that got worse.
5. Propose updating accepted.json only if the intervals support it. Never overwrite it without my
   confirmation.
6. Update docs/model-card.md and log the run (seasons, artifact versions) in reports/backtest/runs.csv.
```

### Hooks and permissions

Hooks enforce four things: protected paths stay untouched, paid and destructive commands are blocked, Python is formatted on every edit, and Claude cannot finish a turn with failing fast tests.

```json
{
  "permissions": {
    "allow": ["Bash(uv run *)", "Bash(uv sync)", "Bash(git status)", "Bash(git diff *)",
              "Bash(git log *)", "Bash(gh pr *)"],
    "deny": ["Read(./.env)", "Read(./.env.*)", "Bash(git push --force *)"]
  },
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "uv run nhl status --brief"}]}
    ],
    "PreToolUse": [
      {"matcher": "Edit|Write", "hooks": [{"type": "command",
        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/protect-paths.sh"}]},
      {"matcher": "Bash", "hooks": [{"type": "command",
        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard-bash.sh"}]}
    ],
    "PostToolUse": [
      {"matcher": "Edit|Write", "hooks": [{"type": "command",
        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/format-python.sh"}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "timeout": 300,
        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/stop-checks.sh"}]}
    ]
  }
}
```

```bash
#!/usr/bin/env bash
# .claude/hooks/protect-paths.sh
path=$(jq -r '.tool_input.file_path // empty')
case "$path" in
  */data/raw/*|*/tests/golden/*|*/.env|*/.env.*)
    echo "Blocked: $path is protected. Change raw data and golden fixtures by hand only." >&2
    exit 2 ;;
esac
exit 0

#!/usr/bin/env bash
# .claude/hooks/guard-bash.sh
cmd=$(jq -r '.tool_input.command // empty')
if echo "$cmd" | grep -qE 'the-odds-api\.com/v4/historical|nhl odds backfill' \
   && [ "${ALLOW_PAID_ODDS:-0}" != "1" ]; then
  echo "Blocked: paid historical odds call. Start Claude with ALLOW_PAID_ODDS=1 to allow." >&2
  exit 2
fi
if echo "$cmd" | grep -qE 'rm -rf|git reset --hard|git push (-f|--force)'; then
  echo "Blocked: destructive command." >&2
  exit 2
fi
exit 0

#!/usr/bin/env bash
# .claude/hooks/format-python.sh
path=$(jq -r '.tool_input.file_path // empty')
[[ "$path" == *.py ]] || exit 0
uv run ruff format "$path" >/dev/null
if ! out=$(uv run ruff check --fix "$path" 2>&1); then echo "$out" >&2; exit 2; fi
exit 0

#!/usr/bin/env bash
# .claude/hooks/stop-checks.sh
input=$(cat)
[ "$(echo "$input" | jq -r '.stop_hook_active')" = "true" ] && exit 0   # no loops
git diff --quiet HEAD -- '*.py' && exit 0                                  # nothing changed
out=$(uv run pytest -m "not slow" -q -x 2>&1) || { echo "$out" | tail -30 >&2; exit 2; }
out=$(uv run pyright src 2>&1) || { echo "$out" | tail -30 >&2; exit 2; }
exit 0
```

Exit code 2 blocks the action and sends the message back to Claude, so a failing test makes it keep working instead of stopping. The `stop_hook_active` check prevents an endless loop when a test cannot be fixed.

### Optional MCP

The Supabase MCP server in read-only mode lets Claude query predictions, odds and bets directly while debugging. Add it in phase 9, not earlier; before that, everything it needs is in the lake.

### Working method

Run each phase as one branch and one PR, reviewed automatically by Codex. Open the phase in plan mode, approve the plan, then let Claude build with the relevant subagent and finish with `/leakage-check` and `/run-backtest`. Use `/clear` between unrelated tasks so old context does not steer new work. Early in phase 1, freeze five golden games in tests/golden/: a regulation win, an OT win, a shootout, a late empty-net goal and a game with a 5-on-3, since those are the cases parsers and settlement logic get wrong.

## 8. GitHub setup

One private repo, a protected main branch, six workflows, Codex for pull request reviews and one milestone per phase. The Actions setup fits inside the free 2,000 minutes a month for private repos, at roughly 900 minutes of expected use.

### Repository

Protect `main`: require a pull request, require the `ci` check to pass, block force pushes. Branches follow `phase-N/<topic>`. Add Dependabot for uv and GitHub Actions updates, and a PR template with four checkboxes: leakage check run, backtest delta pasted, ADR written if a modeling choice changed, model card updated.

Create milestones `P0 Setup` through `P6 Add-ons` matching section 6, and labels `data`, `model`, `infra`, `leakage`, `experiment`, `bug`. An `experiment` issue template asks for the hypothesis, the change, the metric you expect to move and the result, so failed ideas stay documented.

### Secrets

| Secret | Used by |
| --- | --- |
| `ODDS_API_KEY` | odds-snapshots |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | ingest, odds, predict |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | ingest, predict, backtest |
| `CLAUDE_CODE_OAUTH_TOKEN` | claude.yml only, if you keep it (set by `/install-github-app`) |

Codex review needs no secret; it runs through the Codex GitHub integration.

### Workflows

| File | Trigger | What it does | Minutes a month |
| --- | --- | --- | --- |
| `ci.yml` | Push and PR | uv sync, ruff, pyright, fast tests, leakage tests | About 150 |
| `ingest-nightly.yml` | Daily 09:00 UTC | Yesterday's games, shifts and rosters to the lake; schema checks; rating refresh; keeps Supabase awake | About 300 |
| `odds-snapshots.yml` | 5 times a day | Checks the NHL schedule first, then pulls the markets set for that slot (full set twice a day, moneyline near starts) into `odds_snapshots` | About 150 |
| `predict-daily.yml` | Daily 16:00 UTC, then each odds slot | Projected lineups, goalie-start probabilities, predictions and paper bets; later runs pick up confirmed goalies (phase 9) | About 150 |
| `backtest-weekly.yml` | Monday 06:00 UTC and manual | Full walk-forward backtest, report uploaded as an artifact, opens an issue if metrics regress | About 120 |
| `claude.yml` (optional) | `@claude` in issues and PRs | [claude-code-action](https://github.com/anthropics/claude-code-action) v1 for questions and small fixes | Varies |
| Codex code review (no file) | Every PR opened or updated | Automatic reviews turned on in Codex settings, following the Review guidelines in AGENTS.md ([Codex docs](https://developers.openai.com/codex/integrations/github)) | None |

```yaml
# .github/workflows/ci.yml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --frozen
      - run: uv run ruff check .
      - run: uv run pyright src
      - run: uv run pytest -m "not slow" -q
      - run: uv run pytest tests/leakage -q
```

```yaml
# .github/workflows/odds-snapshots.yml  (times in UTC, set for winter; shift 1h after DST ends)
name: odds-snapshots
on:
  schedule:
    - cron: "0 12 * * *"    # morning line
    - cron: "45 17 * * *"   # after morning skates
    - cron: "45 23 * * *"   # before 7pm ET starts
    - cron: "45 0 * * *"    # before 8pm ET starts
    - cron: "45 2 * * *"    # before 10pm ET starts
  workflow_dispatch:
concurrency: odds
jobs:
  snapshot:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --frozen
      - run: uv run nhl odds snapshot --regions eu --slot-plan free-tier --skip-if-no-games   # markets per slot, about 300 credits a month
        env:
          ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
```

For Codex, turn on Code review and Automatic reviews for the repo in Codex settings. It flags only P0 and P1 issues and applies the Review guidelines section of the nearest AGENTS.md, so the project's review rules go there. They mirror the CLAUDE.md hard rules, so update both files together.

```markdown
# AGENTS.md
# NHL Edge Model
Codex applies the section below when reviewing. It mirrors the hard rules in CLAUDE.md.

## Review guidelines
- Flag any feature, join or rolling window that could use data not public at bet time
  (observed_utc must be earlier than the odds snapshot used for the bet).
- Flag any fitted component (xG, ratings, priors, shrinkage, blend) trained on data after its fold start.
- Flag any comparison of a regulation probability with a moneyline price.
- Flag odds used without de-vig through market/devig.py, and closing odds used in a tradable prediction.
- Flag bet selection or staking based on probability gap instead of expected return at the
  executable price.
- Flag backtests that use a game's own lineup or starting goalie instead of earlier boxscores.
- Flag metrics reported as pass or fail without intervals.
- Flag new features or model code without tests, any change to data/raw/ or tests/golden/,
  and calls to paid endpoints such as the Odds API historical API.
```

GitHub scheduled runs started 3 to 6 hours late, so a Cloudflare Worker cron dispatches the workflows at the slot times instead (#50, `infra/timer`). Fixed slots still give a rough closing line rather than an exact one. Section 9 covers the fix.

## 9. Gaps and risks

The three risks most likely to hurt are unknown historical odds quality, lineup news the market has and the model lacks, and repeated tuning to the test data. Each has a mitigation inside the free-first approach.

| Gap | Why it matters | Mitigation |
| --- | --- | --- |
| No free historical odds after 2022-23 | SBR stops at 2022-23 and does not state its book, so benchmark quality is unknown | Phase 1 audit of SBR (vig, missing games, open versus close); 2023-26 outcomes still test the hockey models; the true Pinnacle close comes only from live snapshots; the paid backfill (about $90) is the first v2 upgrade |
| Closing proxy quality | Fixed snapshots, delayed cron runs and suspended markets blur CLV when the target is small | Store last\_update and exclude stale quotes; moneyline snapshots 15 minutes before each start cluster; a per-game scheduler on the $30 plan in v2 |
| Lineup news the model lacks | The market may already know a player or goalie is out while the model still counts him | Measured lineup quality; mixtures over fitted availability and goalie-start probabilities; skip bets where the model sides against a sharp market move since the morning snapshot |
| Backtest versus live information | Free sources do not timestamp roster moves, so the backtest cannot reproduce the daily roster pull | Backtest lineups from earlier boxscores only, which makes the backtest slightly conservative |
| Double counting in aggregation | Home ice, goalie and team effects can enter twice when ratings are combined | Written specification in section 5: one home term, goalie only through γ in B3, team residual fitted last |
| Prospect and European data depth | Elite Prospects free tier is 1,000 calls a month with history paid; NHL API career stats cover only players in the NHL system | NHL API career stats for drafted and signed players, Elite Prospects for in-season updates of top prospects, wide priors for undrafted European signings |
| NHL API is unofficial | Endpoints change without notice, as when the old statsapi was retired in 2023 | Raw JSON cache, a nightly contract test on one golden game, alert on schema drift |
| Shift chart errors | Missing or inconsistent shifts corrupt RAPM | Per-season coverage report, stints only from complete charts, drop stints whose on-ice counts are impossible (ADR 0015) |
| Tuning to the test data | About 1,300 games a season and many modeling choices | Season roles fixed in section 5; every backtest run logged; 2022-23 counts as development once inspected; live 2026-27 is the only untouched market test |
| No moneyline edge exists | Closing moneylines are close to efficient | Gate 1 shows early whether even the baseline adds information; if B2 and B3 both fail against B1, v2 shifts to totals, props and timing edges |
| Where you can bet | Until 1 July 2027 only Veikkaus is licensed in Finland; soft books also limit winning accounts | Paper trade until the licensed market opens, then track limits per book in the ledger |
| Data terms | MoneyPuck is free for non-commercial use with attribution | Fine for personal use; a license is needed if this ever becomes commercial |
| Free tier ceilings | Supabase 500 MB and pausing, R2 10 GB | Bulk history in R2, only recent odds and live tables in Supabase, snapshots older than two seasons pruned from Supabase |

### Decisions

- [x] Buy the historical odds backfill (about $90) or accept the SBR-only benchmark for v1. Decided: v1 stays fully free, paid APIs only after v1 proves an edge
- [x] Accept manual lineup and goalie entry for v1. Decided: no manual entry, lineups and goalies are fully automated
- [x] Confirm the 8 to 10 hours a week pace behind the roadmap. No longer needed: the plan carries no dates

| Decision | Choice |
| --- | --- |
| Build order | Team-and-goalie baseline first; the player layer must beat it at gate 2 |
| Test seasons | 2018-19 and 2021-22 development, 2022-23 market validation, 2023-24 and 2024-25 hockey-only validation, 2025-26 one-time hockey test, live 2026-27 final market test |
| Markets | Moneyline only in v1 |
| Paper bet price | Log Pinnacle and best EU book on every bet; the gate uses Pinnacle |
| Supabase | Retire the old app and start a new project |
| Staking | Quarter Kelly on expected return at the executable price, 1.5% cap per bet, 5% per day, 2.5% minimum expected return |
| Paper bankroll | 100 units |
| PR reviews | Codex automatic reviews, guided by AGENTS.md |

## 10. Design safeguards

Seven safeguards protect against false edges that a clean backtest can still hide. Section 5 holds the equations; this section holds the operating rules.

### Rule eras and playoffs

Home ice is a season-level term, and 2019-20 and 2020-21 carry flags for the bubble and empty arenas. League scoring moves over time, so the baseline rate is set per season. If a score model is added in phase 6, its overtime part uses only 2015-16 onward. Playoffs stay out of v1.

### Uncertainty handled explicitly

Uncertainty does not shrink an edge by itself: a 50/50 mix of a 50% and a 70% scenario is still 60%. The plan therefore handles it in three places. Scenario mixtures set the average probability, an uncertainty term in the market blend lets the data decide how much to trust the model, and uncertain games need a higher expected return and get smaller stakes.

### Lineups and goalies without manual work

The projected lineup starts from each team's last dressed lineup, taken from earlier boxscores. Availability is a fitted probability per player, trained on past games using signals such as games missed, early exits and roster changes, not a fixed rule. A call-up gets a role and ice time from his own history and the team's usage pattern instead of inheriting the missing player's minutes.

Goalie starts come from the goalie-start model, and phase 1 checks whether NHL pre-game data confirms starters early enough to use. Lineup quality is scored separately, as section 5 describes. Manual overrides stay possible but are never required.

### Market move guard

When the moneyline moved sharply between the morning and pre-game snapshots and the model sides against that move, the bet is skipped. That pattern usually means goalie or injury news the model does not have. The threshold is set on development seasons using SBR open versus close, then frozen.

### Edge attribution

For every flagged bet, the daily slate shows which inputs drive the gap to the market: starting goalie, individual player ratings, rest and travel, or the team residual. A bug shows up as an edge explained by one odd input, and over a season the attribution shows which parts of the model carry the edge.

### Regression tests from the previous app

The vig and regulation versus full-game bugs from the current app become golden tests in phase 1, so the new build cannot repeat them.

### Security and backups

Before the dashboard goes live, Supabase gets row-level security and the service role key stays server-side, never in the Next.js client. The raw JSON cache gets a second copy outside R2, since it holds data that cannot be downloaded again if the NHL API changes.

## 11. What else to set up

Six things are easy to skip and expensive to add later: staking rules, the bet ledger, model versioning, the test layers, a decision log and a clean first session.

### Staking rules

Track a paper bankroll of 100 units. A bet needs an expected return of at least 2.5% at the executable price, higher when the uncertainty score is high. Stake quarter Kelly at that price, capped at 1.5% of bankroll per bet and 5% per day, with at most one bet per game. The selection policy is frozen before live evaluation. A 20% drawdown triggers a review of data and code, not a model change based on results.

### Bet ledger and CLV

Every paper and real bet goes into `bets` with the Pinnacle price, the best EU book price and the Pinnacle close, so CLV is computed automatically. The gate is judged on Pinnacle; the EU price shows what a soft book would have paid. Review CLV with its interval every week and profit only once a season, since results over a few hundred bets are mostly noise.

### Model versioning

Every prediction row stores its model version. `reports/backtest/accepted.json` holds the metrics of the current production model, and `docs/model-card.md` states what it does, how it scored and its known weaknesses. A new version replaces it only through the run-backtest skill and your confirmation.

### Test layers

| Layer | Catches |
| --- | --- |
| Unit tests | Parsing, feature math, de-vig (probabilities sum to 1, symmetric cases) |
| Golden games | Settlement of regulation, OT, shootout, empty-net and 5-on-3 cases on frozen real data, plus the vig and regulation versus full-game cases from the previous app |
| Leakage tests | Any input observed, or any component trained, at or after the prediction time |
| Contract test (nightly) | NHL API schema changes |
| Backtest regression (weekly) | A merge that quietly made the model worse |

### Decision log

Every modeling choice gets a one-page ADR in `docs/decisions/`: context, options, decision, backtest evidence. Dropping chemistry is ADR 0001, so the reasoning is on record if you revisit it.

### Running cost

Version 1 is fully free: every data source, storage layer and scheduler runs on a free tier, and Claude Code usage is the only variable cost. If v1 passes the paper-trading gate, the v2 upgrades in order of value are the historical odds backfill (about $90 one-off), the Odds API $30 a month plan for per-game closing snapshots and props, and Elite Prospects history for stronger prospect priors.

### First session in Claude Code

Export this doc as markdown into the new repo as `docs/plan.md`, then start Claude Code in plan mode with this prompt:

```text
Read docs/plan.md. We are in phase 0. Set up the repository exactly as sections 2, 5, 7 and 8 describe:
pyproject with uv and Python 3.12, the src/nhl_edge layout, a Typer CLI with stub commands
(ingest, rate, predict, backtest, bets, odds, status), CLAUDE.md, AGENTS.md with Codex review guidelines, .claude/settings.json with the
four hook scripts, the four subagents, skeleton SKILL.md files for the six skills, ci.yml,
.env.example, the PR template, and ADRs 0001 (chemistry out of scope) and 0002 (baseline-first build order).
Do not implement any ingestion or modeling yet. Show me the plan first, then build it and
confirm the hooks fire with a test edit to data/raw/.
```

## Sources

- [NHL API reference (unofficial)](https://github.com/Zmalski/NHL-API-Reference)
- [MoneyPuck data downloads and terms](https://moneypuck.com/data.htm)
- [The Odds API plans](https://the-odds-api.com/) and [v4 docs](https://the-odds-api.com/liveapi/guides/v4/)
- [SBR NHL odds archive](https://www.sportsbookreviewsonline.com/scoresoddsarchives/nhl/nhloddsarchives.htm)
- [Elite Prospects API](https://developer.eliteprospects.com/)
- [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/)
- [Supabase pricing](https://supabase.com/pricing)
- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks) and [skills](https://code.claude.com/docs/en/skills)
- [claude-code-action](https://github.com/anthropics/claude-code-action)
- [Finland licensing timeline, iGaming Business](https://igamingbusiness.com/legal-compliance/licensing/finland-gambling-licensing-market-liberalisation/)
