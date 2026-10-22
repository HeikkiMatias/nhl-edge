# 0044. Challenger `b3_split`: B3 with its expected-goal difference split into its four parts

- Status: Accepted (the owner, 2026-10-22)
- Date: 2026-10-22
- Part of: the review agenda (#250). The challenger is #259, scored under ADR 0038 against B3 itself.

## Context

B3 passes its hockey inputs through one expected-goal difference, Δĝ, with one coefficient (ADR 0023). Δĝ is the home team's raw xG times its finishing multiplier κ·φ times the opposing goalie's γ, less the away team's.

It splits exactly into four additive parts that now share that one coefficient, although their reliability differs:
- **5v5 play:** the 5v5 xG difference, from RAPM's best-estimated ratings.
- **Special teams:** the power-play plus shorthanded xG difference, from fewer minutes.
- **Finishing:** raw·(κ·φ − 1), heavily shrunk.
- **Goalie:** raw·κ·φ·(γ − 1), resting on few shots. It is the only part that changes with the goalie scenario.

The four sum to Δĝ, so B3 is the special case where all four get the same weight.

The live B3 is refitted each decision day on the same games, those before the season's fold start, so its weights are fixed for the season. A split B3 can be fitted the same way, from history alone. Unlike the blend challengers (ADRs 0041 to 0043), it needs no live games to train and predicts from its first day.

## Options

1. **B3 with Δĝ replaced by its four parts, each standardized, under B3's frozen L2, fitted once on the live B3's training games** (chosen). The cost is three more weights to estimate, which the frozen L2 restrains.
2. **Two parts (even strength and the rest).** It has fewer weights, but it keeps finishing and goaltending, the least reliable parts, tied together.
3. **A Poisson score model with explicit overtime and the shootout** (plan §5, "only if earned"). This is the larger form of the idea. It waits until this split has been read (#259).

## Decision

Option 1, as challenger `b3_split`, version 1, registered on the day its code merges.

- **The model:** B3 (ADR 0023) with Δĝ replaced by Δ5, ΔST, Δfin and Δgoalie.
  - Each part is standardized on the training games, under B3's frozen L2 (`b3.TUNED`).
  - h_s, the schedule terms and the empty-seat share are unchanged, and so is the mixing over goalie scenarios.
  - In training, Δgoalie reads the goalies who started, as B3's Δĝ does. In prediction, it reads each scenario's pair.
  - It is `b3.Terms(split=True)`. `Terms()` stays B3 as every policy decision reads it.
- **The fit:** fitted once, at registration, on exactly the live B3's training games for 2026-27: games from 2011-12 with results and boxscores public before the live fold start.
  - It is committed as `reports/live/b3-split-<yyyymmdd>-<shortsha>.json`, with its train_cutoff, and its sha256 is pinned in the registry. Any other file is refused.
  - It is not refitted during the season, as the live B3 is not.
- **Predictions:** for every slate game B3 predicts, from the day's run bundle alone (its B3 rows, with v2's starters in place, ADR 0037). It is never unfitted. Lake reads: none.
- **The stability check,** before registration, reported on #259: the split's fitted weights at the fold starts of 2018-19, 2021-22, 2023-24 and 2024-25.
  - Each fold is fitted on its earlier seasons only, and no test season is scored.
  - It is a diagnostic. It changes nothing fixed here.
- **Primary measure** (ADR 0038 rule 4): the paired log loss of `b3_split` minus B3 (the ledger's `p_b3`), on the games both predicted.
  - **Reported beside it:** what every challenger gets: the differences from the frozen blend and B1, the shadow bets' CLV and the disagreement's slope. They are expected to be poor for a hockey-only model, and they are not its question.
  - Each gets a weekly block bootstrap interval (hard rule 7), with counts only under 4 weeks, and no verdict before 2027-04-12.

## Backtest evidence

None, by design (ADR 0038): the split is fitted on history but scored on future games only. **For context only:**
- **B3 minus B2:** −0.0079 [−0.0130, −0.0028] on the development seasons, E1, and −0.0030 [−0.0094, +0.0034] on the one-time 2025-26 (ADR 0024, `backtest-20261003-b1a7b04` and `backtest-hockey-20261003-82fbada`).
- **B3 minus B1:** +0.0033 [−0.0013, +0.0077] on E1. B3 does not beat the recalibrated market.

## Consequences

- **Code:**
  - `game/b3.py`: the four parts, `Terms(split=True)`, and a scenario's goalie part beside its Δĝ. `Terms()` keeps every number of B3.
  - `live/challengers.py`: the registry entry, reading the committed fit.
  - a CLI step that writes the fit once.
- **Tests:**
  - the parts sum to Δĝ;
  - `Terms()` is untouched, and the bundle still replays its ledger;
  - a leakage test in `tests/leakage/` that the fit reads only games before the fold start, and that a prediction reads the bundle alone.
- **The live report:** a challenger's baseline may be a ledger model (B3, `p_b3`) as well as another challenger.
- **Policy:** none, as for every challenger.
- **Evidence accrues from the first day after registration,** at about 46 v2 games a week.

## Revisit when

- **The next season's live B3 is refitted:** the split is refitted with it, as a new version.
- **A mistaken specification:** that makes a new version with a new registration, never a change to this one.
