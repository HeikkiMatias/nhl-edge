# 0037. Policy v2: confirmed starting goalies in live B3 and in u

- Status: Accepted (by the owner, 2026-10-11)
- Date: 2026-10-11
- Part of: policy v2 (#225), which replaces v1 from its freeze date (the owner, 2026-10-10, #222 option 1)

## Context

**The owner's principle (2026-10-10):** the live model should use all the information available at the decision, even information history doesn't have. Confirmed starting goalies are the first case (#222).

**Policy v1 never reads a confirmation** (ADR 0030):
- Live B3 mixes over the goalie-start model's likely starters (ADR 0012).
- u's goalie doubt is that model's 1 − p_start of the likeliest starter.
- Both sources are polled hourly and stored but unused:
  - the NHL's starter flag (`pregame_goalies`, #42);
  - Daily Faceoff's statuses (`dailyfaceoff_goalies`, #48).
- **Why it matters:** by 12:45 ET Pinnacle's price reflects most confirmed starters. A B3 that ignores them disagrees with the market whenever the confirmed goalie isn't its likeliest one, and the blend reads that as an edge.

**What else v2 takes:** none of the B3 terms tested for v2 passed its rule (ADRs 0035 and 0036). So v2 is v1 plus confirmed goalies, and v2's B3 on history is v1's.

**What the sources show so far** (lake to 2026-10-06):
- **The NHL:** its starter flag named no starter in any of 546 team polls.
- **Daily Faceoff:** "Confirmed" in 357 of 682 team polls, "Likely" in 101.
- **Name matching:** Daily Faceoff names its goalies, with no NHL id. The names matched a goalie-start candidate of the team in 671 of 682 rows.
- **The 11 misses:** goalies listed by a team they hadn't yet dressed for, such as Bobrovsky for TOR on 2026-09-29.

## Options

1. **The source of a confirmation.**
   - a. The NHL's flag first; otherwise Daily Faceoff's "Confirmed".
   - b. Daily Faceoff only.
   - c. Both must agree.
2. **u's goalie doubt for a confirmed starter.**
   - a. 0.
   - b. History's goalie-start value, so u stays the quantity the blend was fitted on (ADR 0030's choice for v1).
3. **Daily Faceoff's "Likely".**
   - a. A likely starter, with more doubt than a confirmed one: his chance of starting is how often Daily Faceoff's "Likely" proved right, measured live before the freeze.
   - b. Ignored, as an unconfirmed team.

## Decision

**The owner's choices on 2026-10-11: 1a, 2a and 3a.**

**The starter at the decision,** for each team, from polls observed before the decision's input cutoff (hard rule 1), latest poll first:
1. **The NHL's starter flag,** when it names one goalie. It wins over Daily Faceoff when they differ.
2. **Otherwise, Daily Faceoff's "Confirmed".**
3. **Otherwise, Daily Faceoff's "Likely":** a likely starter, below.
4. **Otherwise, unconfirmed:** the goalie-start model's mix, as in v1. An empty status counts as none.

**Daily Faceoff's name** is matched to an NHL goalie in three steps:
1. A unique match, ignoring case, accents and punctuation, among the team's goalie-start candidates for the game.
2. Otherwise, a unique surname among them.
   - **Why:** a first name can differ between the sources. #238's audit found Daily Faceoff's "Matthew Murray" for the NHL's "Matt Murray" (8483575), and the NHL lists two goalies by that name.
   - **Recorded:** added on 2026-10-14 before the freeze, and the audit counts a call right by the same match.
3. Otherwise, a unique match among goalies with earlier NHL games.

**A confirmed starter in B3:**
- He replaces the mix: his scenario has probability 1, and B3's other inputs are unchanged.
- **One outside the candidates** (a trade, a signing, a call-up) is rated from his own history: the effect of his latest goalie-effect row known before the decision, from any team. His goalie effect and gamma follow him from team to team (ADRs 0011 and 0022).
  - **Why the latest row:** the morning build runs at 09:00 UTC, before the day's first poll, so it can't rate a goalie the polls name later. A row is as of the morning of his last game as a candidate, so it misses at most the games he has played since then.
  - **His gamma** comes from that effect and the attacking team's xG per shot in the game. **His expected goals saved** use his new team's expected shots.
- **A name that matches no goalie** is rated as a goalie with no earlier game: effect 0, gamma 1, the model's treatment of an unknown goalie. The mismatch is logged for review.

**A likely starter in B3:**
- **His chance of starting** is p = max(L, his own p_start from the goalie-start model). His p_start is 0 if he isn't a candidate. So a "Likely" never makes him less likely than the model already had him.
- **The rest, 1 − p,** goes to the team's other candidates in the goalie-start model's proportions. B3 mixes over these as it does now.
- **L** is the share of Daily Faceoff's "Likely" goalies who started:
  - **Which calls:** each team-game whose latest status at 12:45 ET was "Likely".
  - **Which games:** from the first poll (2026-09-29) to the freeze.
  - **When it's fixed:** once, at the freeze, with its 95% interval. It is never updated within the season, and it's recorded in the freeze commit.
  - **Small samples:** if fewer than 30 such team-games exist by then, L comes back to the owner before the freeze.
- **His rating:** as for a confirmed starter, outside the candidates or unmatched.

**B2,** the yardstick, reads the same starters, so the comparison of B3 with B2 stays like for like (hard rule 3).

**u:**
- **A confirmed team's goalie doubt is 0.**
- **A team with a likely starter has 1 − p**, so a likely starter carries more doubt than a confirmed one.
- **An unconfirmed team** keeps 1 − p_start of its likeliest starter.
- **The game's goalie doubt** is the mean over its two teams, as in ADR 0026.
- u's other parts, its scale and the blend's weights are unchanged.

**A change after the decision** (a late scratch, or a later flag naming another goalie) doesn't touch the bet. The moneyline stands whoever starts, and the ledger is written once and never edited (#164).

**Unchanged from v1:**
- the blend, `blend-live-20261007-89ec631`, which v2 reuses without a refit: its training rows are v1's, since no confirmation exists on history to refit on;
- selection, staking, the guard, the decision time and Pinnacle's price (ADRs 0028 to 0030, 0033).

## Backtest evidence

**None, and none is possible.** No starter confirmations exist for past seasons. Using a game's own starter on history would break hard rule 9. v2's evidence can only come from live games after its freeze.

**Known effect on the blend** (`blend-live-20261007-89ec631`):
- **On u:** with both starters confirmed, goalie doubt falls from about 0.30 to 0. Its standardized part drops by about 3.5, so u drops by about 1.17, or 1.7 of u's standard deviations. A likely starter lowers it part of the way, to 1 − p for his team.
- **B3's weight** falls from 0.49 to about 0.25.
  - The size of that fall rests on b_u, 0.20 [−0.02, +0.42] from the fit's standard errors, whose interval includes 0.
  - In 2021-22's E2 fit, b_u was +0.22 [−0.06, +0.51] (ADR 0030).
- **The hurdle:** its doubt part, 1 point of EV per standard deviation of u above the training mean, mostly falls away.
- **Stakes:** they're no longer divided by 1 + u⁺ in most games.
- **Direction:** the first effect makes bets rarer, the other two make them more frequent and larger. The net effect isn't known.
- **Range:** most v2 games will sit at a u below most of the blend's training games. The blend's weights are then applied outside the range they were fitted on.

## Consequences

**Code:**
- **Live predict and u** read the confirmation (`live/predict.py`, `game/uncertainty.py`, `lineup/` or `features/` for a new goalie).
- **The run bundle** saves the confirmation rows each decision read, so a replay rebuilds the same starters (#171).

**Tests:**
- **Leakage (`tests/leakage/`):**
  - a flag or status observed at or after the decision's input cutoff is never read;
  - a later poll replaces an earlier one only if it was observed before the cutoff.
- **Unit tests:**
  - the source order;
  - the name match;
  - an unmatched name;
  - a goalie outside the candidates;
  - a likely starter's p and the other candidates' shares;
  - u with confirmed, likely and unconfirmed starters.

**The freeze:**
- **When:** after #42's audit (2026-10-13), which reports how often and how early each source named the actual starter. The owner reviews those figures before the freeze.
- **The audit adds one measure:** each source's status at 12:45 ET, with how often its "Confirmed" and its "Likely" proved right. The second is L, fixed at the freeze.
- **The version:** a new `POLICY_VERSION` and `FROZEN_ON` (`betting/selection.py`, `tests/unit/test_freeze.py`).
- **The blend:** `live/blend_fit.py` accepts the v1 blend record under v2.

**Evaluation:**
- v2 counts from the day after its freeze. ADR 0032's rules and its formal review on 2027-04-12 judge v2 on its own games.
- v1's decisions before the freeze stay on record under v1, reported separately.

**Supersedes:** for v2, ADR 0030's rules on live B3 and on u. v1's record keeps them.

## Revisit when

- At the season's review, Daily Faceoff's "Likely" proved right at a rate outside L's interval. L stays fixed within the season.
- #42's audit, or later live use, shows Daily Faceoff's "Confirmed" naming a different starter than the one who played, at a rate the owner judges too high.
- The NHL starts flagging starters before the decision. Then which source comes first is checked again.
- Live u sits outside the blend's training range so often that the weekly reports show the blend behaving unlike its backtest. That is a reason to review, not to change the policy mid-season (ADR 0032).
