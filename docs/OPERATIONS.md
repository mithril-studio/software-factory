# Operations

How to run the Software Factory: deploy it, build goldens, connect repos, configure it.
What it is and how it is designed live in the [README](../README.md) and
[architecture.md](architecture.md).

Skills the agent uses live in a separate repo,
[mithril-studio/agent-skills](https://github.com/mithril-studio/agent-skills). They ship
**into the VM**, not onto the server — the agent's code, not the factory's — so they are
installed when a golden is built rather than deployed with the control plane.

The skill that *fills* this repo's queue lives there too: **`factory-compose`** turns a brief
into the ordered backlog the factory builds. It is the one skill that runs outside a VM —
on a laptop, or on the `planner` box over SSH — so it is installed like any other skill
rather than shipped with a golden. It is not vendored here; there is one copy, in
agent-skills, because two would drift.

## Quickstart

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env   # set BOXD_API_KEY and FACTORY_AUTH_EMAIL/PASSWORD;
                       # leave FACTORY_REPOS empty — connect repos in the UI
npm --prefix web install && npm --prefix web run build   # build the UI once
.venv/bin/uvicorn control.app:app --port 8765 --reload
```

Then open <http://localhost:8765> and log in with the credentials you set — auth ships with
no defaults, so an unset `FACTORY_AUTH_EMAIL`/`FACTORY_AUTH_PASSWORD` means nobody can log
in. `GITHUB_TOKEN` is optional locally — it falls back to `gh auth token`.

For UI work, run the Vite dev server alongside uvicorn so the SPA hot-reloads (it proxies
`/api` back to :8765):

```bash
npm --prefix web run dev        # then open the URL Vite prints (:5173)
```

## Deploying

The control plane lives on a **boxd** machine, `mithril-factory` — a plain checkout under
`~/software-factory` running uvicorn under systemd, published by boxd's own proxy with TLS at
<https://mithril-factory.boxd.sh>. It is the same platform every run VM comes from, reached
over the same SDK with its own `BOXD_API_KEY`.

It has been here before. On 2026-08-19 the first boxd control plane lost its root filesystem
and the run history with it; the factory moved to a Hetzner box, which stopped answering on
2026-11-02 (up according to Hetzner, unreachable on every port after a reboot), and it moved
back. What is different this time is what the first machine lacked: daily disk backups
(`backups.period 24h`) and a checkpoint before every deploy. The external health monitor
(below) is what notices the next time.

```bash
scripts/deploy.sh                 # checkpoint, fast-forward to origin/main, rebuild what moved, restart
scripts/deploy.sh --ref branch    # deploy a branch, tag or sha instead
boxd machine checkpoint restore mithril-factory pre-deploy-<sha>   # roll back
```

| | |
|---|---|
| Machine | `mithril-factory` (2 vCPU / 8 GB), never suspends or hibernates, bot protection off |
| Service | `factory.service` — `boxd machine exec mithril-factory -- 'systemctl status factory'`, logs in `var/uvicorn.log` |
| URL | `https://mithril-factory.boxd.sh` (boxd proxy → port 8765) |
| Backups | boxd disk backups every 24h; checkpoint `provisioned` and the last three `pre-deploy-*` |
| Checkout | read-only deploy key on the machine; the control plane's `GITHUB_TOKEN` is not used for it |

The machine builds itself with `scripts/provision-boxd.sh`, from a laptop with the boxd CLI
and a `gh` that may add deploy keys, and can again. Re-running it is safe. It mints the
machine's own `BOXD_API_KEY` (that needs an interactive `boxd login`; an API-key session may
not mint keys, and the script says what to run instead) and generates the UI login, and it
writes no GitHub or agent credential. Those are handed over with `scripts/factory-secrets.sh`,
which prompts for them hidden and moves them straight into the machine's `.env` — or set
them with `boxd env set --secret` scoped to the machine: boxd injects those into exec sessions
but not into systemd, so `scripts/deploy.sh` copies `CLAUDE_CODE_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY` and `GITHUB_TOKEN` from them into `.env` on every deploy, and a token
rotated with `boxd env set` reaches the service with the next deploy. Secrets live in that
`.env` and in boxd's env store, and in no other place — not in this repo, not in CI.

The boxd machine name doubles as its `.boxd.sh` subdomain and is unique across boxd, which is
why it is not `software-factory` — that name belongs to somebody else.

Schema changes apply themselves on boot (`db.init()` and `telemetry.store.init()` are both
idempotent), so there is no migration step. To load telemetry for runs that finished before
that layer existed:

