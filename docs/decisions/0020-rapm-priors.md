# 0020. RAPM priors: traits fitted inside RAPM at each season's start, NHLe from the last two seasons

- Status: Proposed
- Date: 2026-10-02

## Context

RAPM (ADR 0019) pulls every rating toward 0, the average skater, until a player's own stints say otherwise. #102 (phase 3 task 8) replaces that 0 with a prior mean from what is known about the player before his stints: his age, draft slot and role, and for offense and the power play his minor-league or European scoring translated to the NHL (NHLe). It also gives the age curve that #103's aging will use. The phase 3 plan fits the priors once before each season, on players with a boxscore before it, so their effects are fixed for the season. The owner chose among the options below on 2026-10-02.

The counts behind it come from the training seasons 2011-12 to 2017-18, with RAPM's provisional settings. No fit was scored.
- **Who the prior is for.** Among the candidates in `lineups`, 13% to 21% have less than 5 hours of 5v5 data behind their rating, and 6% to 9% less than 2 hours.
- **What the traits say.** For players with 10 hours or more at a season's end, the 5v5 offense rating correlates −0.25 with log(draft pick): better picks rate higher. It correlates −0.12 with age.
- **NHLe coverage.** 884 of the 888 players who debuted from 2012-13 to 2017-18 had a non-NHL league season the year before, 533 of them in the AHL.
- **Career lines** come from `player_league_seasons` (#98, ADR 0016). No tournament line reaches 15 games: the World Championship has at most 10 to 12 and the World Juniors at most 14.

## Options

1. **How the trait effects are fitted.**
   - **Inside RAPM at each season's start** (chosen): before each season, RAPM is refit once with each player's traits as extra columns, so a trait's effect is learned from the stints themselves.
   - **From last season's ratings:** regress each player's season-end rating on his traits, weighted by ice time. That is simpler, but it understates the effects, because those ratings were already pulled toward 0, most of all for the low-ice-time players the prior is for.
2. **Which seasons feed a player's NHLe.**
   - **His non-NHL league seasons of the two seasons before, weighted by games** (chosen). That covers AHL call-ups as well as rookies.
   - **Only the season before his debut.** Fixed for his career, it never updates for a player who moves back and forth.
3. **How strongly defense and the penalty kill are pulled toward their role-level prior** (age, draft slot and role, without NHLe).
   - **The same pull as offense** (chosen). One pull for all four components, as #103 tunes it.
   - **Half the pull of offense.** A fixed ratio that #103 would not tune.

## Decision

**Traits,** per player and season, centred on a reference skater: a 27-year-old, drafted 60th, with no recent minor-league season. His prior mean is 0.
- **Age:** his age on October 1 of the season's first year, less 27, and its square.
- **Draft slot:** log(pick / 60) for a drafted player, and 0 with an undrafted flag for an undrafted one.
- **NHLe** (offense and the power play only): his NHLe points per game less 0.3, and a flag that he has one.
- **Role:** on the power play, through ADR 0019's defensemen term. At 5v5 and on the penalty kill a forward's and a defenseman's averages cannot be told apart (ADR 0019).

**NHLe factors,** per season:
- **A move** is a league season followed by the next NHL season, both of 15 games or more, regular season only.
- **A league's factor** is the NHL points per game over the league's, as a ratio of sums over the moves whose NHL season is one of the 10 seasons before.
- **Pooling:** a league with fewer than 30 moves joins one pooled factor.
- **Clean-up:** a season split across teams is summed, and a league listed under two abbreviations in one season keeps the abbreviation with more games.
- **A player's NHLe** is his non-NHL league seasons of the two seasons before, each of 15 games or more, translated by its league's factor and weighted by games.

**The season-start fit.** Before each season, RAPM's 5v5 and power-play models are solved once on the stints read so far, with trait columns added:
- the attacking skaters' traits summed for offense (and the power play);
- the defending skaters' traits summed and negated for defense (and the penalty kill);
- the players' own columns pulled toward 0, as before.

A trait's effect is fixed for the season. A player's prior mean for a component is his traits that season times the effects. The daily refits hold the trait columns at 0 and pull each player toward his prior mean, with ADR 0019's pull. A player without data sits at his prior. 2011-12 starts without effects, since nothing earlier has xG.

**The age curve,** per season and component:
- **The data:** each player's change in rating from his last game of one season to his last game of the next, for players with 10 hours or more behind both (2 hours on the power play and the penalty kill), using only pairs of seasons that ended before the season starts.
- **The fit:** that change regressed on his age at the second season (less 27) and its square, weighted by the harmonic mean of the two seasons' hours.
- **Its use:** #103 applies it with a tuned aging weight.

**Point in time.** A season's priors read only data public before its first as-of time: the lines (`player_league_seasons.observed_utc`, never before the player's first boxscore, ADR 0016), the stints, and the season-end ratings of earlier seasons. A future debutant never shapes them.

**What is stored:**
- **`player_ratings`** gets `prior`: the mean each rating is pulled toward, on the same footing as `mean`.
- **`rapm_terms`** carries the trait effects in force, as `prior:<component>:<trait>`.
- **The report** shows each training season's NHLe factors, trait effects and age curves.

## Backtest evidence

None yet. #103 tunes the pull, the memory and the aging weight with these priors in place (ADR 0011), and B3 (#106) and gate 2 (#107) score the ratings.

## Consequences

- **The traits' effects are learned from the stints,** not from ratings already pulled toward 0. Each comes with its season's data, so it can move from season to season. The report shows them.
- **The scoring credits** are those of the 2026 copies of the landing pages, corrections included, dated at the season's end (ADR 0016).
- **The age curve damps the true aging,** since the season-end ratings remember about a season of earlier play (ADR 0019's half-life). #103's aging weight can absorb that.
- **Defense and the penalty kill** get no scoring input, so their priors carry less information, with the same pull as offense.
- **Nothing here is tuned.** The thresholds (15 games, a 10-season window, 30 moves, two seasons of NHLe, 10 and 2 hours) are fixed here. The reference skater only sets where 0 is.

## Revisit when

- **#103** tunes the pull and the aging weight.
- **#117** refetches the landing pages and measures their corrections.
- **Or gate 2's development seasons** show the priors not helping new players. A change of traits or method then needs a new ADR.
