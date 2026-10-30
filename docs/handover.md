# Handover, 2026-10-16

This file says where the build stands after the cloud sessions of 2026-09-29 to 10-16, and how a fresh session picks it up. CLAUDE.md holds the rules; this file holds the state. Update it, or delete it, when it goes stale.

## Start here (a fresh session)

1. **Read** CLAUDE.md, this file and `docs/model-card.md`. For the newest work, also read ADR 0038 (challengers) and ADR 0039 (real money).
2. **Take over the scheduled tasks.** They are Routines bound to the session that created them, so they keep firing into the old session, not a new one. List them (`list_triggers`), create each again bound to the new session with the same time and prompt, then delete the old one. A task whose time has passed in the old session needs doing by hand: check its result first. On 2026-10-16 they were:
   - **2026-10-17 10:45 UTC:** check `market_only`'s first challenger output (#255). After the 10-17 nightly, R2 should hold `challengers/live/market_only/1/2026-10-16.parquet`. Its `p_home` should equal that day's ledger `p_blend_market`, game by game, and `nhl live challengers --check --r2` should reproduce it.
   - **Mondays 10:20 UTC:** the weekly live report. It is also the run that decides ADR 0039's real-money start rule and weekly CLV stop (step 3 of its prompt).
   - **2026-11-05 10:40 UTC:** #121 step 3, which also revisits #120's early-exit gap.
