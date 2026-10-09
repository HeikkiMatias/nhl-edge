# Dashboard

The paper-trading dashboard (#168, docs/plans/phase-5.md task 8). It is a Next.js app that runs in the browser and reads Supabase. It shows:

- **Four headline tiles:** CLV per bet, which is the measure, then the paper bankroll with the stakes in play, the record (won–lost) and the drawdown from the peak.
- **Results and the bankroll** (ADR 0034):
  - a note that over this few bets results are mostly luck;
  - the bankroll after each settled day, with its table;
  - the bets per day;
  - each bet's expected return at the decision against its CLV at the close.
- **Live scores** (#211) for the slate's games:
  - each game's score, period and time left;
  - the paper bet's team in bold;
  - "Provisionally won" or "lost" from the final score, until the nightly run settles the bet officially.

  The scores come from the NHL's public scoreboard through `app/api/scores`. They refresh every 30 seconds while a game is on, and are display only (docs/data-sources.md).
- **The slate:** the latest game day's decisions, with B1, the blend, their gap (above 8 points it goes to hand review), and each bet's side, price and stake.
- **The paper bets:** all of them, newest first, with the result (won, lost, void or awaiting), the final score and how the game ended (regulation, OT or a shootout), the profit, the closing proxy status, CLV and the fair move.
- **Cumulative CLV per bet:** each nightly report's CLV per bet to its date under the current policy, with its weekly block bootstrap interval, as a chart and a table.
- **The latest live report** (`nhl live report`), with coverage, CLV with the floor and bound, the model comparisons, the blend's calibration band, gaps for hand review, and the operational alerts.

**Explanations.** Each section has a "What these mean" panel, and a glossary closes the page. Every term is written for a reader with no betting or statistics background, in one place (`lib/glossary.ts`), so the panels and the glossary never disagree.

**Where the figures come from.**
- Every evaluation figure is the report's own, and the dashboard gives no verdict before the formal review (ADR 0032).
- It computes only sums of the settlement's own columns: the bankroll from 100 units plus the settled bets' `profit`, its running peak and drawdown, the record and the bets per day (`lib/results.ts`).
- The owner chose on 2026-10-09 to show results during the season (#210, ADR 0034, which amends ADR 0032 and plan §11). CLV stays the measure. Nothing in the frozen policy changes because of results, and the 20% drawdown line calls for a review of the data and code, never of the model.

## Security

- **The browser holds only the public anon key.** The service role key stays in the GitHub workflows' secrets.
  - The app's one server route, `app/api/scores`, relays the NHL's public scoreboard. It holds no secret and reads no Supabase data.
  - It serves only dates within two days of today, and Vercel's CDN caches its answers for 20 seconds.
- **Row-level security decides what a signed-in user can read.** Only the users in `public.dashboard_owners` can read `predictions`, `paper_bets` and `live_reports`, and, from `games`, the scores of the bets' games. Anon reads nothing.
- **Sign-in is by password, or by email link,** and only for users who already exist: sign-ups are off, and the link never creates a user (`shouldCreateUser: false`). Any other signed-in account is told it is not an owner, and is shown its user id.
- **A link that comes back signed out says why,** in Supabase's words from the client's initialization: an expired or used-up link (as when a mail scanner opened it first), or a code it couldn't exchange, as in a browser other than the one that asked for it. A sign-in clears the message.
- The page asks search engines not to index it.

## Environment variables

Both are public: Next.js inlines them into the browser bundle at build time, so a change needs a redeploy.

| Variable | Value |
| --- | --- |
| `NEXT_PUBLIC_SUPABASE_URL` | The project URL: Supabase → Project Settings → API (or Data API). |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | The anon (public) key from the same page, or the publishable key (`sb_publishable_…`). Never the service role or secret key. |

Without them the build still succeeds, and the page says it isn't configured.

## Owner setup

1. **Supabase migrations.** Apply them in order, with `supabase db push` or by pasting each into the SQL editor:
   - `supabase/migrations/20261008120000_paper_ledger.sql` (#167);
   - `supabase/migrations/20261008140000_live_reports.sql`;
   - `supabase/migrations/20261009120000_games_owner_reads.sql` (#210): lets an owner read the final scores. Until it is applied, the page says so and shows the results without scores.
2. **Your user.**
   - Authentication → Users → Add user → Create new user, with your email and a password, and "Auto Confirm User" ticked. You sign in with those.
   - A user made without a password: delete it and add it again with one, then put its new id in `dashboard_owners` (step 2's insert). Or set the password through Supabase Auth's admin API, which hashes it as Auth does, never with SQL on `auth.users`:
     `curl -X PUT "$SUPABASE_URL/auth/v1/admin/users/<user id>" -H "apikey: $SERVICE_ROLE_KEY" -H "Authorization: Bearer $SERVICE_ROLE_KEY" -H "Content-Type: application/json" -d '{"password": "<password>"}'`
   - Authentication → Sign In / Providers: turn off "Allow new users to sign up".
   - In the SQL editor, run `insert into public.dashboard_owners (user_id) values ('<your user id>');`
3. **The repository variable `SUPABASE_LEDGER=true`** (GitHub → Settings → Secrets and variables → Actions → Variables), if not already set for #167. The midday and nightly runs then copy the ledger, and the nightly run writes the live report.
4. **Vercel.**
   - New Project → import `HeikkiMatias/nhl-edge`.
   - Set Root Directory to `web`. The framework is detected as Next.js.
   - Add the two variables above, then deploy.
5. **Supabase → Authentication → URL Configuration:**
   - set the Site URL to the Vercel production URL;
   - add it to the Redirect URLs.

   The email link returns there.

## Development

```sh
npm ci
npm run dev        # http://localhost:3000, with the two variables in web/.env.local
npm run typecheck
npm test           # vitest: lib/ in Node, the page's states in jsdom
npm run build
```

The tests run the real supabase-js client against a stand-in for PostgREST (`lib/testing.ts`) that applies row-level security by token, paging and the policy filter. By file:
- `lib/load.test.ts`: the reads.
- `lib/results.test.ts`: the bankroll and the record.
- `lib/live.test.ts` and `app/api/scores/route.test.ts`: the live scores and their route, on the NHL's recorded scoreboards of 2026-10-08 and 10-09 (`lib/fixtures/score-*.json`).
- `app/dashboard.test.tsx`: the page's states.
  - not configured;
  - the sign-in form: a password, or an email link, and a link that came back signed out;
  - an owner's board, with the results, the bankroll, the charts' keyboard readout and the glossary, and a non-owner's denial;
  - the scores before their migration is applied;
  - live scores during a game, a provisional result after it, a failed scoreboard, and an older slate that asks for none;
  - a failed read;
  - a session that changes, or signs out.

The report fixture (`lib/fixtures/live-report.json`) is `nhl live report`'s output on the Python tests' synthetic season.

CI's `web` workflow (`.github/workflows/web.yml`) runs `npm ci`, the typecheck, the tests and the build without the variables, on every pull request that changes `web/` and on `main` after its merge.
