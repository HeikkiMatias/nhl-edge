# The Kalshi runner

A GitHub Actions runner on the owner's own machine in Finland, for the jobs that trade on Kalshi
(#347). Kalshi checks where each trading request comes from. On 2026-10-26 it refused both demo
orders with 403, because that day's GitHub-hosted runner was in Washington state, where Kalshi
doesn't allow sports trading. GitHub's runners are US cloud machines in several regions, so any
day's order, bid change or cancel could be refused. From the owner's machine, the orders come
from where the account holder is, every day.

Sending the requests through some other network to get past the check is not an option: the
runner is the owner's own machine, in the owner's own country.

| Runs on the Kalshi runner | Why |
| --- | --- |
| `odds-snapshots.yml`, its `orders` job | Places the midday orders and tracks them at every slot (`nhl real place`, `nhl real orders`) |
| `odds-snapshots.yml`, its `revalue` job | Lowers or cancels the resting bids with the slot's snapshot (`nhl real revalue --slot`) |
| `kalshi-revalue.yml` | The same every 15 minutes from 13:05 to 22:50 ET |
| `kalshi-cancel.yml` | The kill switch, with GitHub's runner as a choice when the machine is down |
| `kalshi-check.yml` | Proves the key and the order path from where the orders go |

Everything else stays on GitHub's runners: the odds snapshots, the decision and the alert, the
Kalshi watch and books (public read endpoints only), and the nightly. The nightly reads Kalshi's
orders and settlements only after every order has expired, 5 minutes before its game.

The repository variable `KALSHI_RUNNER` chooses the runner. Set to `self-hosted`, those jobs run
on the runner with the label `kalshi`. Unset or anything else, they run on GitHub's runners as
before. So the switch needs no commit, either way.

## The machine

- **A small server in Finland,** recommended: for example Hetzner Cloud's Helsinki location or
  UpCloud's Helsinki zone.
  - Ubuntu 24.04, x86 (x64);
  - 2 vCPUs, 4 GB of memory and 40 GB of disk: a job installs about 1 GB of Python packages and
    fits nothing;
  - a few euros a month.
- **Or the owner's own always-on Linux computer,** under a user account of its own. It must be
  up whenever orders rest: on game days from about 19:30 to 06:00 Finnish time, and for the kill
  switch at any time.
- **The machine is the owner's alone,** with nothing else of value on it. While a job runs, it
  holds the Kalshi and R2 keys, and the day's private order records sit on its disk until the
  next job's checkout cleans them.
- **If Kalshi refuses the server,** for example because its location check treats a data centre's
  address as unknown, the demo check in step 9 shows it with a 403. The owner's own computer at
  home is then the way, with the same steps.

## Setup

About 20 minutes.

1. **Create the server** with your SSH key, so no password logs in. Note its IP address.
2. **Prepare it,** as root (`ssh root@<the IP>`):

   ```sh
   apt-get update && apt-get -y upgrade
   apt-get install -y git curl ca-certificates unattended-upgrades
   adduser --disabled-password --gecos "" runner
   ufw allow OpenSSH && ufw --force enable
   ```

   The runner only connects out, so no port opens but SSH. Ubuntu's unattended-upgrades installs
   the security updates.
3. **Check where it is:** `curl -s https://ipinfo.io/country` should print `FI`. That's a lookup
   of the address the world sees, as Kalshi's check is.
4. **Register the runner.** On GitHub, open the repository's Settings, then Actions, then Runners,
   then New self-hosted runner, and choose Linux and x64. The page shows the commands with a token
   valid for an hour.
   - Become the runner user (`su - runner`).
   - Run the page's Download commands. They create `actions-runner` and check the download's
     SHA-256.
   - Run its Configure command with this added at the end:

     ```sh
     --name fi-kalshi --labels kalshi --unattended
     ```

   - Don't run `./run.sh`, as the page goes on to say: the service below starts the runner.
   - Leave the runner user with `exit`.
5. **Start it as a service,** as root, so it starts again at every boot:

   ```sh
   cd /home/runner/actions-runner
   ./bin/installdependencies.sh
   ./svc.sh install runner
   ./svc.sh start
   ```

6. **Check it on GitHub:** Settings, Actions, Runners lists `fi-kalshi` as Idle, with the labels
   `self-hosted`, `Linux`, `X64` and `kalshi`.
7. **Check the fork setting:** Settings, Actions, General, "Approval for running fork pull request
   workflows from contributors" is "Require approval for all external contributors"
   (`docs/security.md`, item 4). Never approve a fork's run without reading its changes to
   `.github/workflows/`: an approved run could name the runner's label.
8. **Switch the jobs over:** Settings, Secrets and variables, Actions, Variables tab, New
   repository variable `KALSHI_RUNNER` with the value `self-hosted`.
9. **Prove it:** Actions, then `kalshi-check`, then Run workflow with `env` demo.
   - The run's "Set up job" step names `fi-kalshi` as its runner, and the check passes.
   - The next midday's demo orders go from the runner, and Claude reviews their records.
   - Before the first real bet, run the same check with `env` live.

## When the machine is down

- **What shows it:** GitHub sends no alert. On a day with bets, the alert comes at midday but no
  "orders placed" message follows, and the Actions tab shows the trading jobs as Queued, waiting
  for a runner.
- **Bring it back:** `ssh root@<the IP>`, then `/home/runner/actions-runner/svc.sh status`, or
  reboot it from the provider's console.
  - Queued jobs run when it is back, up to 24 hours later.
  - An order's own checks still skip one too close to its game.
- **Cancel at once:** Actions, then `kalshi-cancel`, then Run workflow with `runner` github. That
  runs on GitHub's machines, which Kalshi may refuse from some US states. Kalshi's own site
  cancels by hand too.
- **Go back to GitHub's runners for a while:** delete the variable `KALSHI_RUNNER`. Then cancel the
  runs still queued for the machine, since their runner was chosen when they started.

## Upkeep

- **The runner updates itself.** Reboot the server now and then for the kernel's updates
  (`reboot`). The service starts again with it.
- **Disk:** each job checks out the repository afresh and installs its packages. `df -h` should
  stay well below full.
- **To remove it:** Settings, Actions, Runners, the runner, then Remove, which shows the commands:
  `./svc.sh stop` and `./svc.sh uninstall` as root, then `./config.sh remove --token <token>` as
  the runner user.
