# Targeted features on the yardstick (targeted-features-20261104-ee21ec8)

#370, on the explore half (2013-14 and 2014-15, fitted from 2012-13), each candidate
fixed on the issue before it was scored. Move skill in thousandths: negative means the
candidate anticipates SBR's move beyond the opener's drift. Intervals are 95% weekly
block bootstrap. The features' settings were tuned on these seasons (ADR 0011).

Not fresh on these games: #361's diagnostics already read the early-season subset's move
skill and, descriptively, where the goalie starter surprised (reports/yardstick/
yardstick-diagnostics-20261101-c7bfa52.md), so early's p-value is development evidence
twice over. Only travel and density must hold on the rested games by the rule; the
others' rested intervals are given beside, since a start probability can read last
night's game, which an opener of the evening before may not have known (ADR 0006).

| Candidate | Games | Move skill | On the rested games | Result beyond the close |
|---|---|---|---|---|
| travel | 2,445 | +0.005 [-0.033, +0.044] | +0.034 [-0.013, +0.086] | +0.979 [-0.132, +2.073] |
| density | 2,445 | -1.343 [-1.696, -1.012] | -0.373 [-0.536, -0.211] | +5.712 [+3.255, +8.188] |
| B3 split | 2,445 | -0.464 [-0.842, -0.134] | -0.554 [-1.061, -0.121] | +3.323 [-0.126, +6.690] |
| early | 2,445 | -0.041 [-0.482, +0.385] | -0.214 [-0.785, +0.279] | +3.301 [-0.095, +6.649] |
| goalie_presumed | 2,445 | +0.043 [-0.013, +0.103] | +0.064 [+0.013, +0.117] | -0.189 [-0.730, +0.350] |
| B3 split + goalie_presumed | 2,445 | -0.460 [-0.838, -0.133] | -0.548 [-1.054, -0.115] | +3.117 [-0.572, +6.697] |
| B3 split + density | 2,445 | -2.019 [-2.541, -1.535] | -0.948 [-1.587, -0.397] | +9.321 [+4.643, +13.839] |

**The family's decision** (Holm's step-down at 0.05 over five, lineup_change at p = 1
until scored; early and goalie_presumed are paired against B3's split):

| Candidate | Primary estimate | On the rested games | p | Holm threshold | Kept |
|---|---|---|---|---|---|
| travel | +0.005 [-0.033, +0.044] | +0.034 [-0.013, +0.086] | 0.7880 | 0.0250 | no |
| density | -1.343 [-1.696, -1.012] | -0.373 [-0.536, -0.211] | 0.0000 | 0.0100 | yes |
| early | +0.423 [+0.284, +0.566] | +0.340 [+0.184, +0.488] | 0.0000 | 0.0125 | no |
| goalie_presumed | +0.004 [-0.002, +0.009] | +0.006 [+0.002, +0.011] | 0.1940 | 0.0167 | no |
| lineup_change | not yet scored | | | | |

**Kept:** density (rested interval below 0): proposed to the owner as challenger inputs, as development evidence only.

**Descriptive, after the decision and not part of it:** a kept candidate on top of B3's
split, which already carries the goalie-start mixture, paired against the split (move
skill and result beyond the close, thousandths):

| Comparison | All games | Rested | Back-to-back | Result beyond the close |
|---|---|---|---|---|
| density beyond B3's split | -1.556 [-1.899, -1.219] | -0.394 [-0.599, -0.195] | -4.295 [-5.303, -3.346] | +5.999 [+3.404, +8.713] |
