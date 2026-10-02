# 0016. Season totals from late landing pages count as public at the season's end, corrections included

- Status: Accepted
- Date: 2026-10-02

## Context

`player_league_seasons` (#98) holds each player's goals, assists and games played per league-season, from the `seasonTotals` of his NHL landing page. It is the input of the NHLe offensive priors (#102). The backfill fetched every page in September 2026, years after most of the seasons, and the NHL keeps no dated copies of a page. So a season total in the table may include corrections made after that season ended.

The table already decides when a row is public: the later of the season's end (July 1, or later for the late leagues and seasons) and the player's first NHL boxscore (PR #116). ADR 0004 accepted post-game corrections for the per-game tables, but left assists, points and other scoring credits out of them, and said a feature that wants them needs a new decision first. Codex's second review of PR #116 raised exactly this: a total corrected after the season would be dated to the season's end.

## Options

1. **Date the 2026 copies at the season's end, corrections included.** This is ADR 0004's reasoning, extended to season totals. The cost is that the backtest can see a total corrected after the season, which live would not have seen at the time.
2. **Keep games played only.** No scoring credits, so no correction risk to them. But the NHLe priors lose points per game, the input plan §5 builds them on, and offensive priors would rest on age, draft slot and role alone.
3. **Date every line at its page's fetch.** Strictly point in time, but all 2010-26 lines would become public in September 2026. The backtest could never use them, so the priors could not be tested before gate 2.

## Decision

Option 1, chosen by the owner on 2026-10-02.

- **Why the risk is small.** A correction describes the same finished season more accurately and carries no news about any later game. Most scoring changes are made within days of the game, long before the July 1 after the season.
- **How the priors use it.** The totals enter as points per game over whole seasons, pooled across players into league factors, then shrunk. So a moved assist barely changes a prior.
- **The risk is unmeasured.** Like ADR 0004's, it favours the backtest.

**Scope:**
- The decision covers `goals`, `assists` and `games_played` in `player_league_seasons` only.
- The per-game tables keep ADR 0004's rule: no assists or points.
- A per-game or in-season use of scoring credits still needs its own decision.

## Backtest evidence

None yet. The NHLe priors (#102) are the first component to read these columns, and B3's walk-forward with them scores the effect against B2 at gate 2. The correction rate itself is unmeasured. #117's yearly refetch of the landing pages will give two copies of each page, whose finished-season totals can be diffed.

## Consequences

- **The table** keeps goals and assists with `observed_utc` as above. Its leakage test locks the column set, so a new column needs a look at its timing first.
- **Live and backtest.** Live reads pages fetched when a player first shows up. For finished seasons these are as corrected as the backfill's, so the gap is in how late a correction can arrive, not in the source.
- **Docs.** `docs/data-sources.md` and the schema's docstring cite this decision where they cited ADR 0004 for these pages.
- **#117** should report, when it first refetches, how many finished-season lines changed and in which columns.

## Revisit when

- **#117's diff** shows finished-season goals or assists changing in more than a few lines a season.
- **Or a component** starts using these totals for anything but season-level priors, such as in-season form.