3. **Finish any waiting item whose date has come** (the next section). The SessionStart hook lists the open issues of the earliest milestone. Phase 2 closed with gate 1 on 2026-10-21, so that is now P5, whose items wait on the calendar, not on work: don't start them early.
4. **Then the [priority] work for real money (#271):**
   - #268 and #270 are done: the alert, the record, the form and the dashboard's real record (below);
   - #266, the venue, which waits on the owner's steps;
   - #269 once the venue grants API access.

   ADR 0039's earliest start is 2026-11-16. See "Phase 6" below.
5. **Otherwise phase 5 (#14) runs itself.** Everything in its plan (`docs/plans/phase-5.md`) is built. What's left is to watch the live test, finish the dated items and fix what the weekly reports turn up.
   - Gate 3 is not met (ADR 0031): on history, the blend adds no information beyond the recalibrated market. As the owner decided, the frozen policy is traded on paper anyway, and live 2026-27 is the remaining test.
   - Gate 1 is done (ADR 0014, 2026-10-21): B2 does not beat the market on its own, as expected, and stays the baseline.
   - **The clock runs:** policy v2 was frozen on 2026-10-14 and counts from 2026-10-15. v1's bets of 2026-10-08 to 10-14 stay on record under v1 and are reported apart. Every game without a logged pre-game prediction is a paper bet the live test can't use.
   - **Never change a frozen policy** to fit live results. A change is a new policy version with its own ADR and freeze date, and its CLV count restarts.

## Waiting items: when and how

| Item | Ready when | Then |
| --- | --- | --- |
| `market_only`'s first challenger output (#255) | After the **2026-10-17** nightly (09:00 UTC) | As the 10:45 UTC task says. If it's missing or differs, read the nightly's "Challengers" step log first. |
| #268, the real ledger and alerts | Done (#276, #277): the alert, the record, settlement and the dashboard's form. The owner's setup is done and checked on 2026-10-16: the migration applied, `SUPABASE_REAL=true`, and `DASHBOARD_URL` opens the dashboard's sign-in. | Practice alerts arrive daily at about 19:50 Helsinki time; the first, on 2026-10-16, ran end to end (R2 record, Telegram, dashboard row). `telegram-test.yml`'s setup check (branch `phase-6/telegram-test`) reports the link, the migration and `SUPABASE_REAL` to the owner's Telegram. |
| #270, the dashboard's real record | Done (#278) | The owner applied `supabase/migrations/20261016150000_real_record.sql`: the nightly of 2026-10-22 sent rows to `real_days`. |
| The venue: Kalshi, through its API (#266, the owner's decision of 2026-10-18) | Before the start. The law and tax checks are done; Pinnacle stays the manual option. | Built in three PRs: **#286**, the signed client and `nhl kalshi check` (merged in #288; the owner's demo check passed on 2026-10-21, after #300 read the pasted keys and kept an unreadable key from failing runs that don't use it); **#281**, placement, the record and ADR 0040 (merged in #289; the ADR accepted by the owner on 2026-10-22); **#287**, settlement on Kalshi's rules, the nightly check against Kalshi's settlements (`nhl real reconcile-kalshi`) and the dashboard. A game Kalshi resolves at its own price (played over two weeks late, or cancelled) is held, then paid out at Kalshi's price once the nightly check has read Kalshi's settlement (#290); the owner applied `supabase/migrations/20261023120000_real_payouts.sql` on 2026-10-23. #287's migration, `supabase/migrations/20261018180000_kalshi_orders.sql`, was applied by the owner on 2026-10-22. **The owner:** create a demo account and an Ed25519 API key there and on the real account; add `KALSHI_DEMO_KEY_ID` and `KALSHI_DEMO_PRIVATE_KEY` as repository secrets, and the live `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY` as secrets of the `kalshi-live` environment, restricted to `main` (`docs/security.md`, #294); the keys are in, the demo check has passed, and the owner set the repository variable `KALSHI_TRADING` to `demo` on 2026-10-22. Practice orders then go to the demo exchange each midday (`nhl real place`), tracked at every slot and the nightly (`nhl real orders`); review the first days' `real/demo/orders/` records. `kalshi-cancel.yml` cancels resting orders at once. Kalshi refuses trading from GitHub's cloud machines in some US states (#347, 2026-10-26: Washington), so the trading jobs move to the Kalshi runner, the owner's machine in Finland (the owner's choice of 2026-10-29; `infra/runner/README.md`, with `KALSHI_RUNNER` set to `self-hosted`), and a demo week from it passes before the start. Before the first real bet: the runner, the live check from it, a review of the demo run, the owner's deposit and its rate, and the venue commit setting `live/real.VENUE`. |
| The public repository's security (#294) | Before the first real bet | `docs/security.md`: what an outsider can't do, the code's own order limits, and the owner's settings in GitHub (the `kalshi-live` environment, protecting `main`, secret scanning, Actions), Cloudflare (R2 public access, token scopes, a billing notification) and Kalshi (two-factor sign-in, a small balance, deleting every key after a suspected leak). |
| The weekly live report | Mondays 10:20 UTC | As the Routine's prompt says: v2's report, reviewed and committed. It gives counts only, no estimate, until a policy has 4 weeks of games (#235). It is the run that decides ADR 0039's start rule and weekly CLV stop: it runs after Sunday's results are public at 10:00 UTC, while the 09:00 UTC nightly defers to it (#267). From 2026-11-16 it states the verdict, and the owner is told it. |
| #121 step 3, Daily Faceoff's injury statuses | **2026-11-05** | As the Routine's prompt says. Any use needs an ADR and a later policy version. |
| #67, `EXPECTED_GAMES` for 2026-27 | The regular season's end, about **2027-04-17** | See "Keep an eye on". |
| Real money (ADR 0039) | The first decision day from **2026-11-16 to 2026-11-30** on which the start rule held at the latest weekly report | Before then: the venue (#266), and the build issues ADR 0039 lists. Check the start rule at each weekly report from 2026-11-16 on. |
| The morning view (#357, ADR 0052) | Registered on the day its code merges, once the owner accepts the ADR | The morning slot then writes `morning/live/<date>.parquet` and `bundles/morning/<date>/` at about 07:15 ET, and the nightly's "Morning view check" step replays the latest three days. The first morning's log should show its rows by status; a day skipped for the window means the timer's 07:05 dispatch ran late. Counts only in the report under 4 weeks. It is #358's fair value, if it shows information. |
| The formal review | **2027-04-12** | ADR 0032 judges v2 on its own games. v1's record is reported beside it. |

### Gate 1: done (ADR 0014, 2026-10-21)

- **#30's measurement:** 89 games of 2026-27 were fetched again a week later. 39 of them changed beyond event order, about 1% of shot rows, with no goal scorer changed. The owner kept ADR 0004, which now records the measurement.
- **The verdict:** B2 minus B1 is +0.0112 [+0.0044, +0.0185] on E1 and +0.0078 [+0.0013, +0.0146] on E2 (`backtest-20261005-b7639f5`). B2 goes forward unchanged as the player layer's reference, and its under-confidence is left to the blend.
- **The gap review:** it found no bug (`reports/backtest/gap-review.md`).

## Phase 3: done (#12)

Every deliverable merged under its own task issue:
- penalties and faceoffs (#96);
- stints (#97, ADR 0015);
- the 57 empty 2024-25 shift charts (#68);
- `player_league_seasons` (#98, ADR 0016);
- lineup availability (#99, ADR 0017);
- ice time and power-play units (#100, ADR 0018);
- RAPM (#101, ADR 0019), with priors (#102, ADR 0020) and tuning (#103, ADR 0011);
- penalty rates and expected power plays (#104, ADR 0021);
- finishing and goalie conversion (#105, ADR 0022);
- B3 (#106, ADR 0023) and gate 2 (#107, ADR 0024).

**B3** is B2's logistic model with one input, Δĝ, in place of team strength and goalie effects. Δĝ is the home team's expected goals less the away team's, built from the projected lineups. B3 minus B2, paired log loss with a 95% weekly block bootstrap interval:

| Test | Run | B3 minus B2 |
| --- | --- | --- |
| Development 2018-19 and 2021-22, E1 | `backtest-20261003-b1a7b04` | -0.0079 [-0.0130, -0.0028] |
| Hockey validation 2023-24 and 2024-25 | `backtest-hockey-20261003-93d0f92` | -0.0078 [-0.0130, -0.0027] |
| One-time test 2025-26 | `backtest-hockey-20261003-82fbada` | -0.0030 [-0.0094, +0.0034] |

- **The verdict (ADR 0024):** gate 2's wording is not met on 2025-26, but B3 goes forward as the model phase 4 compares with the market. B3 minus B2 stays in every report (hard rule 3), and live 2026-27 is B3's next independent evidence.
- **B3 against the market:** on the development seasons, B3 minus B1 is +0.0033 [-0.0013, +0.0077] on E1 and -0.0000 [-0.0047, +0.0043] on E2. Level with the market isn't an edge. The blend is what must add information.
- **Calibration moves between seasons:** the slope is 1.34 on the development seasons, 0.94 on hockey validation and 0.55 on 2025-26. An inputs-only check of 2025-26 found no data shift (model card).
- **The one-time test is spent.** `nhl backtest --hockey-only --one-time-test` is refused for good. Records of the run sit in R2 (`ledger/one_time_test.txt`), beside the lake and in `reports/backtest/one_time_test.txt`.
- **The gap review** (`reports/gaps/b3-gap-review.md`, tool `nhl audit gaps`) found no data error in 92 games read without results.

**P3 follow-ups, decided by the owner on 2026-10-09:**
- **#120 is closed** by the owner's decision, with no lineup change for now. The figures are on the issue; all were measured on the training seasons only:
  - **After an early exit:** the gap is +0.137 [+0.116, +0.158]. It moves +0.96% [+0.83%, +1.08%] of the expected 5v5 minutes of a team-game with such a skater. This gap is revisited with #121 step 3.
  - **Openers, for candidates who didn't dress in the team's last game:** the gap is −0.082 [−0.122, −0.042]. It moves −3.9% [−5.3%, −2.4%] of an opener team-game's minutes. These intervals resample whole seasons, since a season's openers fall in one week.
  - **Openers, for skaters who did dress in the team's last game:** the gap is +0.013 [−0.009, +0.033]. That interval includes zero, so it was recorded and not pursued.
- **#125 and #134 are parked for the next model version** (label `next-model-version`, milestone P6). #125 is a debutant's NHLe in his debut season; #134 is replacement skaters rated at RAPM's reference. They are decided after the formal review on 2027-04-12, each through its own ADR.
  - #125's is ADR 0050, Proposed on 2026-10-29: it recommends a1, refreshing a debutant's prior once his lines are public, with the training-season evidence in `reports/ratings/debut-nhle-20261029-0087fbc.md` (`scripts/debut_nhle.py`).
  - #134's is ADR 0051, Proposed on 2026-10-30: it recommends keeping replacement skaters at 0, since no level helps, with the evidence in `reports/lineups/replacement-level-20261030-bc2e843.md` (`scripts/replacement_level.py`). The replacements' minutes sit mostly in teams' first games, where they stand for summer arrivals rated about average (ADR 0017's limit).

Done since: #117 and #199, the yearly landing refresh (#200, #205); #130 (#185); #131 (#201); #132 (#202).

## Phase 4: done (#13)

Every deliverable merged under its own task issue:
- 2022-23's price checks (#66, ADR 0025);
- the blend's training folds (#138);
- the uncertainty score u (#139, ADR 0026);
- the market blend (#140, ADR 0027);
- selection and staking (#141, ADR 0028);
- the market move guard (#142, ADR 0029);
- E3 and attribution (#143);
- the development backtest and the freeze (#144, ADR 0030);
- 2022-23's one run (#145);
- gate 3 (#146, ADR 0031).

**The frozen policy v1, `policy-20261005-8ec5cf3`** (model card, "The freeze"), live from 2026-10-06 to 10-14:
- **The blend:** logit p = a + b_m·logit p_mkt + (b_x + b_u·u)·logit p_B3, fitted per whole-season fold on earlier folds' out-of-sample predictions.
  - Live bets use **E1's fit**, learned against SBR's close and fed Pinnacle's 12:45 ET price (ADR 0030).
- **u:** goalie doubt, availability doubt and rookie minutes, equally weighted. Under v1, its goalie doubt reads the goalie-start model, never a confirmation (ADR 0030).
- **Selection and staking:**
  - a hurdle of 2.5% expected return plus 1 point per standard deviation of u above average;
  - a quarter of Kelly divided by (1 + u⁺);
  - caps of 1.5% per bet and 5% per day;
  - 100 units a season.
- **Bets** are placed at 12:45 ET at Pinnacle's price, with the best EU book logged beside it.
- **The guard** skips a bet whose side fell more than 5.35 points between Pinnacle's 07:05 and 12:45 quotes.
- `betting/selection.py` holds `POLICY_VERSION`, `FROZEN_ON` and `LIVE_BLEND_EXPERIMENT`, and `tests/unit/test_freeze.py` fails if a frozen number changes.

**Policy v2, `policy-20261014-e15db87`,** is v1 plus the day's confirmed goalies, live from 2026-10-15. See "Phase 5: where it stands".

**Gate 3 (ADR 0031): not met.** The pooled figures combine 2021-22 and 2022-23 (`reports/backtest/accepted.json`):
- **BLEND minus B1:** E1 -0.0002 [-0.0037, +0.0032] and E2 -0.0027 [-0.0072, +0.0019]. 2022-23 alone loses on both.
- **B3 minus B2:** -0.0068 [-0.0120, -0.0016] over three seasons: passes overall, with the 39-game lineup-change subset inconclusive.
- **The blend's calibration:** passes on intervals.
- **CLV against SBR's close:** -1.23% [-2.01%, -0.46%] over 625 bets. That fails for E2's fit at the opener; the live policy's E1 fit has no historical bets, so only live can judge it.
- **Gaps:** reviewed by hand, with one data error and no bug.

**2022-23 is spent.** Its one run is `market-validation-20261005-6ec331b`, claimed in R2 (`ledger/market_validation.txt`), beside the lake and in `reports/backtest/market_validation.txt`. A second run is refused.

**ADR 0026's revisit trigger fired.** 2022-23's fits put u's weight at +0.24 [+0.01, +0.48] (E1): the model is trusted more as doubt grows. The owner kept u frozen. Live evidence decides, and any change is a new policy version.

**Its follow-ups are done:** #154, the E3 driver (task 7 below), and #156, the gap screen.

## Phase 5: where it stands (#14)

**The plan** was approved by the owner on 2026-10-05 and is committed as `docs/plans/phase-5.md`, also posted on #14. Paste it into every phase 5 PR's description, as CLAUDE.md asks.

**A live day runs on its own** (docs/data-sources.md, "When jobs run"; the Cloudflare timer dispatches each job on time):
- **Odds:** five Pinnacle snapshots a day. The 07:05 ET morning one feeds the market move guard.
- **Goalie polls:** hourly at :50, from 12:50 to 02:50 UTC.
- **The 12:45 ET midday dispatch:**
  - it polls the goalies, raw only, then takes the odds snapshot;
  - it runs `nhl predict`, which reads the day's polls itself under v2 (`live/goalies.py`);
  - it writes the write-once ledger and the run bundle to R2, and mirrors the ledger to Supabase.
- **Kalshi watch (#319):** at 07:05, 11:05, 15:05 and 19:05 ET, each run watching every game's markets until a minute before the next.
- **Kalshi re-valuation (#321):** every 15 minutes from 13:05 to 22:50 ET, besides each odds slot.
- **Kalshi books (#282):** every slot ends with `nhl kalshi books --mirror-raw`. From the midday alert to each game's start, it keeps Kalshi's order book of the alerted bets' markets in R2 and the lake's `kalshi_books`, privately, for the owner's own orders (#281). It prints counts only, and its data never goes into the repository (docs/data-sources.md, "Kalshi order book").
- **The 09:00 UTC nightly:**
  - the ingest, then `nhl recheck`;
  - the odds replay, with each game's closing proxy;
  - `nhl live settle` and the Supabase copy;
  - the goalie replays and the day's slate feature rows;
  - the live report to Supabase, the run bundle check, the challengers (#255) and the Kalshi price shadow (#320).

**The pieces, all merged:**
- **The live data,** each with its task:
  - live targets and feature rows (#162, #180, #181);
  - the season's one live blend fit, `reports/live/blend-live-20261007-89ec631.json` (#163, #182). It is claimed in R2, and a second fit is refused. Its weights: a −0.065, b_m 0.710, b_x 0.492 and b_u 0.203, on 4,875 games.
- **`nhl predict` and the write-once ledger** (#164, #183, ADR 0033). The quote must be at most 5 minutes old at the decision and at most 15 at publication.
- **Live data readiness** (#170, #184, #185): a game that starts before publication gets no prediction.
- **Run bundles** (#171): each decision day's inputs, fits and hashed manifest, under `bundles/live/<date>/`. `nhl live replay` reproduces a day from its bundle alone.
- **The closing proxy** (#21), and **settlement and CLV** against it (#165).
- **The live report** (#166, ADR 0032): `nhl live report --r2`, interim with no verdict until the formal review. Under 4 weeks of games it gives counts and no estimates (#235).
- **Edge attribution** (#154, #193): `nhl live slate` prints each bet's driver against usual levels fixed beside the live fit.
- **Supabase and the dashboard** (#167, #168):
  - The site in `web/` shows the latest slate, the paper bets with their results, the bankroll, each night's CLV per bet, and the latest report.
  - Results show during the season (#210, ADR 0034), live scores too (#211, #217), and every time is in Helsinki time (#218, #219).
  - The owner signs in with a password (#206). The owner applied every migration in `supabase/migrations/`.

**Policy v2 (#222, #225; ADRs 0035 to 0037).**
- **No new B3 term passed** (#223, #224). The arena term (ADR 0035) and the team-level terms (ADR 0036) were each scored under a pre-registered ADR on 2018-19, 2021-22, 2023-24 and 2024-25. The coach run was repeated twice after two fixes from Codex on #227, with the same figures to the last digit; all three runs stay logged. None passed, so v2's B3 is v1's.
- **What v2 adds: the day's confirmed goalies** (ADR 0037). The policy constants (`betting/selection.py`) are:
  - `POLICY_VERSION = policy-20261014-e15db87` and `FROZEN_ON = 2026-10-14`;
  - `LIKELY_STARTS = 46/47`;
  - `FIT_POLICY = POLICY_V1`, so v2 reuses v1's live blend and attribution levels.

  `tests/unit/test_freeze.py` fails if any of them changes.
- **The evidence for the sources and L** (#238, `reports/audit/2026-10-13.md`):
  - the NHL's flag named no starter before any start, in all 210 team-games with a known starter;
  - at the decision snapshot, for the 208 team-games starting after it, Daily Faceoff's "Confirmed" was right in 85 of 85 and its "Likely" in 45 of 45; 78 had no call.
- **The code:**
  - `lineup/confirmed.py`: the calls, the name match to NHL ids, the starters, and a goalie outside the candidates;
  - `live/goalies.py`: the day's polls into the goalie rows that B2, B3 and u read;
  - `audit/goalies.py`: the decision measure.

**The live record so far:**
- **v1** (`policy-20261005-8ec5cf3`): 52 games and 21 bets on 2026-10-08 to 10-14. Its final report is `reports/live/report-2026-10-15-policy-20261005-8ec5cf3.md` (#262): counts only (2 weeks), every eligible bet with a closing proxy, and no operational problem.
- **v2** (`policy-20261014-e15db87`): first day 2026-10-15, 11 games and 3 bets.
  - The calls and polls were all saved in the run bundle, and all of them were taken before the decision snapshot.
  - `nhl live replay --r2` reproduced the ledger, every column.
  - The paper bankroll stood at 104.49 units at that decision.
  - **The first CLV interval:** it needs v2's eligible bets with a closing proxy to span 4 Monday-to-Sunday weeks. The 4th such week starts on 2026-11-02, so the earliest weekly report with an interval is 2026-11-09's, and later if a week has no qualifying bet. Until then a counts-only figure is expected, not a failure (Codex on #274).
- **The first weekly report** (`reports/live/report-2026-10-14.md`, #235): no operational problem.
  - Both ANA–CGY games had u's rookie share beyond 3 training standard deviations. That is ANA's young lineup, with no data gap (#236).
  - Availability doubt passed 3 standard deviations once (MIN at FLA on 2026-10-10).
- **One bankroll runs through both policies.** `predict.bankroll` stakes from the season's whole ledger, and the dashboard's bankroll line runs on. The report's drawdown review counts every policy's bets (ADR 0037). Each policy's CLV, calibration and comparisons count only its own bets.

**What's left in phase 5:**
- #169, task 9: a second copy of `raw/` outside R2. The owner chose, on 2026-10-09, to keep it on their own computer, set up later. The steps are on the issue (`nhl lake restore-raw` with a read-only R2 token).
- #264 (fixed): a replay stamps the policy its day's manifest names, so v1 days replay every column again. Today's selection, guard and staking decide every replay, which v1 and v2 share. A replay of any policy outside `selection.REPLAYABLE` is refused, so a freeze that changes them must add code that replays its days.
- #253, selected-bet calibration: a diagnostic from the review agenda. #254, the proxy's lead, is in the weekly report (below), and its cost is counted on the issue; the owner decides once the measure has intervals.
- #121 step 3, on 2026-11-05; #67, around 2027-04-17.
- #173 (done, 2026-10-21): post-game corrections move the live predictions by hundredths of a point and change no bet (ADR 0004's "Measured at the level of predictions"). `nhl audit corrections-replay --work <scratch> --r2` runs it again as rechecks accumulate; it takes about 15 minutes, and never writes the lake, R2 or the ledger.

## Phase 6: the review agenda (#250) and real money (#271)

**The review agenda.** An external review on 2026-10-15 gave the agenda in #250. Three of its diagnostics are now in the weekly live report, with no verdict:
- **Entry to close** (#251, `live/movement.py`): whether Pinnacle's price moved toward each model's disagreement by the close. First reading: 42 games, a mean move of 1.0 point, and a correlation of 0.05. It gives counts only until 4 weeks.
- **The closing proxy's lead** (#254): Pinnacle's fair move from the decision to the proxy, and the bets' CLV, by the proxy's lead before the start (up to 30, 30 to 60 and over 60 minutes), with each one's slope on the lead. It gives counts only until 4 weeks. If either changes with the lead, the proxy is biased for some start times.
  - **The cost of a fix:** a near-start h2h snapshot for each distinct start time would cost 799 credits a season (at most 139 in a month, in March). That is against about 290 a month now and the free tier's 500. It would also need the timer to fire at each start time.
- **Shadows** (#252, `live/shadow.py`):
  - **Market-only selection:** the frozen rules on the market-only control made 0 bets.
  - **Best-price execution:** a better fresh bookmaker price existed for 7 of v1's 17 bets with a close, adding +0.56 points of CLV. Counting the exchanges (before commission), 16 of 17 had a better price, adding +1.8 points.
- **Edge bids on every game** (#308, `live/shadow.py`): a resting bid on every side the policy didn't bet, at the lowest price to the cent where the blend clears that side's hurdle, filled at the bid by the first later fresh Pinnacle quote at or above it before the start, and valued against the same close. Beside it, the share of fills after Daily Faceoff's named starter of either team changed, the adverse selection it would suffer. First reading (v2's decisions of 2026-10-15 to 10-22): 102 bids, 8 filled (7 at the 18:45 slot, 1 at 19:45), all 8 with a close, and no starter change before any of them. Its CLV gives counts only until 4 weeks; a positive interval after enough weeks would earn its own ADR and a new policy version (#308).

**The second external review (2026-10-23, #317).** Its agenda: select and execute on Kalshi's own prices against a sharp fair value. The work: #318 to #323.
- **Done: the reference after each fill** (#318, `live/fills.py`). `nhl real fills --r2` runs nightly. Each order's taker part and maker part get CLV only from the fills observed before the game's Pinnacle proxy; a later fill is counted apart, never valued against an earlier quote. The figures go privately to `real/<env>/fill-reports/`, with counts only in the log.
- **Built: every game's Kalshi markets watched through the day** (#319, `ingest/kalshi_watch.py`, `kalshi-watch.yml`). The top of book is polled each minute, and the full books and the trades every 5 minutes, from 07:05 ET to each start, into the private `kalshi_tops`, `kalshi_depth` and `kalshi_trades`. The owner redeployed the timer on 2026-10-28, and it now dispatches the four windows; that day was watched from one manual run at 19:27 UTC.
- **Built: each order in two parts** (#321 part A, ADR 0045, accepted by the owner on 2026-10-23). Each alerted bet's order is an immediate-or-cancel taker part at L_t, then a post-only maker part at L_m, the higher limit the maker fee allows, sized from what the taker part filled; a bid that would cross the book goes a cent lower, down to L_t. It goes to the demo exchange from the merge, for the week of practice before 2026-11-16. ADR 0040's orders already placed are tracked to their end as one order.
- **Built: the Kalshi price shadow** (#320, ADR 0046, `live/kalshi_shadow.py`, `nhl real shadow`, nightly). Two fair values select on Kalshi's own prices from the watch's logs: Pinnacle de-vigged, and `conservative_blend`'s. Both use one taker and maker execution and one notional 100 € budget each. It is private in R2 and paper only, with no verdict before 4 weeks. Its days 2026-10-24 to 10-27 were kept with no watch logs, before the timer's redeploy on 10-28; its hockey arm starts with the blend's first fitted week (about 2026-11-16).
- **Built: re-valuing a resting maker part** (#321 part B, `live/revalue.py`). A resting bid is re-valued at every odds slot with the slot's snapshot. Every 15 minutes (`kalshi-revalue.yml`) it is also re-valued on a 3¢ move in Kalshi's top of book, with one Odds API h2h call (at most 4 a day), or on a change in Daily Faceoff's named starter, which cancels it. The bid is lowered or cancelled at the frozen blend with Pinnacle's fresh price and the decision's B3 and u, and never raised. The timer has dispatched it since its redeploy on 2026-10-28.
- **Built: the scale-up rule** (#322, ADRs 0047 and 0048, accepted by the owner on 2026-10-29). Growing the real bankroll is read once, at the weekly report on the Monday 12 weeks after the first real fill's week, taker and maker fills apart (`live/scale_up.py`, in `nhl real fills`'s private figures). The weekly report shows the date and counts only. The verdict goes to the owner, and a pass permits growth, never makes it. ADR 0048's Holm step-down applies at the challenger review on 2027-04-12; its code is #350, to build before then.
- **Still the owner's decision:** one or two paid Odds API months for the historical Pinnacle prices of 2022-11-28 to 2025-26 (#323).

**Research run bundles (#186).** Every `nhl backtest` run keeps its per-game rows in `bundles/research/<run id>/` (pass `--r2` so a session's run outlives its container). The bundle is sealed only once they reproduce the report. `nhl research compare` pairs two kept runs' models game by game, with intervals, and names the inputs they read differently (docs/data-sources.md). Runs before 2026-10-21 have no bundle, and spent tests are never run again to make one.

**Challengers (#255, ADR 0038).** New models are judged only on decision days after their registration, never on history again.
- **Where:**
  - each challenger is an entry in `live/challengers.py`;
  - `nhl live challengers --r2` scores it nightly, after the bundle check, from the day's run bundle;
  - each output is written once to `challengers/live/<name>/<version>/<date>.parquet`, and the lake's `challenger_predictions` is rebuilt from them;
  - the live report's Challengers section reports them.
- **The rules (ADR 0038):**
  - its ADR is fixed before its first scored game, and nothing is backfilled;
  - a day without its output withholds the challenger's figures;
  - every registered challenger is reported;
  - a challenger enters a policy only through a new policy version and the owner.
- **The lake, point in time:** a challenger may read only the tables in `challengers.READERS`, each through a reviewed reader:
  - `games`, through `results_known_at`;
  - `schedule`, through `schedule_known_at`;
  - `odds_snapshots`, without its after-the-fact columns;
  - `paper_ledger`, the decisions published before the cutoff, read from the ledgers in R2 rather than the lake's copy;
  - `goalie_spreads`, each v2 day's spreads, kept once from its bundle (`challengers/inputs/goalie_spread/`) and read from there.

  Any other table needs its reader and a leakage test first.
- **Registered:**
  - `market_only`, from 2026-10-15, as a check of the mechanism. On R2's 2026-10-15 bundle its 11 games equalled the ledger's `p_blend_market`, and `--check` reproduced them.
  - `conservative_blend` (#256, ADR 0041), from 2026-10-22: the market plus B3's disagreement with it, fitted each ET week with fixed priors on the earlier weeks' v2 decisions. Below 200 training games its rows are `unfitted`, which should last until about the week of 2026-11-16. Each day's fit is saved in the output's `fit` column, and the report shows the latest.
  - `goalie_spread_blend` (#257, ADR 0042), from 2026-10-22: `conservative_blend` with B3's weight moving with the goalie spread, the SD of B3's log-odds over its goalie scenarios. It's paired with `conservative_blend`, its baseline, on all games and on those with a spread above 0. The inputs-only reading before registration is on #257.
  - `team_residual_blend` (#258, ADR 0043), from 2026-10-22: `conservative_blend` on B3 corrected by each team's effect on what B3 leaves unexplained, fitted each Monday on the earlier weeks' v2 residuals (prior N(0, 0.2²), half-life 6 weeks). It's paired with `conservative_blend`, its baseline, and saves the week's effects with each output.
  - `b3_split` (#259, ADR 0044), from 2026-10-22: B3 with Δĝ split into its four parts (5v5, special teams, finishing, goalie), each with its own weight, fitted once on the live B3's training games (`reports/live/b3-split-20261022-5656908.json`) and judged against B3's own `p_b3`. It predicts from its first day. The stability check is on #259. Next season's B3 refit brings a refit of the split as a new version.
  - `close_model` (#260, ADR 0049, accepted by the owner on 2026-10-29), from 2026-10-29: Pinnacle's move from the decision to its closing proxy, forecast from B3's disagreement, Pinnacle's move since the morning and the other books' gap, with a shrunk linear fit refitted each ET week on the earlier weeks' v2 games with a close. Below 200 training games it is `unfitted`, about mid-November. Its primary measure is its forecast move's squared error less no move's. It is the fifth candidate under ADR 0048's Holm step-down (#350).
- **At the review (ADR 0048, #350):** the report's challengers section gives each candidate's primary measure's p-value, its Holm threshold and whether it is a candidate. It does this only at the formal review on 2027-04-12, over every registered challenger but `market_only`. A challenger's evidence starts the day after registration, so the earlier one is registered, the more games it gets.

**Real money (ADR 0039, the owner, 2026-10-15 and 10-16).** Real stakes start from mid to late November 2026, beside the paper ledger, which continues. The tracking issue is #271.
- **The start rule** (`live/real.py`; the report's "Real stakes (ADR 0039)" section, #267):
  - **(a) CLV:** v2's CLV per bet interval has its upper bound above 0, the 90% coverage floor holds, and the pessimistic bound's upper bound is above 0 too;
  - **(c) operations:** every run bundle is complete, and no day had games but no decision;
  - **(d) the venue:** `real.VENUE` is set.
- **Two signals block a start beside the rule:**
  - **the owner's veto:** the paper bankroll at or below 100;
  - **the drawdown review:** a 20% fall from a peak.

  Each is a point estimate, so it never passes the rule on its own (hard rule 7). Codex's P0 on #272 asking to drop the veto is won't-fix, by the owner's decision of 2026-10-16.
- **When it decides:** only the Monday weekly report decides, the scheduled task at 10:20 UTC, after Sunday's results are public at 10:00. It reads only evidence public at its clock. The 09:00 UTC nightly defers to it. The window is 2026-11-16 to 11-30; after that, a start needs a new decision by the owner.
- **The stakes:**
  - the real bankroll is 100 €;
  - each stake is the frozen staking rule recomputed at the executable price, never above the paper fraction: at most 1.5% of the real bankroll, so 1.50 € at the start;
  - a stake below the venue's minimum is skipped, not rounded up.
- **The stops:**
  - the real bankroll falls 20%: checked before every day's bets, from the real ledger (#268);
  - v2's CLV interval or its pessimistic bound falls entirely below 0: at a weekly report;
  - the coverage floor fails: a pause until it holds again.
- **The venue (#266):**
  - **Veikkaus forbids programmatic fixed-odds betting** ("Kiinteäkertoimisen vedonlyönnin ohjelmallinen pelaaminen on kielletty"), so it is manual only, and unofficial API wrappers are never used.
  - **Pinnacle is the best fit:** its Bets API is open by application only, since 2025-07-23, and it supports pregame handicapping projects. The policy already decides at its price.
  - **Matchbook's API** officially supports trading bots, after an inquiry form.
  - **Betfair's availability** in Finland is unclear.
  - **Finland's licensed market** opens on 2027-07-01 (reported).
- **The build:**
  - #267 is done (#273);
  - #268 is done (#276, #277): the real ledger, the Telegram alert, the dashboard's form and settlement;
  - #269: API placement, once access is granted;
  - #270 is done (#278): the dashboard's real record.

## The owner's standing decisions

- **Ask the owner first** before any ADR, any won't-fix on a P0, or anything outside an issue's scope. Explain choices in plain language, without jargon.
- **One PR per issue,** never combined.
- **Tuning (ADR 0011):** each component is tuned once on the training seasons and frozen. That covers team strength, goalie effects, schedule terms, B2's L2 penalty and RAPM. The penalty model and finishing tune nothing (ADR 0021, 0022): they reuse RAPM's and the goalie effect's frozen settings, and they measure their pulls and league figures from earlier data each season. Near-ties go to the steadier setting. The tuning runs' losses are not walk-forward evidence. Codex's P0s on #88 and #92 are won't-fix, by the owner's decision.
- **B2 (ADR 0013) and B3 (ADR 0023):**
  - Both train on the starters who played and predict by mixing over the goalie-start probabilities. B3 trains on the projected skaters, not the ones who dressed. Codex's P0s on #90, #92 and #133 are won't-fix, by the owner's decision.
  - B3 reuses B2's L2 of 100.
  - Gate 2's subsets stay as ADR 0023 defined them.
- **Seen seasons:**
  - The development seasons (2018-19, 2021-22) and the hockey validation seasons (2023-24, 2024-25) have been scored. 2025-26 and 2022-23's 342 priced games are spent. Nothing may be tuned on any of them.
  - A change to B2 or B3 now needs the owner and an ADR.
- **Gate 2 (ADR 0024):** B3 is phase 4's model against the market. B2 stays in every report as B3's reference.
- **Phase 4 (ADRs 0025 to 0031):**
  - The policy was frozen as `policy-20261005-8ec5cf3`, with live games from 2026-10-06. Policy v2 (`policy-20261014-e15db87`, ADR 0037) adds the day's confirmed goalies and counts from 2026-10-15; v1's record stays separate.
  - Live bets use E1's blend fit and Pinnacle's 12:45 ET price, and under v1 u never reads a confirmation (ADR 0030). Codex's P0s on #155 and #159, "closing odds in a tradable prediction" for E1's fit, are won't-fix by the owner's decision: the fit reads only earlier seasons' closes.
  - Gate 3 is not met, and phase 5 goes ahead regardless (ADR 0031).
  - u stays frozen although its revisit trigger fired: live evidence decides.
- **Data:**
  - **Stints (ADR 0015):** drop only impossible on-ice counts.
  - **Time-on-ice reports (#68):** fetched once, for the 57 games only, and never again (NHL.com's terms).
  - **Season totals (ADR 0016):** the 2026 copies' goals and assists count as public at the season's end, corrections included.
- **Lineups (ADR 0017, 0018):** the specifications are fixed, with nothing tuned. A change to their inputs, window or total needs a new ADR (#120).
- **Policy v2 (#222, decided 2026-10-11 to 10-14):**
  - Daily Faceoff's "Confirmed" counts as confirmed. If the NHL flags a different starter, the NHL wins.
  - A confirmed starter's goalie doubt is 0 ("confirmed is confirmed").
  - A "Likely" starter is a likely choice with more doubt: L = 46/47, the rule of succession on 45 of 45 right.
  - ADRs 0035 and 0036 are accepted: their terms didn't pass, so they stay out of B3.
- **One paper bankroll a season** runs through every policy. Each policy is judged on its own bets.
- **Dependabot:** on 2026-10-14 the owner asked for the bumps to be checked and merged as needed. Polars 2.0 went in after the slow suite passed and every live day's bundle replayed identically (#230, #242).
- **The dashboard shows results during the season (ADR 0034).** If the results start pulling toward a policy change, hiding them again is the remedy, never changing the policy.
- **Real money from mid to late November 2026 (ADR 0039, the owner, 2026-10-15).**
  - **The real bankroll:** 100 €. It bets the frozen policy's paper bets, staked by the frozen rule at the executable price and never above the paper fraction: at most 1.5% of the bankroll, 1.50 € at the start. The paper ledger continues beside it.
  - **The start:** the first decision day from 2026-11-16 to 2026-11-30 on which the start rule held at the latest weekly report, Monday's at 10:20 UTC. The rule is three conditions: v2's CLV interval, its coverage floor and its pessimistic bound all hold (upper bounds above 0); the operations are clean; and the venue is ready (#266). Beside them, the owner's veto (the paper bankroll at or below 100) and the drawdown review each block a start, and never pass one. If the rule hasn't held by November's end, a start needs a new decision by the owner.
  - **The stops:** the real bankroll falls 20%, checked before every day's bets. Or, at a weekly report, v2's CLV interval or its pessimistic bound falls entirely below 0; a failed coverage floor pauses real bets.
  - **Placement:** manual from alerts until an API venue is confirmed.
- **#268, the real ledger (the owner, 2026-10-16):** the day's real bets reach the owner as a Telegram message; placed bets are recorded on a dashboard form; practice alerts run daily until the start and never count in the real bankroll; a bet recorded after its game started is accepted and marked late, if it was placed before the start.
- **Challengers (ADR 0038, the owner's approval of #255's plan, 2026-10-15):** pre-registered, judged on future decision days only, every one reported, and never a change to the frozen policy.
- **The venue (2026-10-15):** any major sportsbook or exchange open to Finland that allows API betting may be used, under its own terms. Veikkaus itself matters little to the owner.
- **The repository is public** since 2026-10-09, so Actions minutes are free (#213 has the history).
- **Dependabot's four patch bumps of 2026-10-05** (#174 to #177) were closed at the owner's request. None was a security fix.

## State

- **Lake (R2):** every regular-season game from 2010-11 to 2025-26 (19,152), plus 2026-27's as the nightly ingest adds them.
  - **Raw facts:**
    - `games` and `schedule` (ADR 0003, 0005), `players`;
    - the per-game tables `shots`, `shifts`, `actual_lineups`, `shift_coverage`, `strength_time`, `penalties` and `faceoffs` (#5, #72, #96, ADR 0004, 0009).
  - **Phase 2's features,** through 2026-09-30:
    - `shot_xg` (ADR 0010);
    - `team_strength`, `goalie_effects` and `schedule_terms` (ADR 0011);
    - `goalie_starts` (ADR 0012).
  - **Phase 3's tables,** through 2025-26 (the last rows are 2026-04-16):
    - `stints`, through 2026-09-30;
    - `player_league_seasons`;
    - `lineups` and `lineup_replacements` (`lineup-20261002-6297f8c`);
    - `player_ratings` and `rapm_terms` (`rapm-20261003-86a736f`);
    - `penalty_rates` and `expected_power_plays` (`power-plays-20261003-86a736f`);
    - `finishing` and `goal_multipliers` (`finishing-20261003-f87acde`).

    The live season's rows are built for each slate by the nightly's `nhl live features` (#162). The SessionStart hook lists the tables that stop before today on this machine as "behind", and that is expected until `nhl status`'s pulls bring them up to R2.
  - **Tuning cutoff:** the tuned tables' rows of 2011-12 to 2017-18 count as known only from 2018-04-09 10:00 UTC.
  - **Market and goalie data:**
    - `odds_snapshots` holds every Odds API snapshot from 2026-09-28 on (#20).
    - `sbr_odds` holds SBR's openers and closes from 2010-11 to 2022-11-27 (#7, 133,594 prices).
    - `pregame_goalies` and `dailyfaceoff_goalies` hold the goalie polls since 2026-09-29 (#43, #48).
- **Reference files** (`src/nhl_edge/reference/`, #8, #26): team codes, arenas, venue names, home arenas, coaches and attendance limits. `nhl audit reference` checks them. Features read them through `coaches_known_at` and `capacity_share`.
- **SBR archive** (#7, #52, #56, ADR 0006, 0007):
  - It is HTML only and stops on 2022-11-27. That gives 2022-23 342 of its 1,312 games (#66).
  - E2 reads the opener at 10:00 US Eastern on the game date.
  - `reference/sbr_suspect_openers.csv` lists 40 likely wrong openers, and its `bad_close` column marks 3 bad closes (#64).
- **Backtest:**
  - `nhl backtest` runs the walk-forward of B0 to B3, the three blends, the policy's bets and E3 on the development seasons. It writes `reports/backtest/summary.json`, `runs.csv`, `bets.csv`, `gaps.csv`, `gaps_b3.csv` and `gaps_blend.csv`.
  - `nhl backtest --seasons 20222023 --market-validation` was 2022-23's one run, and it is refused for good.
  - `reports/backtest/accepted.json` holds the frozen policy's figures and gate 3's verdict.
  - `nhl backtest --hockey-only` scores B2 and B3 on outcomes alone, without prices, as for 2023-24 and 2024-25 at gate 2.
  - `nhl audit gaps` explains each B3 gap for review. `--blend` does the same for the blend's gaps, each at its own prediction time.
  - **The baselines:**
    - **B0** is the de-vigged market (ADR 0008).
    - **B1** is its per-fold recalibration. B1 doesn't beat B0: B0 minus B1 is +0.0004 [-0.0005, +0.0013] on E1.
  - The model card has every figure.
- **CI** (#213): `ci.yml` runs the Python checks, and `web.yml` the dashboard's, once per change: on its pull request, and on `main` after the merge. A push to a branch with no pull request runs nothing, and a newer push cancels the older run. `ci.yml` skips changes confined to `web/`, and `web.yml` runs only when `web/` changes.
- **The repository is public** (2026-10-09):
  - Actions minutes are free. When it was private, 1,804 of the 2,000 included minutes were used by 2026-10-09.
  - Run logs are public, so never print a secret or anything private in a workflow. Secrets are masked, but values passed as plain variables are not.
  - GitHub pauses scheduled workflows in a public repository after 60 days without a commit. The timer's dispatches keep running regardless, and a commit restarts the schedules.
- **Jobs:**
  - **What runs:** GitHub Actions runs the nightly ingest at 09:00 UTC, then `nhl recheck`. It also runs five odds slots a day, and the goalie polls hourly at :50 from 12:50 to 02:50 UTC (docs/data-sources.md, "When jobs run").
  - **The timer:** the Cloudflare Worker `nhl-edge-timer` (`infra/timer`, #53) dispatches them on time. `TIMER_ACTIVE` is `true`, so GitHub's own late schedule skips.
  - **If dispatches stop:**
    - Run `gh variable set TIMER_ACTIVE --body false`, then `npx wrangler@4 tail` in `infra/timer`.
    - A 401 is a bad or expired token (it expires around 2027-09). A 403 or 404 means the token lacks Actions read and write.
    - `npx wrangler@4 secret put GITHUB_TOKEN` stores a new token.
    - Start the goalie polls by hand meanwhile, but never an odds slot: it spends Odds API credits.
- **Open, for the owner:** #169, the raw backup on the owner's own computer (P5); #266's venue steps: the Kalshi runner (#347, `infra/runner/README.md`), the live check from it, a review of the demo run, the deposit and its rate.
- **Open, [priority]:** #271, the real-money tracking issue; #266, the venue.
- **Open, next:** #269 (real money, after the venue); #253 and #254 (diagnostics); #260 (a challenger, with its own ADR).
- **Parked for the next model version:** #125 and #134 (P6, label `next-model-version`).
- **Open, later:** phase 5's #14, #67 (in April 2027) and #121 (step 3 on 2026-11-05); #15 and #250 (P6).

## Keep an eye on

- **Missed pre-game polls can't be redone.** If the timer stops, a late poll loses that game's data. Under v2 the midday decision reads the polls taken before its snapshot. Without them, it falls back to the goalie-start mix for that day, as v1 did, and the run bundle records that no call was read.
- **In-sample training seasons:** the training seasons are in-sample for xG's rebound term (ADR 0010) and for every tuned setting (ADR 0011). They may train later models, but no result may present them as out-of-sample.
- **Closing keywords:** a closing keyword followed by another issue's number, anywhere in a PR description, closes that issue on merge. That includes pasted plans.
- **Replaying live dates (#109):** a replay leaves a date alone, with a warning, when its cached schedule lists a game there that isn't final. To rebuild such a date, replay the nightly's own window.
- **Writing feature tables to R2:** run each command with `--r2` from a clean checkout of main, so the rows' `artifact_version` names the merged commit and doesn't end in `-dirty`. Later commands read the tables of earlier ones, so keep this order:
  1. `nhl xg`
  2. `nhl stints`
  3. `nhl team-strength`
  4. `nhl goalie-start`
  5. `nhl lineups` (it copies the goalie-start probabilities, and reads stints for ice time)
  6. `nhl goalie-effect`
  7. `nhl schedule-terms`
  8. `nhl player-seasons` (no fitted model, so it can run any time before `nhl rapm`)
  9. `nhl rapm`
  10. `nhl power-plays`
  11. `nhl finishing`
- **GitHub from a cloud session:** `gh api` works through the session's proxy, but other `gh` commands don't. The GitHub MCP tools work too.
  - The proxy refuses branch deletes. GitHub deletes a PR's branch when it merges (on since 2026-10-22), and the owner deleted the older merged branches that day.
- **Codex:**
  - It answers as a review with findings, a 👍 reaction, or a comment saying it found no major issues. Check all three.
  - It doesn't always start on its own. If nothing has arrived about 15 minutes after a PR opens, comment `@codex review`. A "Something went wrong" reply is an error, not a review round: ask again.
  - Reply to every finding with "Fixed in <sha>", "Follow-up #n" or "Won't fix: <reason>". A won't-fix on a P0 needs the owner first.
- **A frozen policy's code:** a PR that changes what the decision reads or computes changes the frozen policy, and only a new version may do that. Docs, audit, dashboard and ingest fixes can merge any time. A change meant to start with the next day's decision merges only once that day's 12:45 ET decision is published: check the day's ledger, not the clock. 12:45 ET is 16:45 UTC in daylight time and 17:45 UTC in standard time, from 2026-11-01.
- **The NHL's empty shift charts** (#237): a week after their games, the NHL served `{"data": [], "total": 0}` for 2026020006 to 2026020052 and full charts for the rest. The tables' first copies are complete. `nhl recheck` never takes an empty chart as a copy.
- **The cloud session's Bash guard:** it blocks commands that combine `data/raw/` or `tests/golden/` with a write, any `rm -rf`, and any command whose text contains `.env`. That includes jq paths such as `.environment`. The Write tool can't write `.env` files. Split such a command, put the text in a file and run the file, or read through the project's own code.
- **The GitHub API from a cloud session:** GraphQL is unavailable and the search endpoints return 403. Use repo-scoped REST endpoints through `gh api`, and the GitHub MCP tools for review threads (replying and resolving).
- **Waiting on Codex and CI:** never sleep in the foreground. Run a background poll that checks Codex's three answers on the PR, and the check runs on the head commit. The time it checks from must be earlier than the PR's creation.
- **Types:** `uv run pyright` covers the tests too, and is clean since #242. CI checks only `src`.
- **The PR script's closing-keyword check:** the scratchpad's `mkpr` scripts refuse any line after the first with a closing word within 3 characters of `#n`. Even "move to the close (#251)" trips it, so word such lines differently.
- **Anything that feeds a real-money decision must be point in time at its own clock,** never only at the game's:
  - settlements count only once `result_utc <= now`;
  - expected decision days come from the games, not from the ledgers that exist;
  - a bundle counts only up to the report's date.

  Codex holds such code to hard rule 1 and to hard rule 7 (no pass on a point estimate). Expect P0s there.
- **Lake tables aren't all point in time by `observed_utc`:**
  - `schedule` holds only games that went on to be played;
  - `odds_snapshots` carries `is_closing_proxy`, `game_id` and `game_type`, all set afterwards.

  Read them through `schedule_known_at` and a column drop, as `challengers.READERS` does.
- **Reference upkeep in 2026-27:** at a coaching change, end the old stint in `coaches.csv` and add the new one. Add a new venue name to `venues.csv`, and the building to `arenas.csv` if it is new (docs/data-sources.md). `nhl audit reference` flags both.
- **#67 (P5):** `EXPECTED_GAMES` has no 2026-27 entry. Add it only once the regular season ends, around 2027-04-17: every live feature build's input check compares each season up to its own with its count, so a mid-season entry would refuse them all. The July 2027 player-seasons refresh needs it.

## Continuing from a terminal

1. `git pull` on `main`, then `uv sync`.
2. `uv run nhl status` compares this machine with R2. To catch up from nothing:
   1. `uv run nhl lake restore-raw` (about 0.5 GB)
   2. `uv run nhl ingest --seasons 20102011-20252026 --replay`
   3. `uv run nhl odds replay`, `uv run nhl odds sbr --replay` and `uv run nhl goalies replay`
   4. the feature commands in the order above
   5. for the live season, the ingest window and replays that `uv run nhl status` prints

   None of them calls a paid endpoint.
3. `uv run nhl backtest` reruns the backtest, and `uv run nhl backtest --hockey-only --seasons 20232024,20242025` the hockey validation. Without `--seasons`, the hockey-only mode scores the development seasons. Commit code first: the run's version ends in `-dirty` when `src/`, `pyproject.toml` or `uv.lock` has uncommitted changes. Never include 2025-26, 2022-23 or live games unless the owner says so: 2025-26's and 2022-23's one runs are spent.
4. **Tuning:** `nhl team-strength`, `nhl goalie-effect`, `nhl schedule-terms` and `nhl rapm` take `--tune`. They and `nhl tune-b2` rerun their grids on the training seasons only, and log them to `reports/tuning/`. Frozen settings change only through a new ADR.
5. CLAUDE.md's workflow (a branch per issue, the Codex review budget, merging when ready) applies as before.
