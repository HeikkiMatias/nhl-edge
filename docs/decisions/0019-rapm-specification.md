# 0019. RAPM: ridge ratings per strength state, refit every game day, with an arena term

- Status: Accepted
- Date: 2026-10-02

## Context

B3 needs each skater's offense and defense per minute (docs/plan.md §5): T·(μ_s + Σ_home (t_i/T)·o_i − Σ_away (t_j/T)·d_j), and the same for the power play against the penalty kill. #101 (phase 3 task 7) builds them as RAPM, ridge regression on stints. The owner decided on 2026-10-01 that ratings refresh every game day from the stints public before each game, as team strength does. Only the memory, the pull and aging are tuned, in #103 (ADR 0011). Until then the settings are provisional, and this ADR fixes the specification before any fit is scored.

The counts behind it come from the training seasons 2011-12 to 2017-18, from the stints of games with a complete shift chart (ADR 0015). No fit was scored.
- **5v5:** 1.81 million kept stints, 6,417 hours. The median stint lasts 8 seconds, and 17.7% of stints open with a faceoff.
- **Who is on the ice at 5v5:** 99.3% of the time it is three forwards and two defensemen (2015-16 to 2017-18).
- **Arenas differ.** A team's home games and its road games are compared on total 5v5 xG per hour, both teams together. The home-to-road ratio varies between teams with a spread of about 0.11 each season, against about 0.05 that chance alone would give. An arena that runs high one season tends to run high the next (correlation 0.71). Without a term for it, the players of a team whose arena runs high look better on offense and worse on defense than they are.
- **The power play:**
  - by time: 5v4 96.6%, 5v3 2.0%, 4v3 1.4%, and power plays with an empty net 0.9%;
  - on the 5v4 power play, 51% of the time has one defenseman and 48% two;
  - on the 5v4 penalty kill, 99.7% has two (2014-15 to 2016-17).

## Options

The owner chose among these on 2026-10-02:
1. **An arena term at 5v5** (chosen), or only the terms #101 lists: the season's league rate, home, score and zone.
2. **The power play's situations: 5v4, 5v3 and 4v3, with a term for each** (chosen), or 5v4 only.
3. **Provisional settings until #103:**
   - **a half-life of one season of league game days and a pull worth 20 hours** (chosen);
   - half a season and 10 hours;
   - two seasons and 40 hours.

## Decision

**Rows.** Only stints RAPM keeps (ADR 0015) with xG (2011-12 on) and both goalies in net count.
- **5v5:** each 5v5 stint gives two rows, one per team attacking.
- **Power play:** each stint at 5v4, 5v3 or 4v3 gives one row, the team on the power play attacking.
- **The response** is the attacking team's xG per hour in the stint.
- **The weight** is the stint's length in hours times its decay.
- 4v4, 3v3 and play with an empty net are not rated.

**5v5 model.** xG per hour = intercept + season + arena + home + score + zone + Σ attacking skaters' offense − Σ defending skaters' defense.
- **Season:** the season's departure from the intercept, the league rate.
- **Arena:** the arena's departure, the same for both teams. The arena comes from the venue through `venues.csv`, so it follows a building through renames.
- **Home:** the attacking team is at home, and the game is not at a neutral site.
- **Score:** the attacking team's lead at the stint's start, capped at two either way. Tied is the base.
- **Zone:** the faceoff that opens the stint, from the attacking team's side (offensive, neutral or defensive). A change on the fly is the base.

**Power-play model.** The power play's xG per hour = intercept + season + home + score + zone + situation + defensemen + Σ power-play skaters' power-play offense − Σ penalty-kill skaters' penalty-kill defense.
- **Situation:** 5v3 or 4v3; 5v4 is the base.
- **Defensemen:** the number of defensemen on the power play. Its term is a defenseman's average there against a forward's, and a defenseman's stored power-play rating adds it back.
- There is no arena term. Shorthanded offense and power-play defense are not rated (plan §5); #104 gives shorthanded xG from the league rate.

**What the terms are for.** Every term other than the players' only removes bias from the ratings (plan §5). Home ice lives in h_s, and goalies enter B3 only through γ.

**The pull.** Ridge regression pulls every player's rating toward 0 by 20 hours of ice time: about what one more season of a regular forward's 5v5 time at the average would do.
- **0 is the average skater** at 5v5 and on the penalty kill, the position average #101 asks for until the priors (#102). With 99.3% and 99.7% of the time in one mix of forwards and defensemen, a forward's and a defenseman's averages cannot be told apart there.
- **On the power play,** 0 is the average forward, and the defensemen term carries the defensemen's average.
- **The season and arena terms** get a pull of 1 hour, only to keep the fit solvable. The other terms get none.

**The memory.** A stint's decay is 0.5^(k/180), k the league game days from its date to the latest date read. Only dates with regular-season games count, so the summer does not decay a rating.

**Refits.** Every game's ratings read only the stints public before its as-of time (team strength's: 10:00 US Eastern on the game date, or an hour before the start if that is earlier). With ADR 0004, that means every game up to the day before. The fit keeps its weighted sums from day to day, so each game day costs one solve; there are no approximations.

**What is stored** (`nhl rapm`):
- **`player_ratings`:** for every candidate skater in `lineups` (ADR 0017), per game, four components: `ev_off`, `ev_def`, `pp` and `pk`.
  - `mean` is in xG per hour of ice time. Offense raises his team's rate, and defense lowers the other team's.
  - `sd` is the ridge's posterior spread, and `hours` the decayed ice time behind the rating.
  - A player without data gets 0 (a defenseman's power-play average on the power play), the prior's sd and 0 hours.
- **`rapm_terms`:** the terms of each refit, with sigma, the residual sd per square-root hour. Ratings are relative to a skater rated 0, so B3 reads its rates from the intercept and season terms, not the league's observed rate.
- **Time stamps.** `train_cutoff` is the end of the training seasons, whose figures this specification read, as for team strength (ADR 0011). A rating's `observed_utc` is the later of its as-of time and that cutoff.

## Backtest evidence

None yet. #103 tunes the memory, the pull and aging on 5v5 (ADR 0011), and the power-play model reuses them. B3's walk-forward (#106) and gate 2 (#107) score the ratings through B3 against B2. The first run, `rapm-<yyyymmdd>-<sha>`, will be quoted on #101's PR with its counts and terms, but no score.

## Consequences

- **B3 (#106)** reads `ev_off`, `ev_def`, `pp` and `pk` with the lineup's expected minutes (ADR 0018), and its rates from `rapm_terms`.
- **The first ratings start cold.** 2011-12 opens with every rating at 0, since 2010-11 has no xG. Tuning (#103) scores 2012-13 on.
- **New players** stay at 0 until the priors (#102) give them a starting point by age, draft slot and NHLe.
- **The arena term** removes arena differences whatever their cause, the scorers or the building. It feeds no prediction.
- **Corrections:** ratings come from stints, which post-game corrections can change (ADR 0004, measured by #30).
- **Nothing here is tuned.** The provisional settings are the owner's choice, and #103 replaces them.

## Revisit when

- **#103** tunes the memory, the pull and aging. A setting outside its grid needs a new ADR.
- **#102** replaces the prior mean of 0.
- **Or gate 2** shows the ratings not moving B3, or the arena term or the power-play model's terms behaving oddly. A change to the rows or terms then needs a new ADR.