```bash
.venv/bin/python -m telemetry.backfill      # replays every salvaged transcript
```

An external monitor, `scripts/factory-health.sh`, runs from a
GitHub Actions cron every 10 minutes and opens (and closes) an alert issue when `/healthz`
stops answering.

## What a run does

1. The poller finds the lowest open issue labelled `agent:queued` on a watched repo with no
   run in flight. With `FACTORY_SEQUENTIAL=1` it also waits while another factory PR into the
   target branch is still open.
2. The run takes `agent:queued` off the issue (the claim — if that fails, the run stops before
   it spends a VM), labels it `agent:running`, and comments where the PR will go.
3. It makes sure the target branch exists (`FACTORY_TARGET_BRANCH`, default `test`), creating
   it from the default branch if not.
4. It restores a VM from the golden snapshot (~0.2s), clones the repo (or reuses a warm
   golden's checkout), and checks out `factory/issue-<n>`:
   - an **open PR** on that branch is continued — the agent reads its comments first;
   - a branch whose PR was **closed or merged** is deleted and started fresh from the target;
   - a branch with **no PR at all** (an earlier run that died) is continued.
5. It runs the agent — `factory-agent`, the launch line the golden carries — with the issue as
   the prompt, streaming the log live and feeding telemetry.
6. It finds the open PR, salvages the transcript, and destroys the VM, on every path.
7. It labels the issue `agent:done` or `agent:failed` and comments with the PR, the cost, and
   whether the run passed the soft budget.

Nothing merges. You review the PR into `test`, merge it, and the next queued issue starts.

## Labels, retries, restarts

Four labels: `agent:queued`, `agent:running`, `agent:done`, `agent:failed`. Adding
`agent:queued` is how work enters; the factory writes the rest. Nothing halts a repo: a
failed issue is labelled and the queue moves on.

- **Retry or iterate:** re-add `agent:queued`. The next run continues the branch, and if its
  PR is open, it addresses the PR's comments. That is also how you give feedback.
- **Restart:** a run interrupted by a deploy or crash is closed on boot and its issue goes back
  to `agent:queued` automatically, continuing from whatever was pushed.
- **Budget:** a run past `FACTORY_RUN_BUDGET_EUR` (default €100) is flagged in the UI and the
  issue comment. It is not stopped.
- **Post-mortem:** `FACTORY_KEEP_FAILED=1` keeps a failed run's VM around to SSH into.

## The golden VM

Every run boots from a **golden snapshot** — never from a machine it shares with another run,
and never from something a person configured by hand and hoped to remember.

### Two tiers, and which one a run picks

The name is the whole registry. There is no list of goldens anywhere else:

| Snapshot | What it is |
|---|---|
| `golden-copy` | The **base image**. Tooling, skills, an agent login, warm toolchain caches — and no repo. Serves every repo. |
| `golden-<owner-repo>` | The **warm tier**: the same image with one repo already cloned at `$HOME/work/<name>` and installed. |
| `golden-tester` | `golden-copy` plus Playwright and Chromium, for a browser harness. Never resolved for a run in v1. |

A run resolves in that order, most specific first (`agents.resolve_snapshot`):

1. `golden-<owner-repo>` for the repo it was assigned, if that snapshot exists;
2. otherwise `golden-copy`;
3. otherwise nothing — the dispatch fails rather than borrowing somebody else's golden, and
   `preflight` says so before you spend a run finding out.

`<owner-repo>` is the repo slugged: lowercase, every run of non-alphanumerics collapsed to one
hyphen. The owner half is kept, so two owners with a repo of the same name cannot collide —
and because `owner/name` always contains a `/`, every slug carries a hyphen and no repo can
ever produce the bare name `copy`.

**The agent is not in the name.** It used to be (`golden-<agent>--<owner-repo>`), which made
listing the snapshots the way to discover the agents — elegant, and backwards for a platform
that watches many repos and runs one agent. Which agent an image launches is now something the
image says about itself: every golden carries `/etc/factory/agent.json` and announces it into
the run log, which is recorded on the run. That registry cannot drift from what actually
booted. Running a *second* agent needs a second base image and a way to choose between them —
on the backlog, deliberately, since nothing needs it yet.

**The warm tier is only a speed-up.** Every run installs the repo for itself either way — the
prompt says so, and says nothing about dependencies being present. So a repo with no warm
snapshot is not broken, and a warm snapshot that has gone stale costs minutes, not
correctness. That is what lets a repo be connected and dispatched in the same minute.

### Building the base: `scripts/build-golden.sh`

```bash
scripts/build-golden.sh
```

Creates a machine from the `factory-base` snapshot, installs that agent's CLI, the skills from
[agent-skills](https://github.com/mithril-studio/agent-skills), `fnm` and `jq`, warms the
package-manager download caches (node versions, pnpm/yarn, CPython — toolchains, never a
project's packages), writes the two files a golden owes the control plane
(`/usr/local/bin/factory-agent` and `/etc/factory/agent.json`, see `../control/README.md` §2.3),
runs its checks while the machine is still alive, saves `golden-copy`, and destroys the
machine.

It stops once, in the middle, for the one step a script cannot do: **the agent's login is a
browser OAuth.** It prints how to connect, waits, and only then checks and saves — the
credential has to be on disk inside the machine before the snapshot captures it. That
credential expires, which is what re-running this script is for. `--no-login` skips the pause
for a deployment that sets `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` instead.

An agent the script has never seen needs `--cli-install`, `--launch` and `--manifest`, and
`--name` to keep it off `golden-copy`. It refuses rather than guessing: a wrong launch line
produces a golden that looks built and dies at the first dispatch.

### Refreshing a warm one by hand: `scripts/refresh-golden.sh <owner/repo>`

```bash
scripts/refresh-golden.sh mithril-studio/software-factory
```

A snapshot cannot be edited in place, so this is restore, update, re-save, destroy: create a
machine from `golden-<slug>`, **refuse if the working tree is dirty**, fetch and hard
reset to the base branch, run the project's setup command, re-save under the same name (boxd
captures a new version; the old one stays forkable, so a run dispatched mid-refresh keeps
working), destroy the machine.

The setup command is never guessed. It is `--setup`, or the command in the repo's own
`## Setup` section (below). If neither says, the script stops.

### What a golden must carry

- `git`, `gh`, `jq`, `fnm` and warm toolchain caches — but *not* a repo, except on the warm
  tier. Each run clones the repo it was assigned into `$HOME/work/<name>` (`FACTORY_WORKDIR`
  moves that), and reuses a checkout already there when it is the right repo
- `/usr/local/bin/factory-agent` and `/etc/factory/agent.json` — the launch contract, see
  `../control/README.md` §2.3
- the agent authenticated (inherited by every restore; expires)
- `gh` authenticated, as the fallback for a deployment that sets no `GITHUB_TOKEN`
- skills installed from [agent-skills](https://github.com/mithril-studio/agent-skills)

Never `boxd machine share` a golden: sharing deletes the in-VM agent credentials.

### Knowing which goldens work

What kills a golden is credential expiry, and the only real test of a credential is using it.
A run that produced usage authenticated, so the Runs page is the evidence: a golden whose runs
fail at once with an auth error needs re-authenticating on a fork and re-snapshotting under the
same name. The Goldens page lists what the fleet holds and whether each one has a ready version.

## Adding a repo

1. Make sure the base image exists — a `golden-copy` snapshot, built by
   `scripts/build-golden.sh` (above). A repo does not need a golden of its own; `golden-copy`
   serves every repo that has none. A warm `golden-<owner-repo>` is optional and only makes
   runs faster.
2. Connect it — **Projects → Connect repo**, or `POST /api/repos {"repo": "owner/name"}`.
   The field is a picker over the repos the deployment's token can see (`GET
   /api/github/repos`), and it is still free text: a repo created a minute ago, or any repo at
   all when GitHub is unreachable, is connected by typing its slug. Check shows what preflight
   says, Connect commits it; a blocking check refuses the connection and says which one. On
   success the repo is in the register, its lifecycle labels exist, and the poller picks it up
   on its next tick — **no restart and no `.env` edit.** Disconnecting keeps the repo's runs.

   `FACTORY_REPOS` is the *seed* for that register: entries are added on boot if they are
   not already there, and removing one does not unwatch the repo.
3. Optionally point its pull requests somewhere other than `FACTORY_TARGET_BRANCH` —
   **Projects → PRs go into**, or `PATCH /api/repos/{owner}/{name} {"base_branch": "develop"}`.
   The first run creates the branch from the default branch if it does not exist. Because
   GitHub only honours `Closes #n` on the default branch, issues stay open (labelled
   `agent:done`) until the work reaches it.
4. Give the repo a `.factory.md` (below).
5. Ask, before trusting any of it:

   ```bash
   .venv/bin/python -m control.preflight mithril-studio/legal-ai-app
   ```

   It reports on the repo — readable, pushable, target branch, has a `.factory.md`, has a `##
   Setup` section, labels — and on one thing outside it: whether a golden snapshot exists
   for this repo to resolve onto. Exit status 0 means ready. Same answers over HTTP at
   `/api/preflight?repo=owner/repo`, which is what the Check button calls.

   It boots nothing. Every check is a GitHub call or a snapshot listing, which is the whole
   point: these are the questions a run otherwise answers the expensive way, after forking a
   VM and spending forty minutes to find out the token cannot push.

## What a watched repo tells the factory: `.factory.md`

A repo can describe itself to the agent in a `.factory.md` at its root. The control plane
reads it from the target branch at dispatch and splices it into the prompt,
so it is the one place that says how *this* project is set up and how it is verified:

```markdown
## Setup

`cd app && npm ci` — about 90 seconds. Run it once, before anything else.

## Verify

- Everything runs from `app/`: `npm run lint`, `npm run typecheck`, `npm test`.
- `app/.env.local` is already there. Do not print it, do not regenerate it.
- There is no local database — Supabase is remote. Do not try to start one.
```

### `## Setup`

The single highest-value line in the file, and now the one the control plane reads too. The
base golden carries toolchains and a warm package-manager download cache and nothing installed
for your repo, so the prompt sends the agent to run this command once, in the foreground,
before it touches any code — the difference between one install and a run that spends turns
discovering it needs one, guessing a command, and re-reading its whole context between each
guess.

It is also what `scripts/refresh-golden.sh` runs when it bakes this repo's warm golden, so a
repo that names one can get that install done once into a snapshot instead of once per run.

Name the command, and say roughly how long it takes so the agent picks a sane timeout. Put it
above the verify commands: it runs first, and a file is read in the order it is written.

Without it the agent still gets there — the default tells it to find the lock file and run
that package manager's frozen install — but it gets there by inference, and inference is the
expensive half.

It lives in the repo rather than in this one because it describes that repo, and because
editing it is then a pull request there rather than a control-plane deploy. A repo without
one gets a deliberately vague default that asserts nothing about this project in particular —
a wrong fact costs more than a missing one, since the agent acts on it before it can find out.

`preflight` reports a repo with no `.factory.md`, and a `.factory.md` with no `## Setup`
section, as warnings. Neither blocks a run; both cost it turns.

Keep harness invariants out of it. Anything that would hang or corrupt *any* run (do not
background long commands, commit and push as you go) belongs in the prompt, not here; see
`../discussion.md` §"Where rules live".

## Configuration

The variables an operator actually decides about. Every variable is annotated in
`../.env.example`; defaults live in `control/config.py`.

| Variable | |
|---|---|
| `BOXD_API_KEY` | **Required.** Run VMs come from boxd. |
| `FACTORY_AUTH_EMAIL` / `FACTORY_AUTH_PASSWORD` | **Required.** UI login. Unset means nobody can log in. |
| `GITHUB_TOKEN` | Required on a server (`repo` + `workflow` scopes); falls back to `gh auth token` locally. |
| `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` | Durable agent auth; set one for unattended operation. |
| `FACTORY_TARGET_BRANCH` | Where pull requests go (`test`). Created on first use. |
| `FACTORY_SEQUENTIAL` | Wait for the factory's open PR into the target to be merged or closed before the next issue (on). |
| `FACTORY_RUN_BUDGET_EUR` | Soft per-run budget (€100): flagged, not stopped. `FACTORY_USD_PER_EUR` converts. |
| `FACTORY_VM_PREFIX` | Prefix of every VM this control plane creates and the only ones it reaps (`job-`). |
| `FACTORY_MAX_CONCURRENT` | Concurrent runs across all repos (3). Always one per repo. |
| `FACTORY_RUN_TIMEOUT` | Hard ceiling per run (5400s). |
| `FACTORY_KEEP_FAILED` | Keep a failed run's VM for a post-mortem. |
| `FACTORY_LOG_LEVEL` | Control-plane log verbosity. |

## API surface

All under `/api`, all behind the session auth gate except `/api/login` and `/healthz`.
State-changing requests from another site (another `*.boxd.sh` machine included) are refused.
The exhaustive list is `control/app.py`.

- **Auth** — `POST /api/login`, `POST /api/logout`, `GET /api/me`
- **Runs** — `POST /api/runs {"repo", "issue_number"}` starts one (409 if the repo is busy).
  `GET /api/runs`, `GET /api/runs/{id}`, `POST .../cancel`, `GET .../stream` (SSE, resumes on
  reconnect), `GET .../telemetry`.
- **Queue** — `GET /api/plan?repo=` (open issues with their factory state; 502 when GitHub
  cannot be read), `POST /api/plan/queue {"repo", "number"}` (labels the issue
  `agent:queued`), `GET /api/issues`, `GET /api/config`
- **Telemetry** — `GET /api/telemetry`, `GET /api/digest?repo=&days=`
- **Repos** — `POST`/`DELETE /api/repos`, `PATCH /api/repos/{owner}/{name}` (`base_branch`:
  where its PRs go), `GET /api/projects`, `GET /api/github/repos`, `GET /api/preflight?repo=`
- **Fleet** — `GET /api/goldens`, `GET /api/machines`, `POST /api/reconcile`
- **Health** — `GET /healthz`, unauthenticated, for the external monitor

## Scripts

| Script | |
|---|---|
| `verify.sh` | Every Python CI gate, in CI's order — ruff, all tests, the memory validator. CI runs this exact file, plus a separate `web` build job. |
| `deploy.sh` | Checkpoint the boxd machine, fast-forward, rebuild what moved, restart. `--ref` deploys a branch or tag. |
| `build-golden.sh` | Build the `golden-copy` base image (above). |
| `refresh-golden.sh` | Refresh a repo's warm golden in place (above). |
| `factory-health.sh` | External monitor; runs from a GitHub Actions cron every 10 minutes and manages an alert issue. |
| `provision-boxd.sh` | Build the control-plane machine from nothing: deploy key, checkout, build, `.env`, a minted boxd key, systemd, the proxy, backups. |
| `factory-secrets.sh` | Hand the control plane its GitHub and agent credentials, hidden, straight into its `.env`. |
| `planner-setup.sh` | Provision the `planner` box that runs `factory-compose`. |

## The UI

A React + Tailwind + shadcn single-page app in `web/`, built to `web/dist` and served by
FastAPI — one deployable, at the cost of a `vite build` step. It reads the JSON API under
`/api`; the control plane holds no HTML.

**Visual language.** Brutalist-editorial, and deliberate — agents edit this UI, so the rules
are written down rather than inferred from the diff. Every token lives in `web/src/index.css`;
change them there, not in a component.

- **Type is mixed by job.** Instrument Serif for headings and display figures, JetBrains Mono
  for every label and every datum, Inter for prose. A number the reader compares is mono; a
  number that *is* the headline is serif.
- **No border radius.** `--radius: 0px`, and the `radius-*` scale is pinned to zero so a
  stray `rounded-lg` cannot reintroduce a soft corner.
- **Hard shadows, never blur.** `shadow-hard` / `-sm` / `-lg` / `-accent` are solid offsets.
  The accent (terracotta) shadow marks the one thing on a screen worth acting on — the lead
  stat tile, the sign-in card — and nothing else.
- **Two border weights.** Black `border-border` frames anything that is its own object;
  grey `border-subtle` divides rows inside one.
- **Warm neutrals.** Paper, not screen: the ground is a warm off-white, the sidebar a slab of
  ink in both themes. The app ships light; dark inverts the ground and keeps the logic.
- **Hover is tactile.** `hard-lift` shifts an object -2px and deepens its shadow by the same
  amount; press seats it flat. Only put it on things that are actually clickable.
- **Labels are uppercase mono.** The `eyebrow` utility, used for column heads, field labels,
  section heads and page kickers — one voice for everything that names a thing.
- **Optical padding.** `p-optical` / `p-optical-lg` carry more top and left than bottom and
  right, because equal padding reads bottom-heavy once type sits in it.

- **Runs** — every run: state, issue, duration, tokens, cost, over-budget flag, PR link.
- **Run detail** — live agent output over SSE, the run's telemetry (model calls, cache, derived
  cost, unpriced calls), and a cancel button.
- **Queue** — open issues across watched repos with their factory state, lowest number first.
- **Projects** — watched repos with run tallies, the golden each one boots, and where its PRs
  go. Connect a repo by naming it and reading what preflight says.
- **Goldens** — the golden snapshots the fleet holds, and the boxd machines running now, with
  orphans flagged and a reconcile button.
- **Telemetry** — cost by token class, spend by day, tools by wall time, cost per shipped issue.
- **Digest** — what shipped, how runs failed, and what it cost over a window.

## Docs

- [architecture.md](architecture.md) — what v1 is, the contracts, and what comes next
- [../control/README.md](../control/README.md) — control plane design notes
- [../telemetry/README.md](../telemetry/README.md) — trace layer design notes
- [agent-skills](https://github.com/mithril-studio/agent-skills) — the skills the in-VM agent loads
