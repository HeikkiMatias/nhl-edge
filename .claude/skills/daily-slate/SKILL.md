---
name: daily-slate
description: Review today's paper decision after the 12:45 ET midday run - list each game's prediction or why it has none, the bets, and the flags and lineup gaps for manual review. Never changes the frozen policy.
disable-model-invocation: true
argument-hint: "[date, default today]"
---
The day's decision is made once, by the midday dispatch (`odds-snapshots.yml`'s Predict step, 12:45 to 13:15 ET), and written once to R2 (ADR 0033). This review reads it and changes nothing: no prediction, stake or policy setting is ever edited after the fact, and a flag is never a reason to tune the model (hard rule 8).

1. Run `uv run nhl status --brief`. Check that the nightly built today's live features (`feature_builds` for the date) and that the midday odds snapshot landed.
2. Run `uv run nhl live slate --date $ARGUMENTS --r2` (default: today, US Eastern game date). If it says there is no ledger:
   - before 13:15 ET, the decision hasn't run yet;
   - after 13:15 ET, read the Predict step's log. A day without a midday snapshot in the window is skipped, never reconstructed.

   To look at a day before its real run, `uv run nhl predict --dry-run --date <d> --at <instant>`, then `uv run nhl live slate --from data/live/dry/<d>.parquet`. A dry run is never the record.
3. Report the slate table: each game's status, B1, B3, the blend, the gap, u in training standard deviations, and the bet (side, Pinnacle's price, EV at that price, stake). The ledger also holds the best EU book's price and its `last_update` beside Pinnacle's.
4. Report every flag the command lists:
   - a gap above 8 points between the blend and B1: review by hand;
   - a pick the market move guard stopped;
   - a game not predicted, and why (no midday price, a stale price, a missing input, started before the decision or its publication);
   - u more than 2 training standard deviations out (ADR 0030's revisit check).
5. Report the attribution the command prints after the flags (#193): each bet's driver and its parts in log-odds toward its side. The market's part is the blend's reshaping of Pinnacle's fair price; each input's part (skaters, goalies, home ice, schedule) is its departure from its usual level at that price. An edge explained by one odd input is a suspected bug: open an issue, never edit the model. For each gap, also look at B3 against B2 (the player layer), the goalie doubt and the starter the goalie-start model expects, the availability doubt and the rookie share.
6. Report the lineup gaps: the replacement skaters each team's lineup is short of. Also report any team whose goalie doubt is high, where no goalie is a likely starter.
7. Missing feeds, skipped days and stale prices are operational alerts: open an issue if one repeats. The weekly live report (`uv run nhl live report --r2`) gathers them with the evidence (ADR 0032).
