# lineup_change on the yardstick: lineup-change-20261104-6a61e1f

#370's fifth candidate, by the rule fixed on the issue before it was scored. The
explore half (2013-14 and 2014-15), each season fitted on the earlier ones from 2012-13
(ADR 0053), at E2's prediction time. Move skill is in thousandths of squared log-odds
against the opener's drift: negative means it anticipates the move. Intervals are 95%
weekly block bootstrap. The features' settings were tuned on these seasons (ADR 0011):
development evidence only.

| Measure | Games | Value |
| --- | --- | --- |
| Move skill (primary) | 2,445 | -0.104 [-0.227, +0.015] |
| The same, rested games | 1,717 | -0.134 [-0.274, +0.008] |
| Result beyond the close | 2,445 | +0.453 [-0.494, +1.449] |
| Slope of the move on it | 2,445 | -0.484 [-0.635, -0.329] |

**Holm's step-down over the family of five** (the other four at #389's p-values):

| Candidate | p | Threshold | Passes |
| --- | --- | --- | --- |
| density | 0.000 | 0.0100 | yes |
| early | 0.000 | 0.0125 | yes |
| lineup_change | 0.083 | 0.0167 | no |
| goalie_presumed | 0.194 | 0.0250 | no |
| travel | 0.788 | 0.0500 | no |

**lineup_change is not kept:** it doesn't pass the step-down with a negative estimate. The four earlier decisions stand: density is kept, travel, early and goalie_presumed are not.

**Descriptive, never part of the decision:** B3's split plus lineup_change less the
split, +0.071 [-0.040, +0.183] thousandths.
