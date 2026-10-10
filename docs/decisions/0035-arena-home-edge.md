# 0035. Policy v2's B3: each arena's home edge beyond the league's

- Status: Proposed
- Date: 2026-10-10
- Part of: policy v2 (#225), which the owner decided on 2026-10-10 (#222)

## Context

The owner asked whether some arenas are harder for visitors, for reasons such as altitude, the crowd or the trip in (#223). The owner decided to bake an arena term into policy v2 if it holds up (#222).

**What B3 has now** (ADR 0023):
- **The home edge, h_s:** one league-wide edge per season, from its home win rate (ADR 0011).
- **The schedule terms:** rest, back-to-backs, kilometres travelled and time zones crossed. They count the same at every destination.
- **RAPM's arena term (ADR 0019):** it corrects arenas whose shot recording runs high or low for both teams. It is not a home edge.

So the model treats every arena's home edge as the same.

**The evidence can't come from the training seasons.** B3 can't predict them out of sample: its settings were tuned there, and the walk-forward refuses those folds (ADR 0011). The term is therefore fixed here before it is scored (#225).

## Options

1. **Each arena's edge, estimated within each fold and pulled toward zero by how much arenas truly differ.** The size of the pull is measured from the fold's own training games, so nothing is tuned. It adds one number per arena.
2. **One altitude input** (the arena's elevation). It covers Colorado but not crowds or trips. It's a single input, and its effect would rest mostly on one arena.
3. **One indicator per arena inside B3's regression,** under its frozen L2 (ADR 0023). The L2 acts on standardized inputs, and a rare indicator is barely held back: about 12 games' worth of pull against some 300 home games of data. Each arena would keep nearly its raw edge, which is noise more than signal.
4. **No arena term.**

## Decision

**Option 1, proposed before any score is read.** The exact form is fixed here.

**Estimation,** within each fold (one estimate per season, as B3's own fit):
- **The games:** the fold's B3 training games that are non-neutral and at least half full (`empty_seats` ≤ 0.5), so 2020-21's empty arenas don't dilute the crowd.
- **Per arena** a, where an arena is the `arena_id` of the venue (`venues.csv`):
  - g_a = Σ (home win − p);
  - h_a = Σ p(1 − p);
  - p is B3's fitted chance for each of those games.
- **How much arenas truly differ:** τ² = max(0, (Σ g_a²/h_a − k) / Σ h_a), where k is the number of arenas. This assumes the edges average zero around h_s and B3's intercept.
- **The shift:** shift_a = g_a·τ² / (τ²·h_a + 1). This is each arena's raw edge, g_a/h_a, pulled toward zero by τ²/(τ² + 1/h_a). An arena with few games, or a fold where arenas don't differ beyond chance, gets close to 0.

**Use:**
- The shift is added to the log-odds of every non-neutral game at least half full at arena a.
- It is added in each goalie scenario, before they are mixed.
- An arena without training games gets 0.

**Nothing is tuned:** τ² comes from each fold's own training games.

**The test and the rule for keeping it:**
- **The run:** `nhl backtest --hockey-only --seasons 20182019,20212022,20232024,20242025 --b3-terms arena`. These seasons are scored, never tuned on. 2019-20 and 2020-21 are left out for the bubble and empty arenas, and 2022-23 and 2025-26 are spent.
- **The measure:** B3+arena minus B3, as paired log loss with the pooled weekly block bootstrap interval.
- **The rule:** the term goes into v2 only if that interval lies wholly below 0.
- **Also reported:** the result per season, each fold's τ² and its shifts.
- **A caveat:** this is one of several terms tested for v2 (#224 adds others), so a pass by chance is possible.

## Backtest evidence

None yet. The run above provides it, and its result is added here before the owner decides on acceptance.

## Consequences

- **v1 is untouched.** `b3.Terms()` adds nothing, so every v1 decision, replay and backtest reads the B3 it read before.
- **A fit carries its arena shifts** (`B3Model.arenas`), so a v2 run bundle replays them.
- **`b3.game_inputs`** gains `arena_id`, which is null for neutral sites. The venue is the schedule's, public a day before the game (ADR 0005).
- **Tests:** a leakage test in `tests/leakage/test_b3.py` (hard rule 1): a fold's shifts read only training games whose results were public before the fold starts. Unit tests cover the shrinkage and the half-full rule.
- **Live v2** needs each slate game's venue, which the schedule row already has. #225 wires it.

## Revisit when

- B3+arena's interval doesn't lie below 0. Then the term stays out of v2, and this ADR records why.
- Or, in live use, a single arena's shift is driven by a recording change, such as a new rink or a new scorer. That would show up as a jump in RAPM's arena term.
