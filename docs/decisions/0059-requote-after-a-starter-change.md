# 0059. Requote after a starter change: a measurement beside wait

- Status: Accepted (by the owner, 2026-11-08, #410): option 2 for now, and option 3 once a paid Odds API subscription is in place (#323), as `requote` version 2
- Date: 2026-11-08
- Part of: #410. From the owner on 2026-11-08: "Why do we not post new bids after the goalie starter has been changed? We could just calculate new probabilities and post those bids right?"

## Context

**The rule today.** The quoting simulator (`live/kalshi_quotes.py`, #358) and the demo quoter (ADR 0055) share one rule. When Daily Faceoff names another starter for either team, they pull the game's bids. The game then waits for the model's next publication:
- **the morning view:** about 07:20 ET;
- **the decision:** the day's last publication.

**Why it pulls.**
- **The fair value is a published prediction,** and that prediction mixes the possible starters:
  - B2 and B3 weigh every candidate goalie pair by its start probability;
  - u's goalie doubt counts how unsure the starter is.
- **A new starter makes the price wrong:** once another starter is named, the posted price is partly for the wrong goalie.
- **Real orders do the same:** ADR 0045 cancels a maker part on a starter change for the same reason.

**What it costs.**
- **Before the decision:** the decision re-predicts with the new starter, and the game comes back.
- **After the decision:** nothing publishes again, so the game sits out until its start (#385, the demo of 11-02).
- **How often:** at the model's 1¢ variant, the simulator pulled on a starter change 23 times in 60 games, from 10-31 to 11-07, against 9 pulls on a price move. Its counts don't say how many came after the decision.

**Recomputing is cheap.** Each publication's bundle (`live/bundle.py`) already holds what the model needs: its inputs at the input cutoff, the saved fits, and every candidate goalie with his effect. The frozen prediction path can give the game's value again with the named starter, from the bundle alone.

## Options

1. **Wait (today).** Keep the pull, and wait for the next publication.
   - Cost: a game whose starter changes after the decision isn't quoted again that day.
2. **Requote at the publication's own Pinnacle price.** Recompute the model's value with the named starters from the publication's bundle. Pinnacle's de-vigged pair stays the one the prediction used.
   - Cost: that price predates the news. Pinnacle and Kalshi move on a starter change within minutes, and the blend leans on the market. So the requote may under-react, and quote close to a market that has already moved.
3. **Requote at the newest fresh Pinnacle snapshot after the news.** As option 2, but with the newest stored snapshot taken after the news, fresh by ADR 0033's 5 minutes.
   - Cost: snapshots come only at the odds slots, a few a day, so most requotes would find none and hold. Live, it would need an on-demand Odds API call (1 credit each, as re-valuation's).
4. **Requote anchored to Kalshi's mid after the news.** Recompute as in option 2, then shrink the value toward Kalshi's mid, as the anchored fair value does.
   - Cost: it brings Kalshi's own price into the fair value. The simulator's anchored variant already measures that idea.

## Decision: option 2, measured beside option 1, as `requote` version 1 (the owner, 2026-11-08)

**Then option 3:** once a paid Odds API subscription is in place (#323), the requote takes the newest fresh snapshot after the news instead, as version 2 with its own first day. The owner's words: "proposed for now and switch to fresh when we have a paid subscription".

**The measurement is a record of its own** (`real/shadow/kalshi_quotes/requote/1`), the way #385's hold rules were measured:
- **It doesn't change** the simulator's record or rules, the hold rules, the demo quoter, or real orders.
- **Its variants:** the model's fair value at each half-spread, under two rules:
  - **wait:** the simulator's rule, so both are scored on the same games;
  - **requote:** described below.
- **When it starts:** the rule is fixed on the day this ADR is accepted, and scored from the next game day. Earlier days are replayed only as a check of the mechanics, never kept.

**The requote rule.** At a starter change at time t, the game's bids are pulled as today. The game's model value is then recomputed at the requote moment m, a minute after t (REQUOTE_AFTER: time to see the change and post):
- **The publication:** the newest one before m that predicted the game, the morning view's or the decision's, and its bundle.
- **The named starters:** each team's starter at m by ADR 0037's rule (`lineup/confirmed.py`), the one the decision already uses at its own cutoff:
  - the NHL's starter flag, else Daily Faceoff's Confirmed, else its Likely;
  - only polls observed before m count.
- **A goalie outside the candidates,** such as a call-up, is rated as ADR 0037 rates one: from his latest goalie effect before the publication's cutoff, or as a goalie with no earlier game.
- **The new value:**
  - the starters are put in place in the bundle's goalie rows (`confirmed.adjusted`);
  - the frozen prediction path gives B2, B3 and u's parts again;
  - the blend is formed at the publication's own Pinnacle pair (hard rule 3, unchanged).
- **Quoting resumes** at the first step after m, at the new value.
- **A later change requotes again,** and the source's next publication replaces the recomputed value as usual.

**Holds and guards.**
- **The guard:** the publication is first recomputed from its bundle with no new call. It must give the published value to 1e-9, or the change is held.
- **When it holds:** a change with no value holds the game as wait does, and the record counts it. That covers:
  - a change before the day's first publication;
  - a bundle that can't be read;
  - a guard that fails;
  - a call withdrawn: a team the publication named a starter for, and none at m by the rule. The bundle's rows hold the earlier starter in place, not the goalie-start model's mix to go back to (Codex on #411);
  - a change in the game's last minute before its stop.
- **Point in time:** only polls observed before m count (hard rule 1), with a test in `tests/leakage/`.

**The measures,** as the hold rules'. The primary measure is profit at settlement per game, requote less wait, at 1¢, on the same games. Its interval comes from 4 weeks (hard rule 7), with no verdict before then. Beside it, the record counts:
- starter changes, and of those, requoted and held, by reason;
- the minutes quoted;
- the fills after a requote, with their 5- and 30-minute markouts. Those markouts are where option 2's under-reaction would show.

## Backtest evidence

None yet. The record provides it from its first scored day, with an interval after 4 weeks. The evidence so far is counts only: 23 starter pulls in 60 games, against 9 on a price move.

## Consequences

- **The code:**
  - `live/requote.py`: the value from a publication's bundle with the starters named by m;
  - `live/kalshi_quotes.py`: the requote record and its two rules, beside the hold rules;
  - `nhl real quotes`: keeps the record nightly in R2, reading the day's bundles, and prints its counts only;
  - **the dashboard copy waits:** Supabase's `quote_days` accepts only the two existing records, so showing it on the Results tab needs a migration, a follow-up.
- **The tests:**
  - a synthetic bundle;
  - a leakage test that a starter is used only from its poll on;
  - the replay of a requoted day equalling its record.
- **No change** to the simulator's existing record, the demo quoter or real orders until the owner decides on the reading.

## Revisit when

- The record has 4 weeks: the owner then decides whether the simulator, the demo quoter (a new `demo_quotes` version) or real orders (ADR 0045's cancel) should requote.
- A paid Odds API subscription is in place (#323): option 3, as version 2.
- The held share is large: then find out why before reading the measure.
- The markouts after a requote are clearly negative: then consider option 3 or 4.
