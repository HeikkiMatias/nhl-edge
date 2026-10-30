# 0053. The market-relative yardstick: features scored on the move to the close, on the training seasons

- Status: Accepted (by the owner, 2026-10-30, #361): option 1, with the move skill as the primary measure, the explore and confirm halves, the rested games, the training seasons' exception and the finishing part's first season
- Date: 2026-10-30
- Part of: #361, from the owner's ask of 2026-10-30, "we need to find a way to develop our model to be better if we're losing to the market"; it extends ADR 0011

## Context

**The yardstick used so far can't guide that work.**
- **Too noisy:** ADR 0011 scores a change by the log loss of later games' results. That target is so noisy that ADRs 0050 and 0051 could only show differences within noise.
- **The wrong question:** a better model of the outcome needn't beat the market. Gate 3 (ADR 0031) and the live record say ours doesn't.

**History already hints at where the model helps.** On 2021-22 and 2022-23, the policy's E2 bets anticipated the market, with a fair move of +2.83% [+2.04%, +3.62%] per bet. Yet they lost to the close, at a CLV of −1.23% [−2.01%, −0.46%] (ADR 0031).
- So the model may know at the open some of what the market learns by the close.
- The move from the opener to the close is a target with far less noise than the result: the market's own later estimate.
- It is also what posting early pays on (#357, #358).

**No choice here rests on live or 2025-26 figures.** The live record is cited only as the verdict that the model doesn't beat the market.

**The constraints:**
- **Seasons:** ADR 0011 keeps the development and held-out seasons unread while developing, so the work stays on the training seasons.
- **B2's and B3's own fits** refuse every fold there, since their settings were tuned on those seasons' results (the tuning cutoff, 2018-04-09).
- **SBR's opener has no time.** E2 assumes 10:00 ET (ADR 0006). An opener posted the evening before would predate the previous night's results, which the 10:00 ET features already know.

## Options

1. **A market-relative yardstick on the training seasons** that scores per-game features on the move to the close and on the result beyond it, as ADR 0011 scores a component. It is fast and has power. The features' settings are in-sample, and its confirmation is live.
2. **Results only, as now (ADR 0011).** Nothing new to build, but it can't tell small changes apart, and it never asks the market's question.
3. **Live evidence only:** the morning view (ADR 0052) and the challengers (ADR 0038). Clean, but months pass before a reading, and one test per idea.

## Decision: option 1, with the owner's choices of 2026-10-30

**The code** is `backtest/yardstick.py` and `nhl research yardstick`.

**What is scored:** point-in-time per-game features. A candidate is one feature or a set. They are taken as B3 sees them at E2's prediction time, a second after the opener's assumed 10:00 ET:
- **The inputs:** `b2.game_inputs` and `b3.game_inputs`, with `b3.split_inputs` for Δĝ's parts (ADR 0044).
- **The goalie terms:** averaged over the start scenarios known then (ADR 0012), never the starters.
- **The candidates:** B2's ΔS, ΔG and h_s; B3's Δĝ and its 5v5, special-teams, finishing and goalie parts; B2's inputs together, B3's split together, and all of them.

**The training seasons' exception (ADR 0011):** each tuned table's row (team strength, schedule terms, goalie effects, the ratings, RAPM's terms, the power plays and the multipliers) is read at the time its data was known (`untuned`), not from the tuning cutoff its frozen settings carry.
- This is the in-sample-by-design reading that B2's own tuning makes (`b2.tuning_scores`), and no hard rule changes.
- The code refuses any row whose observed time differs from its data's time by anything but that cutoff. On the lake, every row of the seven tables passes.

**The two targets,** in the home side's log-odds, from SBR's opener o and close c, de-vigged multiplicatively (hard rule 3), with the openers E2 refuses dropped (ADR 0007). The close is a target only, never an input.
1. **The move,** y = c − o.
   - **The forecast:** ŷ = a + b_o·o + b·x, by least squares. **The baseline:** ŷ₀ = a + b_o·o, the opener's own drift.
   - **The primary measure** (the owner's choice) is the move skill per game, s = (y − ŷ)² − (y − ŷ₀)². Negative means the feature anticipates the move.
   - **Beside it,** for a single feature: the slope of y on x.
2. **The result beyond the close.** The log loss of a logistic model of the home win on the full game (hard rule 2), on c and x, less that of c alone. This is gate 3's question.

**The fits** follow ADR 0011: each scored season is fitted on the earlier seasons' games whose results were public before its fold start. As in the backtest's E2, that is its first start, or its first opener prediction when that comes earlier.
- **The checks:** `through` refuses any row fitted after the tuning cutoff, and any row of an untimed table observed before its fit.
- **The first season fitted on:** 2011-12, B3's first season with features. It is never scored.
- **A later first season:** ADR 0011 scores a season only once an earlier one has the component's inputs. Every 2011-12 shooter finishes at φ = 1 (ADR 0022), so a candidate with the finishing part is fitted from 2012-13 and scored from 2013-14.

**The halves** (the owner's choice): explore on 2012-13 to 2014-15, confirm on 2015-16 to 2017-18.
- **Explore** is for the diagnostics and for choosing features. A run reads no season after its half's last.
- **Confirm:** a claim from the explore half is written to `reports/yardstick/claims/<claim>.md` and committed. It is then scored once on the confirm half.
  - The confirm half is fresh for the yardstick's claims, but not for the features' settings, which were tuned on 2011-12 to 2017-18.
  - The command refuses a confirm run without its committed claim, or without `--r2`.
  - It keeps the run under the claim's name in R2, and a second run of that claim is refused.

**The stale opener** (the owner's choice): every figure is also given on the rested games, where neither team played the day before.
- On those games, neither team's own last game falls between an opener of the evening before and 10:00 ET.
- League-wide refits (RAPM's joint fit, the league's rates) still take in the night's other games, so the rested games narrow the gap without closing it.

**Reading the figures:**
- each is a pooled mean with its weekly block bootstrap interval (hard rule 7);
- two candidates compare through their paired per-game difference;
- nothing is read as a pass or fail;
- every run keeps its per-game rows in `bundles/yardstick/`, each with its season fit's train_cutoff, with the tuning cutoff in its manifest and its report in `reports/yardstick/`.

## Backtest evidence

**The explore run** is `yardstick-20261030-dafedeb` (`reports/yardstick/explore-yardstick-20261030-dafedeb.md`), on 3,165 games of 2012-13 to 2014-15 (2,445 from 2013-14 for the finishing part).
- **Units:** the move skill is in thousandths of squared log-odds. The baseline's mean squared error is 11.903 thousandths.
- **Status:** it is the yardstick's baseline reading. It changes no model.

| Candidate | Move skill | Rested games | Beyond the close |
| --- | --- | --- | --- |
| ΔS, team strength | −0.203 [−0.348, −0.072] | −0.331 [−0.522, −0.167] | −0.307 [−1.598, +0.999] |
| 5v5 xG difference | −0.257 [−0.516, −0.017] | −0.432 [−0.754, −0.135] | −0.491 [−2.317, +1.232] |
| Special teams | −0.058 [−0.130, +0.013] | −0.094 [−0.166, −0.025] | +0.723 [−0.193, +1.726] |
| ΔG, B2's goalie effect | −0.029 [−0.102, +0.048] | +0.059 [−0.039, +0.156] | +0.612 [−1.044, +2.219] |
| Δĝ, B3's whole difference | −0.072 [−0.328, +0.176] | −0.121 [−0.411, +0.146] | +0.961 [+0.031, +1.931] |
| B2's inputs together | −0.138 [−0.359, +0.061] | −0.110 [−0.365, +0.128] | +0.235 [−1.932, +2.321] |
| B3's split together | −0.464 [−0.842, −0.134] | −0.554 [−1.061, −0.121] | +3.323 [−0.126, +6.690] |

**What it says, as context for the diagnostics, not a verdict:**
- **The features anticipate a little of the move.** B3's split forecasts about 4% of the move beyond the opener's own drift (0.46 of 11.9). Team strength and the 5v5 part each do so on their own.
  - It holds on the rested games, so last night's results aren't what drives it.
  - The market moves toward every feature: the slope of the move on the 5v5 part is +0.158 [+0.131, +0.188].
- **None adds to the close.** No candidate's result interval lies below zero. Δĝ's fit costs +0.96 [+0.03, +1.93]. This is gate 3's finding again, on the training seasons.
- **Δĝ as one number does worse than its parts.** Its move skill holds zero, while the split's doesn't. That points step 2 at the parts.

## Consequences

- **The code:**
  - `backtest/yardstick.py`;
  - `nhl research yardstick --half explore|confirm [--candidates] [--claim] [--r2]`;
  - `tests/unit/test_yardstick.py`;
  - `tests/leakage/test_yardstick.py`: the close never enters the forecast, the fits read only results public before the season, an explore run never reads the confirm half, the features are as known at the opener's time, and only the tuning cutoff is set aside.
- **Step 2, the diagnostics,** run on the explore half. Each finding the work keeps becomes a claim, scored once on the confirm half.
- **A new feature** (step 4) enters as a column of `features` and a candidate. It is scored here before it enters a challenger, which then decides live (ADR 0038).
- **Step 3, a close forecaster,** can take these features as its inputs. It gets its own ADR.
- **Caveats each report names:**
  - the features' settings are in-sample;
  - SBR's opener is assumed at 10:00 ET (ADR 0006);
  - SBR's quality is SBR's (#56).

## Revisit when

- The confirm half is spent: every training season has then been read, and only live evidence remains.
- A source dates SBR's openers (ADR 0006), which could change the moves.
- The morning view's live move (ADR 0052) disagrees with what the yardstick found.
