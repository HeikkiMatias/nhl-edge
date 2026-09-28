# NHL Edge Model

Codex applies the section below when reviewing. It mirrors the hard rules in CLAUDE.md.

## Review guidelines
- Flag any feature, join or rolling window that could use data not public at bet time
  (observed_utc must be earlier than the odds snapshot used for the bet).
- Flag any fitted component (xG, ratings, priors, shrinkage, blend) trained on data after its fold start.
- Flag any comparison of a regulation probability with a moneyline price.
- Flag odds used without de-vig through market/devig.py, and closing odds used in a tradable prediction.
- Flag bet selection or staking based on probability gap instead of expected return at the
  executable price.
- Flag backtests that use a game's own lineup or starting goalie instead of earlier boxscores.
- Flag metrics reported as pass or fail without intervals.
- Flag new features or model code without tests, any change to data/raw/ or tests/golden/,
  and calls to paid endpoints such as the Odds API historical API.
