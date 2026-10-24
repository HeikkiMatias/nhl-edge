# Security of the public repository

The repository is public, and the owner's card pays for Cloudflare (R2) and Kalshi. This file records what an outsider can and can't do, the code's defences, and the settings only the owner can make (#294, checked on 2026-10-19).

## What an outsider can't do

- **Read a secret.**
  - No commit, on any branch, holds a key or token.
  - Run logs are public, but GitHub masks every secret in them, and the real-money steps print counts only.
  - Kalshi's errors print the call and the status, and the Odds API key is never printed or stored.
- **Run a workflow with secrets.**
  - The workflows that hold secrets run only on their schedule or by dispatch, and dispatch needs write access.
  - A pull request from a fork runs only `ci.yml` and `web.yml`, with no secrets and a read-only token.
  - No workflow uses `pull_request_target`, `workflow_run` or comment triggers.
- **Reach the timer.** The Cloudflare Worker (`infra/timer`) has no public URL and runs only on its cron.
- **Reach R2.** No public bucket URL is in the repository. The endpoint is built from the secret account ID.
- **Move money out of Kalshi.** Kalshi's API has no withdrawal, bank or card endpoint for a retail account. Its transfers stay between the account's own subaccounts.

## The code's defences

- **Orders are sized by the code, never by R2 alone** (`live/placement.py`, ADR 0040 and 0045):
  - a live order goes only to the exchange the venue commit names, at its rate;
  - each of its two parts' cost is recomputed from its contracts and limit, at the taker fee for the taker part and the maker fee for the post-only maker part;
  - its most cost, the larger part's, and the day's orders stay within 1.5% and 5% of 200 €. Earlier orders count from their own records, never from the day's rows, and an order goes only as its record was written: a row rewritten since sends nothing. A maker part a rerun sends passes the same per-order checks again, and it gets only what the taker part's fills left of its own planned cost.

  So a rewritten alert in R2 can spend at most 3 € an order and 10 € a day, in USD at the deposit rate.
  An order over the ceiling is refused, not shrunk. If the real bankroll nears 200 €, `CEILING_BANKROLL` is raised in a commit.
- **Pinned actions.** Every third-party action is pinned to a commit SHA, and Dependabot updates them.
- **The dashboard's packages.** Dependabot also watches `web/`'s npm packages weekly (#316). Their minor and patch updates come as one PR, and `package.json` keeps its exact pins. `@types/node` stays at the major of the Node the dashboard runs on, 22 (#335): Dependabot skips its major updates, and a later Node moves `web.yml`, `engines` and `@types/node` together.
- **Read-only tokens.** The workflows' GitHub tokens are read-only (`permissions: contents: read`).
- **The live Kalshi key is restricted to `main`.** Its jobs run under the `kalshi-live` environment, so the key can be held there and kept from workflows on other branches.

## The owner's settings

Done once, in this order. Each can be checked again at any time.

### GitHub

1. **The `kalshi-live` environment.** Settings → Environments → New environment, named `kalshi-live`.
   - Under "Deployment branches and tags", choose "Selected branches and tags" and add the rule `main`.
   - Don't add required reviewers or a wait timer: the scheduled runs would stop.
   - Add the environment secrets `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY`, the live key.
   - Then delete those two from Settings → Secrets and variables → Actions → Repository secrets.
   - The demo key (`KALSHI_DEMO_*`) can stay a repository secret: its funds are mock.
2. **Protect `main`.** Settings → Rules → Rulesets → New branch ruleset.
   - Set it to Active, and target the default branch.
   - Turn on "Restrict deletions" and "Block force pushes".
   - Requiring a pull request is optional.
3. **Secret scanning.** Settings → Advanced Security ("Code security" on older pages): turn on secret scanning ("Secret Protection") and "Push protection". Both are free on public repositories.
   - Optional, on the same page: Dependabot alerts and Dependabot security updates. A critical advisory then opens its PR the same day, not at the next weekly run (#316).
4. **Actions.** Settings → Actions → General.
   - "Workflow permissions": "Read repository contents and packages permissions".
   - "Approval for running fork pull request workflows from contributors": "Require approval for all external contributors" (older pages: "all outside collaborators").
5. **The timer's token.** It should be fine-grained, for this repository only, with Actions read and write. That's what `infra/timer/README.md` asks for.

### Cloudflare

6. **The R2 bucket.** R2 → the bucket → Settings → Public access.
   - The R2.dev subdomain is disabled.
   - No custom domain is connected.
7. **The R2 tokens.** R2 → Manage API tokens.
   - The token in GitHub's secrets is "Object Read & Write" for this bucket only, never Admin.
   - The token on the owner's computer (#169) is "Object Read only".
8. **A billing notification.** Notifications → Add → "Usage Based Billing", to hear of any unexpected R2 use. The nightly `nhl lake size --max-gb 8` already fails a run before R2's free 10 GB.

### Kalshi

9. **Two-factor sign-in** on the account.
10. **Keep only the real bankroll there,** about 100 €. A leaked key can trade, and can lose what is in the account. It can't withdraw.
11. **After any suspected leak:**
    - in Kalshi's account settings, under API keys, delete every key, since a leaked key can create new ones;
    - make a new key;
    - put it in the `kalshi-live` environment;
    - set `KALSHI_TRADING` to `off` meanwhile.
