# 0001. Player chemistry and line interactions are out of scope for v1

- Status: Accepted
- Date: 2026-09-28

## Context

The player layer (B3) builds each team's expected goals from individual RAPM coefficients weighted by projected ice time (docs/plan.md section 5). A chemistry term would add effects for pairs or lines of players beyond the sum of their individual ratings.

The plan's principles argue against adding this now. Every rating is shrunk toward a prior. Simple models come first, and complexity must earn its place on held-out games. The samples are small: about 1,300 games a season, and section 9 lists tuning to the test data as a main risk. Interaction terms multiply the number of parameters far faster than the ice time available to estimate them, because most player combinations share few minutes. Section 1 lists player chemistry and line interaction terms as a v1 non-goal.

## Options

1. **Pairwise or line-level interaction terms in RAPM.** This captures chemistry if it exists. The cost is many weakly identified parameters, heavy shrinkage toward zero, and a large search space for tuning.
2. **Unit-level ratings for forward lines and defense pairs.** This is simpler than pairwise terms, but units change often, so ratings go stale and have to be projected onto lineups that never played together.
3. **Additive individual ratings only.** The B3 aggregation stays linear in players. Any chemistry effect stays in the residual.

## Decision

Option 3. B3 aggregates individual player ratings only. Chemistry and line interaction terms are out of scope for v1.

## Backtest evidence

None yet. This decision follows from the plan's principles and was made before any data was ingested. The first relevant evidence is the gate 2 comparison of B3 against B2.

## Consequences

- Lineup changes move a prediction only through the individual ratings and projected ice time of the players involved.
- A team's ability that goes beyond the sum of its players is left to the phase 6 team residual and coaching terms. Those are fitted only on what B3 leaves unexplained, which keeps the double-counting rules in section 5 intact.
- The leakage and aggregation tests only need to cover additive weights that sum to five skaters.

## Revisit when

B3 has passed gate 2 and its held-out residuals show consistent structure by line or pair combination. The idea then goes in as an `experiment` issue and is kept only if it beats B3 on held-out games.
