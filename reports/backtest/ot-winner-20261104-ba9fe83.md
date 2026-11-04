# The overtime and shootout winner (ot-winner-20261104-ba9fe83)

#379 step 1, its rules fixed on the issue before the run. Ties after regulation by their
period 1 to 3 goals, the label the home team winning in overtime or the shootout, in the
3-on-3 era. Each scored season is fitted on the earlier ones. Log loss and Brier in
thousandths, the candidate's less the baseline's, tie by tie: negative favours the
candidate. Intervals are 95% weekly block bootstrap. Every season read has been seen,
so this is development evidence. SBR's opener is assumed public at 10:00 ET (ADR 0006).

**q3 decides nothing** (Codex on #390, hard rule 1): its input, Δĝ, has settings tuned
through 2018-04-09 on these training seasons' full-game results, OT and shootout
included (ADR 0011), after both of its folds' starts. Its figures are a description,
and the primary measure's Holm family is q1 and q2, the gate q2 alone. Under the
issue's first rule, with q3 in both, the outcome is the same.

Games where the regulation goals and `decided_in` disagree, dropped: 0.

| Season | Ties | Home won | q1 |
|---|---|---|---|
| 2016-17 | 289 | 0.543 | 0.490 |
| 2017-18 | 296 | 0.568 | 0.508 |
| 2018-19 | 271 | 0.469 | 0.523 |
| 2021-22 | 288 | 0.528 | 0.500 |

**The primary measure,** each candidate against q0 = 0.5 (Holm over q1 and q2):

| Candidate | Ties | Log loss | Brier | p | Holm threshold | Passes |
|---|---|---|---|---|---|---|
| q1 | 1144 | +0.88 [-0.73, +2.42] | +0.44 [-0.37, +1.21] | 0.268 | 0.025 | no |
| q2 | 1143 | -1.27 [-6.32, +3.47] | -0.63 [-3.14, +1.72] | 0.633 | 0.050 | no |
| q3 | 585 | -5.51 [-11.85, +0.60] | -2.76 [-5.90, +0.29] | 0.075 | described only | described only |

**The gate,** q2 against q1, and the share of priced games whose full-game probability
it would move by 1% or more from q1's; q3 beside it, described only:

| Candidate | Ties | Log loss | Brier | p | Shift share | Holm threshold | Passes |
|---|---|---|---|---|---|---|---|
| q2 | 1143 | -2.15 [-6.54, +1.95] | -1.07 [-3.25, +0.95] | 0.316 | 14.8% of 5,074 | 0.050 | no |
| q3 | 585 | -5.38 [-11.19, +0.07] | -2.69 [-5.58, +0.03] | 0.056 | 22.7% of 2,501 | described only | described only |

**q's calibration** (intercept 0 and slope 1 when calibrated):

- q2: intercept +0.10 [-0.03, +0.23], slope +0.65 [-0.42, +1.72]
- q3: intercept +0.24 [+0.04, +0.43], slope +1.52 [+0.48, +2.68]

**Step 2 does not go ahead:** the gate does not pass. q stays implicit in B3; a regulation or totals market would need the owner's scope first.
