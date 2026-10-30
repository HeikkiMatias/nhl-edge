# 0052. The morning view: the model at 07:05 ET, scored on where the market goes

- Status: Accepted (by the owner, 2026-10-30, #357): option 1, with B3's slope as the primary measure, a family of its own, the day's feature rows counted as observed when their build had them, and the ruling below on the live games that prompted it
- Date: 2026-10-30
- Part of: #357, from the owner's decision of 2026-10-30 to post earlier, from the model's own view; it feeds #358

## Context

**The policy decides at 12:45 ET, and live, its view says nothing about the market's next move.**
- **The slope:** the move from the decision to Pinnacle's close, regressed on the blend's disagreement, has a slope of −0.02 [−0.10, +0.05] over 127 games.
- **The direction:** Pinnacle moved toward the blend in 48% of them.

**On history, from SBR's opener the bets anticipated the market:** their fair price moved toward them by +2.83% [+2.04%, +3.62%] (ADR 0039). SBR gives the opener no time, so it is assumed at 10:00 ET (ADR 0006). If the model's information is priced in by midday, an earlier view may still carry it. That is what posting early, and #358's quotes, would earn from.

**No morning model view is stored.** The ledger's `p_morning` is Pinnacle's 07:05 price, not the model's.

**It can't be rebuilt afterwards:** under v2, the decision's starters replace the slate's goalie rows in its run bundle (ADR 0037), and the nightly rewrites the lake's copies.

**Where the idea came from, and why that doesn't touch its evidence:** these figures are the live games' to 2026-10-29, from the owner's performance review. So are B1's home lean below and the choice of B3 for the measure. Under ADR 0038's rules none of those games counts. The evidence is only the games after registration, against a measure fixed here, and no model, fit or frozen policy changes.

**The owner's ruling (2026-10-30):** a pre-registered experiment judged only on games after its registration may take its idea, and the choice of its measure, from earlier live games. The leakage review's rule against designs informed by live games doesn't apply to it.

## Options

1. **A morning run at the 07:05 slot.** The decision's models are computed at the morning snapshot, then written once with their own run bundle, and no bet is made. It costs a second model run each game day, of about 8 minutes.
2. **A challenger from the midday bundle (ADR 0038).** This is cheap, but the bundle no longer holds the morning's goalie rows, so the result would not be the 07:05 view.
3. **Wait for #361's history harness.** It is free, but SBR's opener has no time and no goalie state, and it never gives #358 a fair value at 07:05.

## Decision: option 1 (the owner, 2026-10-30), as `morning_view` version 1, registered on the day its code merges

- **What runs:** at the morning slot, the decision's own code, with no selection and no bet:
  - B2, B3 and u's parts from the season's fits;
  - the frozen live blend and its twins;
  - B0 from Pinnacle's morning pair, de-vigged with `market/devig.py`.

  The live fit and the day's feature build are the decision's. Starters come from v2's rule (ADR 0037), applied to the polls observed before the cutoff. At 07:05 that is mostly the goalie-start mixture.
- **The cutoff (hard rule 1):** the morning snapshot, which is taken after the same dispatch's goalie poll. Every input precedes it, and so does the feature build's finish.
- **The day's feature rows count as observed when their build had them:** at the later of the build's finish and the night's results turning public, 10:00 UTC (06:00 EDT, 05:00 EST).
  - **Why:** their `observed_utc` is history's as-of time of 10:00 ET, the earlier of 10:00 ET and an hour before the start (`live/targets.py`). That says what the rows rate, not when they were known, and it falls after 07:05.
  - **Why that is safe:** the nightly build finishes by about 05:30 ET and reads nothing after it. Every result it rates from is public by then. The midday decision reads the same rows.
  - **Scope:** only the rows that build wrote for the day, by table and version, are restamped, and only for this run.
- **The window:** the snapshot and the run both fall between 07:00 and 07:35 ET. Outside it, the day is written as skipped and never reconstructed.
- **Per game:** a game that starts before publication, or that has no fresh Pinnacle pair, has no prediction. A fresh pair is at most 5 minutes old, as at the decision.
- **Storage:** each day is written once, under `morning/live/<date>.parquet`, with its bundle under `bundles/morning/<date>/`. The lake's `morning_predictions` table is rebuilt from those files. A nightly check replays each stored day from its bundle.
- **Primary measure:** the slope, with an intercept, of the move on B3's disagreement.
  - The move is m = logit p_close − logit p_morning, in the home side's log-odds.
  - B3's disagreement is d_B3 = logit p_B3 − logit p_morning.
  - It is taken over the predicted games whose closing proxy came after the morning snapshot. Its interval comes from the weekly block bootstrap (`metrics.slope`), and its p-value from the same draws.
  - **The reading:** a slope above 0 means the market later prices in what B3 sees at 07:05. B3 is the measure because it is market-free, and its slope is the weight a fair value should give it. The blend's disagreement also carries B1's recalibration, which leans −0.70 points from home on the market alone (the market-only blend against Pinnacle, on the same live games), and nothing of the morning.
- **Reported beside it:**
  - the same slope on the blend's disagreement, and B3's by u;
  - the share of games that move toward each;
  - the move split at the decision: 07:05 to 12:45, and 12:45 to the close;
  - the paired log loss of p_B3 and p_blend at 07:05, against p_b0 at 07:05 and against the decision's blend;
  - shadow bets at 07:05: the frozen selection and staking on the morning blend, at Pinnacle's morning price and without the guard, since there is no earlier pair. They are valued at the closing proxy (CLV).
- **Multiplicity:** a family of its own, one test at 5%, outside ADR 0048's Holm family.
  - It is no candidate for the decision's policy.
  - A policy that acts at 07:05 needs its own ADR, freeze and evidence.
  - The alternative is to count it in Holm's family, making m 6.
- **The evidence:**
  - It starts the first game day after registration and is never backfilled.
  - A day without its file is reported as missing, not withheld. The run ends before any move or result exists, so a failure can't choose its sample.
  - There are counts only under 4 weeks, and no verdict before 2027-04-12.

## Backtest evidence

None, by design: history has neither a morning snapshot with a time nor the goalie state at 07:05 (#323). The evidence is the stored mornings from registration on.

**A check of the mechanism, not evidence:** a dry run on 2026-10-29's real inputs, run as of 07:08 ET, predicted all 11 games.
- Its B0 equals the ledger's `p_morning`, game by game.
- Its bundle replays exactly.
- Its B3 differs from the decision's by 0.6 points on average, mostly through the starters called after 07:05.

## Consequences

- **The code:**
  - `live/morning.py`;
  - an optional root on `live/bundle.py`'s writers and reader;
  - the `morning_predictions` table and its schema;
  - `nhl live morning`, with the model loading shared with `nhl predict`;
  - a step in each of `odds-snapshots.yml` and `ingest-nightly.yml`;
  - the report's "Morning view" section;
  - leakage tests for every input, the window, and the command's cutoff;
  - a replay that refuses a bundle whose saved cutoff isn't its quotes' morning snapshot.
- **The frozen decision path doesn't change:** `predict.decide` is untouched, and the midday bundle keeps its bytes.
- **The morning job runs longer,** by about 8 minutes, and its rows are ready at about 07:15 ET.

## Revisit when

- #358's simulator needs a fair value, or quotes go to Kalshi's demo exchange.
- Near-start snapshots are added (#254), which changes what the close is.
- A policy v3 is frozen.
- The review on 2027-04-12.
