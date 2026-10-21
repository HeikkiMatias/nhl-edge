# 0034. Each paper bet's result and the paper bankroll on the dashboard during the season

- Status: Accepted (by the owner, 2026-10-09)
- Date: 2026-10-09
- Amends: ADR 0032 (for the dashboard only), and plan §11's "profit only once a season"

## Context

Plan §11 reviews CLV every week and profit only once a season, "since results over a few hundred bets are mostly noise". ADR 0032 follows it: "Return at the taken price is reported once, at the season's end." So the dashboard (#168, `web/README.md`) never reads a bet's result or profit, although Supabase's `paper_bets` holds them from the nightly settlement.

After the first live day, the owner asked to see each bet's result and the paper bankroll develop after each bet (#210). The rule exists for a behavioural reason. A few dozen bets' profit is mostly luck, and watching it invites reading signal into noise, and pressure to change a frozen policy, which would restart the live count. The rule protects no data from leakage: the policy is frozen and pinned by `tests/unit/test_freeze.py`, and a result is public the morning after its game.

## Options

1. **(a) Show results and the bankroll.** Each bet's outcome, score, how the game ended and profit, and the paper bankroll with its drawdown, with CLV kept as the headline and a note that results over this few bets are mostly luck. The cost is the temptation the rule was there to remove.
2. **(b) Show results, but not the bankroll.** It is less tempting, but the bankroll is the figure the owner asked for.
3. **(c) Keep both hidden until the season's end.** This is the strictest option. The dashboard can't answer how each bet went.

## Decision

**Option (a), chosen by the owner on 2026-10-09 (#210).**

**The dashboard shows:**
- each paper bet's result once settled: won, lost or void, on the full game with overtime and the shootout included (hard rule 2);
- the final score and whether the game ended in regulation, overtime or a shootout;
- the profit in units;
- the paper bankroll after each settled day, from 100 units;
- its drawdown from the running peak, with the 20% line at which plan §11 calls for a data and code review.

**What stays as it was:**
- **CLV per bet against Pinnacle's closing proxy is still the primary measure and the page's headline.** The results sit beside it, with a visible note that over this few bets they are mostly luck and that nothing in the policy changes because of them.
- **The frozen policy `policy-20261005-8ec5cf3` doesn't change because of results:** not its selection, staking, hurdle, blend or guard. A change would be a new policy version with its own ADR and freeze date, and the live count would restart (handover).
- **The formal review on 2027-04-12** and its verdict rules (ADR 0032) are unchanged. No verdict is drawn before it, from results or from CLV.
- **The weekly live report** (`nhl live report`) still gives CLV and its coverage, not return. Return at the taken price appears in the report at the season's end, as before.

## Backtest evidence

None: this decides what the dashboard shows, not a model or policy. ADR 0031's historical figures and ADR 0032's planning figures are untouched.

## Consequences

- **`web/`:**
  - reads `won`, `profit`, `settlement` and `settled_utc` from `paper_bets`, and the final score from `games`;
  - draws the bankroll, its drawdown and the charts (#210);
  - explains every field in plain language.
- **`web/README.md`** no longer says the dashboard never reads results.
- **A new Supabase migration** grants the dashboard's owners a read of `games`' result columns, under the same `is_dashboard_owner()` policy as the paper ledger. The owner applies it.
- **#211's provisional results** (live scores) may show a finished game's bet as provisionally won or lost before the nightly settlement makes it official.

## Revisit when

- The owner finds the results pulling toward a policy change mid-season. Hiding them again is then the remedy, not changing the policy.
- Or the 20% drawdown line is crossed. That calls for the review plan §11 sets, of data and code only.
