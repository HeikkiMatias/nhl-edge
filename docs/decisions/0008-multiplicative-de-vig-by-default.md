# 0008. Multiplicative is the default de-vig method

- Status: Accepted (by the owner, 2026-09-30)
- Date: 2026-09-30

## Context

Hard rule 3: every probability taken from a price comes out of `market/devig.py`, which offers multiplicative, power and Shin. Until an ADR chose a default (#10), every caller had to name a method. B0 is the de-vigged market, B1 recalibrates B0 per fold, and every model is compared with B1. The same method will de-vig the live snapshots for E2 and the closing proxy for CLV in phase 5. Without a default, callers can drift apart. The audit, the suspect-opener list, E2's implausible bounds (ADR 0007) and B1 each name multiplicative by hand today.

## Options

1. **Multiplicative.** p_i = pi_i / P, closed form for any number of outcomes. Any favourite-longshot bias in the prices stays in B0, for B1 to fit.
2. **Power.** Takes more of the margin off the longshot. It needs a solver per market, and it builds a fixed shape of the bias into B0.
3. **Shin.** With two outcomes it takes the same margin off each side, and its z is solved per market. It also builds a fixed shape of the bias into B0.

## Decision

Multiplicative, for B0, for B1's input, for the audit and for the live pipeline. The owner chose it on 2026-09-30.

The three methods tie on the development seasons. The prices do carry a favourite-longshot bias: B1 fitted on multiplicative probabilities has a slope of 1.07 to 1.12, so favourites won more often than priced. Power and Shin remove part of that bias, and B1 fitted on their probabilities has a slope of 1.02 to 1.08. B1 then scores the same whichever method it recalibrates. With multiplicative, the whole correction happens in one place, B1, which is fitted per fold with a train_cutoff and reported with an interval. With power or Shin, part of it would be assumed in the de-vig step and the rest fitted. Multiplicative also has no solver that can fail, and it treats a three-way market like a two-way one.

## Backtest evidence

Run `backtest-20260930-fb17031` in `reports/backtest/runs.csv`, on 2018-19 and 2021-22, the first with the default in `devig.py` and with every figure the same as `backtest-20260930-a18ef13` before it, with 95% weekly block bootstrap intervals. B0's paired log-loss difference against multiplicative, pooled:

| B0 method | E1 (close) | E2 (opener) |
| --- | --- | --- |
| Power | −0.0001 [−0.0006, +0.0003] | −0.0002 [−0.0009, +0.0004] |
| Shin | −0.0001 [−0.0004, +0.0002] | −0.0002 [−0.0006, +0.0003] |

Every per-season interval includes 0 as well. The closest is power on E2 in 2021-22, at −0.0007 [−0.0016, +0.0003].

B1 was also refitted on each method's B0, on the same folds and bootstrap: a one-off check with `B1_METHOD` set to each method, not a logged run.

| B1's input | Slope, 2018-19 and 2021-22 folds (E1; E2) | B1 minus B1 on multiplicative, E1 | E2 |
| --- | --- | --- | --- |
| Multiplicative | 1.105, 1.075; 1.119, 1.090 | reference | reference |
| Power | 1.046, 1.022; 1.061, 1.030 | +0.00007 [−0.00008, +0.00022] | +0.00005 [−0.00010, +0.00021] |
| Shin | 1.065, 1.040; 1.080, 1.050 | +0.00004 [−0.00006, +0.00014] | +0.00002 [−0.00007, +0.00012] |

## Consequences

- `devig.py` gets `DEFAULT_METHOD`, and `fair_probabilities` uses it when no method is named. B1's input, the report's reference method and the audit read it. No figure changes, since each of them already used multiplicative.
- The backtest keeps scoring B0 under power and Shin, paired against the default, so every run carries the evidence that would reopen this decision.
- ADR 0007's bounds and #56's suspect-opener list stay on multiplicative, through the default.
- Phase 5 de-vigs the live E2 prices and the closing proxy for CLV multiplicatively. At the lower margins of sharp books, the methods differ even less.

## Revisit when

- A paired interval for power or Shin excludes 0, for B0 or for B1's input, on a development or validation season.
- Or the live books quote margins well above SBR's 2% to 4%, where the three methods split further apart.
