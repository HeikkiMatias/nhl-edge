# Dashboard

The paper-trading dashboard (#168, docs/plans/phase-5.md task 8). It is a Next.js app that runs in the browser and reads Supabase. It shows:

- **The slate:** the latest game day's decisions, with B1, the blend, their gap (above 8 points it goes to hand review), and each bet's side, price and stake.
- **The paper bets:** newest first, with their settlement, closing proxy status, CLV and fair move.
- **Cumulative CLV per bet:** each nightly report's CLV per bet to its date, with its weekly block bootstrap interval.
- **The latest live report** (`nhl live report`), with coverage, CLV with the floor and bound, the model comparisons, the blend's calibration band, gaps for hand review, and the operational alerts.

Every figure is the report's own. The dashboard computes none and gives no verdict before the formal review (ADR 0032).

It never reads a bet's result or profit. The return and the drawdown's size appear only once the report carries them, from the season's end (plan §11).

## Security

- **The browser holds only the public anon key.** The service role key stays in the GitHub workflows' secrets, and the app has no server code that could hold it.
- **Row-level security decides what a signed-in user can read.** Only the users in `public.dashboard_owners` can read `predictions`, `paper_bets` and `live_reports`. Anon reads nothing.
- **Sign-in is by email link,** and only for users who already exist (`shouldCreateUser: false`). Any other signed-in account is told it is not an owner, and is shown its user id.
- The page asks search engines not to index it.

## Environment variables

Both are public: Next.js inlines them into the browser bundle at build time, so a change needs a redeploy.

| Variable | Value |
| --- | --- |
| `NEXT_PUBLIC_SUPABASE_URL` | The project URL: Supabase → Project Settings → API (or Data API). |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | The anon (public) key from the same page, or the publishable key (`sb_publishable_…`). Never the service role or secret key. |

Without them the build still succeeds, and the page says it isn't configured.

## Owner setup

1. **Supabase migrations.** Apply both, in order, with `supabase db push` or by pasting each into the SQL editor:
   - `supabase/migrations/20261008120000_paper_ledger.sql` (#167);
   - `supabase/migrations/20261008140000_live_reports.sql`.
2. **Your user.**
   - Authentication → Users → Add user, with your email.
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
npm test           # node --test, Node 22.18 or later
npm run build
```

CI's `web` job runs `npm ci`, the typecheck, the tests and the build without the variables.
