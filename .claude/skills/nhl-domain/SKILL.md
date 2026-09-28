---
name: nhl-domain
description: NHL betting domain reference - moneyline settlement including OT and shootout, de-vig
  methods (multiplicative, power, Shin), expected return, CLV, empty-net behavior and known pitfalls.
  Load before writing or reviewing any code that touches odds, prices, probabilities, game outcomes,
  settlement, bets or CLV.
---
# NHL betting domain reference

Skeleton. Extend it as phase 1 audits the data, and record every choice made here in an ADR.

## Settlement

- **Moneyline (2-way)** settles on the full game, including overtime and the shootout. Always compare it
  with a full-game win probability. Never compare a regulation (60-minute) probability with a moneyline
  price. This was a bug in the previous app.
- **3-way / regulation line** settles on 60 minutes only, and a tie is its own outcome. It is not a
  moneyline.
- **Puck line and totals** usually count OT and the shootout, and the shootout winner is credited with one
  goal. Check each book's rules before these are used in v2. They are logged in v1 but never bet.
- `games.decided_in` is REG, OT or SO. The golden games cover one of each: regulation win, OT win,
  shootout, a late empty-net goal and a 5-on-3.

## Game structure

- Regular-season OT since 2015-16: 5 minutes of 3-on-3 sudden death, then a shootout. From 2005-06 to
  2014-15 it was 4-on-4. A phase 6 score model fits its OT and shootout part on 2015-16 onward only.
- Playoffs have 20-minute 5-on-5 OT periods and no shootout. They are out of scope for v1.
- Special seasons: 2012-13 lockout (48 games), 2019-20 paused with bubble playoffs, 2020-21 (56 games,
  realigned divisions, mostly no fans). The season home term h_s absorbs these eras.
- Team code history: PHX became ARI in 2014-15, ATL moved to WPG in 2011-12, VGK joined in 2017-18,
  SEA in 2021-22, and ARI became UTA in 2024-25. Map through the static team history file, never by name.

## Empty nets

- Trailing teams pull the goalie late, usually when down one or two goals. Empty-net goals inflate totals
  and final margins, and they turn many one-goal leads into puck-line covers.
- Goalie ratings exclude empty-net shots and goals (`shots.is_empty_net`).
- The score model's base rates exclude empty-net goals, which are modeled separately.

## Odds and de-vig

Decimal odds o_i, implied probabilities pi_i = 1 / o_i, overround P = sum of pi_i (above 1 means vig).
American odds: o = 1 + A/100 for A > 0, and o = 1 + 100/|A| for A < 0.

| Method | Fair probability | Note |
| --- | --- | --- |
| Multiplicative | p_i = pi_i / P | Spreads the margin in proportion to the price |
| Power | p_i = pi_i^k, with k solving sum of pi_i^k = 1 | Puts more of the margin on the longshot |
| Shin | p_i = (sqrt(z^2 + 4(1 - z) pi_i^2 / P) - z) / (2(1 - z)), with z solving sum of p_i = 1 | z models insider trading; corrects favorite-longshot bias |

- De-vig only through `src/nhl_edge/market/devig.py`. Which method is default is decided in phase 1, by
  audit and ADR. Tests check that probabilities sum to 1 and that symmetric prices give 0.5.
- B0 is the raw de-vigged market. B1 is a logistic regression on the market log-odds, fitted
  chronologically. A model that beats B0 but not B1 is only correcting market calibration.

## Expected return, staking and CLV

- Select on expected return at the executable decimal price: EV = p_blend * o - 1. Never select on the
  probability gap. A 52.5% blend against a 50% fair price at 1.90 has EV = -0.25%.
- Stake: f = (1/4) * EV / (o - 1), capped at 1.5% of bankroll per bet and 5% per day, with at most one bet
  per game and at least 2.5% EV (more when the uncertainty score is high).
- CLV = o_taken * p_close_fair - 1, where p_close_fair is the de-vigged Pinnacle closing proxy.
- The closing proxy is the last pre-game snapshot whose Odds API `last_update` is fresh. It is never the
  true close. Exclude stale or suspended quotes.

## Known pitfalls

- Regulation probability against a moneyline price (see Settlement).
- Using implied probabilities without removing the vig, or de-vigging outside devig.py.
- Closing odds used as the market input of a tradable (E2) prediction. E2 uses the price available at the
  prediction time: the SBR opening line historically, live snapshots from October 2026.
- The SBR archive does not name its book and ends in 2022-23. Its quality is unknown until the phase 1
  audit.
- Game dates are in US Eastern time, but everything is stored in UTC. Late West Coast games fall on the
  next UTC date.
- Sharp line moves between the morning and pre-game snapshots usually mean goalie or injury news the
  model lacks. Skip bets that side against them.
