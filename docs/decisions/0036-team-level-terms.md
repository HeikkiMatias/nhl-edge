# 0036. Policy v2's B3: team strength, a new coach and penalty drawing beyond the power play

- Status: Proposed: none of the three terms passes its rule (backtest evidence below)
- Date: 2026-10-10
- Part of: policy v2 (#225), which the owner decided on 2026-10-10 (#222)

## Context

The owner asked whether B3, built from players, still accounts for what teams do as teams. The owner also asked about a pest who gets under opponents' skin (#224), and decided to bake such terms into v2 if they hold up (#222).

**What B3 has now** (ADR 0023): Δĝ from the projected lineups, h_s, the schedule terms and the empty-seat share. There is no team term.
- **A team's system** (its coach's structure and chemistry) reaches B3 only through its players' ratings.
- **Coaching changes** are in `coaches.csv` but feed no model.
- **B2's team strength ΔS** is computed every day, but serves only as B3's yardstick (hard rule 3). Gate 2 found B3 better than B2 (ADR 0024). Nobody has measured whether ΔS adds to B3.
- **A pest's penalties drawn** already feed the expected power plays (ADR 0021). Any effect beyond them is untested.

As in ADR 0035, the evidence can't come from the training seasons (#225), so each term is fixed here before it is scored.

## Options

1. **Three inputs, each its own switch, each scored alone against B3:**
   - **team:** ΔS, the home team's strength less the away team's, from B2's `team_strength` (ADR 0011) at the same as-of time.
   - **coach:** a new coach's first games, as home_new − away_new.
     - A team's new is 1 when its coach, as known at the as-of time (`reference.coaches_known_at`, from the morning after his first game), took over mid-season and has coached fewer than 20 of the team's games so far.
     - Mid-season means his first game isn't the team's first game of the season.
   - **pest:** home `drawn_index` − away `drawn_index` from `expected_power_plays` (ADR 0021): penalty drawing beyond what the expected power plays already carry.

   Each enters B3's regression as one more standardized input under B3's frozen L2 (ADR 0023). That is enough for a single continuous or common input, unlike ADR 0035's rare per-arena indicators. Nothing is tuned.
2. **A team's recent results against B3's expectation** (team form). It needs B3's own out-of-sample residuals within each season, which is a recursive fit. Not now; ΔS already weighs recent results by its half-life.
3. **No team term.**

## Decision

**Option 1, proposed before any score is read.** The three switches are `b3.Terms(team=True)`, `Terms(coach=True)` and `Terms(pest=True)`.

**The test and the rule for keeping each:**
- **The runs:** one per term, `nhl backtest --hockey-only --seasons 20182019,20212022,20232024,20242025 --b3-terms <term>`. These are the seasons and protocol of ADR 0035.
- **The measure:** B3 with the term minus B3, as paired log loss with the pooled weekly block bootstrap interval.
- **The rule:** a term goes into v2 only if its interval lies wholly below 0.
- **Also reported:** the result per season and each fold's weight on the term.
- **If more than one term passes:** they are scored together once more, and the combination enters only if it beats each of its parts alone (the interval of the difference lies wholly below 0). Otherwise only the best single term enters.
- **A caveat:** with ADR 0035's arena term, four terms are tested for v2, so a pass by chance is possible.

## Backtest evidence

**Three runs of `backtest-hockey-20261010-a6e67dd`,** one per term, each committed after this ADR (`reports/backtest/hockey-20261010-a6e67dd-210612.json`, `-210638.json` and `-210704.json`, logged in `runs.csv`). Each covers the same 5,207 games over 112 weeks.

| Term minus B3 | Pooled paired log loss [95% weekly block bootstrap] | 2018-19 | 2021-22 | 2023-24 | 2024-25 |
| --- | --- | --- | --- | --- | --- |
| team (ΔS) | **+0.00034 [−0.00138, +0.00198]** | −0.00041 | −0.00161 | +0.00231 | +0.00104 |
| coach | **−0.00006 [−0.00019, +0.00006]** | −0.00027 | +0.00010 | +0.00002 | −0.00010 |
| pest | **−0.00056 [−0.00127, +0.00014]** | −0.00044 | −0.00158 | −0.00027 | +0.00006 |

**No pooled interval lies wholly below 0, so under this ADR's rule none of the three terms enters policy v2.** The combination step doesn't arise.
- **The coach run was repeated** as `backtest-hockey-20261010-fbe351b` (`hockey-20261010-fbe351b-214050.json`). That run gates each coaching stint by its public time rather than by the calendar day (Codex on #227). Its figures and fold fits are the same to the last digit, since every game was predicted after its stints were public. The first run stays logged.
- **The per-season figures** are point estimates. The verdict rests on the pooled interval alone.
- **Each fold's weights** are point estimates with no interval, so no conclusion is drawn from them:
  - **team:** ΔS's weight is 0.135 to 0.152 per fold. Δĝ's weight beside it is 0.227 to 0.310, against 0.310 to 0.404 in B3 alone.
  - **coach:** −0.011 to +0.004 per fold.
  - **pest:** +0.025 in 2018-19's fold, then +0.050, +0.060 and +0.059.
- **The rule stands as written.** Pest's pooled interval is the nearest to passing, but it includes 0. The rule was fixed before these results were read, and changing it now that they are known would be tuning on seen seasons.

## Consequences

- **v1 is untouched.** `Terms()` adds no input, so a v1 fit keeps its inputs and its bundle record.
- **A fit names its inputs** (`B3Model.inputs`), so the backtest report shows each term's weight.
- **A run bundle refuses a fit with extra inputs.** Its replay rebuilds v1's inputs only. Since no term enters v2, no live bundle carries one. If a later version takes a term, #225 makes the replay rebuild it first.
- **`b3.Tables`** gains an optional `team_strength`. The coach input reads `coaches.csv`, so live v2 depends on the reference being updated at each coaching change (handover: reference upkeep).
- **Tests:** a leakage test per term in `tests/leakage/test_b3.py` (hard rule 1):
  - ΔS reads only rows known before the prediction, and with the team term a fold is refused when B2's team strength was retuned after its start;
  - a coach counts from the morning after his first game;
  - the penalty indices are the expected power plays' own, known before the prediction.

## Revisit when

- A term's interval doesn't lie below 0. Then it stays out of v2, and this ADR records why.
- Or the coaching reference falls behind live: a mid-season change missing from `coaches.csv` silently zeroes the coach term.
