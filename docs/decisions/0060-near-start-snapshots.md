# 0060. Near-start snapshots beside the closing proxy

- Status: Accepted (by the owner, 2026-11-09, #254)
- Date: 2026-11-09
- Part of: #254, step 3. The owner, 2026-11-09: "If pinnacle snapshot can fit in the free tier in 254 then do that"

## Context

**The proxy.** The closing proxy (ADR 0033) is the last fresh Pinnacle quote at most 90 minutes before the start. It comes from the slots at 18:45, 19:45 and 21:45 ET:
- most proxies are near the start, with a median lead of 13.5 minutes;
- some are taken up to 75 minutes before it;
- games without a pre-game slot (matinees, 21:30 starts) have none.

**The measurement (11-09 weekly report, #254).**
- **Earlier proxies flatter the bets:** CLV rises by +0.0174 [−0.0006, +0.0328] per hour of lead, on 12 bets in the two longer bands.
- **Over all 44 bets:** CLV is −0.0299 [−0.0411, −0.0228].

**ADR 0033 says** more snapshot slots are "a new decision, made before the games they cover".

**The cost.**
- **The snapshots:** one h2h call (1 credit) per distinct start time. This season's games give about 4.2 start times a game day, about 127 credits a month.
- **The account today:** it spends about 270 a month, so about 400 of the free tier's 500 in total.

## Options

1. **Keep the proxy alone.** No cost, but its lead stays unmeasured, and games without a pre-game slot keep no close.
2. **Near-start snapshots beside the proxy, within the free tier.** About 127 credits a month. The proxy, ADR 0032's coverage and every verdict stay as they are within the season.
3. **Near-start snapshots replacing the proxy.** That would change the primary measure mid-season, after its first interval, which ADR 0032 rules out.

## Decision: option 2 (the owner, 2026-11-09)

**The snapshot.**
- **When:** a game starts within 15 minutes and its start time has no snapshot yet (`ingest/odds.run_near_start`).
- **What:** one Pinnacle h2h call, the eu region.
- **The timing:** the Cloudflare timer dispatches `odds-near-start.yml` at :10, :25, :40 and :55 from 10:55 to 22:55 ET. Every :00, :15, :30 and :45 start gets its snapshot 5 minutes before it, and any other start within 15.

**The credits.**
- **The guard:** a call is made only while the credits left this month, from the newest stored response of any job, also cover what the rest of the month can spend. That's 10 a day for the slots and 4 for re-valuation's calls, today included.
- **Why:** the slots and re-valuation come first, and the 500 hold.

**Kept apart.**
- **The raw responses** go under `odds-near-start/`, never `odds/`. So they never enter `odds_snapshots`, the closing proxy, a decision or settlement.
- **The table:** `nhl odds replay` builds the lake's `odds_near_start` from them.

**Reported beside the proxy** (`live/report.near_start`, in the #254 section). The pair is Pinnacle's latest fresh pair at most 15 minutes before the start (`closing.near_start_pairs`). It reports:
- CLV per bet against that pair, valued as settlement values the proxy;
- on the bets with a valid proxy too, the proxy's CLV and the paired difference;
- apart, the bets without a pre-game slot, outside ADR 0032's coverage.

**From when:** the first games after the timer's redeploy. No earlier game has one.

## Backtest evidence

None. The live reference has no history: the paid history (#323) could give one near each start.

## Consequences

- **The code:**
  - `ingest/odds.py`: the runner, the guard and `CREDIT_SOURCES`;
  - `nhl odds near-start`;
  - `ingest/odds_lake.replay_near_start`;
  - the lake table `odds_near_start` (`LakeOddsNearStart`);
  - `market/closing.near_start_pairs`;
  - `live/report.near_start`;
  - `.github/workflows/odds-near-start.yml`;
  - the timer's `NEAR_START_ET`.
- **The tests:**
  - the runner's due starts, coverage and credit guard;
  - the replay;
  - the pairs;
  - the report;
  - a leakage test that no near-start snapshot reaches `odds_snapshots` or the decision path, and none is taken at or after its start.
- **The owner:** redeploy the timer (`npx wrangler@4 deploy` in `infra/timer`). Until then no snapshot is taken.
- **Unchanged:** ADR 0033's proxy, ADR 0032's coverage and verdicts, and #318's fill references.

## Revisit when

- **The month's credits run short:** the guard skips calls ("skipped, … credits left"). Then reduce them, or take the paid plan (#323).
- **A season's end:** whether the near-start snapshot should become the next season's proxy is a decision made before that season, never within this one.
- **The paired difference is clearly away from 0:** the proxy is then biased by its lead. Raise it for the formal review (2027-04-12).
