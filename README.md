# Software Factory

GitHub issues in, pull requests out.

A control plane watches a repo for work, forks an isolated [boxd](https://boxd.sh) VM per
task, runs a coding agent inside it, and reaps the VM when it's done. You watch it happen
live in a web UI and can start runs yourself.

The control plane is deterministic and contains no LLM. All intelligence lives in the agent,
inside the VM.

> **Status:** V0, in progress. Second attempt — the first failed on scope, see
> `../learnings.md`. The governing constraint of this build is *smallness*.

---

## Layout

One repo, one deployable, one Postgres later. Each layer gets a folder so it can be split
out once it has earned its own release cycle — not before.

| Folder | Layer | State |
|---|---|---|
| `control/` | Control plane: watch issues, fork VMs, dispatch agents, reap, and the UI | **working** |
| `telemetry/` | Trace layer: one row per model call and tool call, priced and joined to outcomes | **working** |
| `docs/` | Architecture and the contracts between layers | — |

The folder is `control/` rather than `exec/` because `exec` is a Python keyword and cannot
be an importable package name.

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
cp .env.example .env               # fill in BOXD_API_KEY and FACTORY_REPOS
npm --prefix web install && npm --prefix web run build   # build the UI once
.venv/bin/uvicorn control.app:app --port 8765 --reload
```

Then open <http://localhost:8765>. `GITHUB_TOKEN` is optional locally — it falls back to
`gh auth token`.

For UI work, run the Vite dev server alongside uvicorn so the SPA hot-reloads (it proxies
`/api` back to :8765):

```bash
npm --prefix web run dev        # then open the URL Vite prints (:5173)
```

## Deploying

The control plane lives on a **Hetzner** VM — a plain checkout under `~/software-factory`
running uvicorn under systemd, not a container, behind Caddy for TLS. It moved off boxd on
2026-08-19, after that VM's root filesystem corrupted and took the run history with it: the
thing the factory forks VMs *from* should not also be the thing that remembers what it did.

boxd is still where every run VM comes from. The Hetzner box reaches it purely over the API,
so it needs `BOXD_API_KEY` and nothing else — but it does not inherit the two things boxd
gave for free, so they are explicit here: TLS is Caddy's, and `.env` is a real file on the
box rather than injected environment.

```bash
scripts/deploy.sh                 # pull the tracked branch, rebuild what moved, restart
scripts/deploy.sh --ref main      # deploy a specific branch or tag
```

| | |
|---|---|
| Host | `factory@46.224.40.20` (Hetzner `cx23`, fsn1), ssh key `~/.ssh/hetzner` |
| Service | `factory.service` — `systemctl status factory`, logs in `var/uvicorn.log` |
| TLS | Caddy, `/etc/caddy/Caddyfile`, cert issued automatically for the configured host |
| Backups | Hetzner daily snapshots, enabled on the server |

systemd, not `nohup`: the old deployment would not have survived a reboot even without the
corruption. Secrets live in `.env` on the box and are in no other place — not in this repo,
not in CI. Rebuilding them means `boxd env` plus a fresh `FACTORY_AUTH_PASSWORD`, which is
also why `scripts/deploy.sh` never touches that file.

Schema changes apply themselves on boot (`db.init()` and `telemetry.store.init()` are both
idempotent), so there is no migration step. To load telemetry for runs that finished before
that layer existed:

```bash
.venv/bin/python -m telemetry.backfill      # replays every salvaged transcript
```

## What a run does

1. Fetch the issue from GitHub
2. Restore a VM from the golden snapshot (~0.2s), with idle-suspend disabled and a self-destruct timer set
3. Clone the assigned repo (or reuse a checkout the golden already has), then `git fetch` and
   check out `factory/issue-<n>` from the default branch
4. Run `claude -p` with the issue as the prompt, streaming its event log back live
5. Look for the PR the agent opened; salvage the session transcript
6. Destroy the VM

Every step is a status transition on a row in `runs`, which is what makes the reconciler
possible: fleet state lives in a table, not in an agent's context window.

## The golden VM

Every run boots from a **golden snapshot** — never from a machine it shares with another run,
and never from something a person configured by hand and hoped to remember.

### Two tiers, and which one a run picks

The name is the whole registry. There is no list of goldens anywhere else:

| Snapshot | What it is |
|---|---|
| `golden-copy` | The **base image**. Tooling, skills, an agent login, warm toolchain caches — and no repo. Serves every repo. |
| `golden-<owner-repo>` | The **warm tier**: the same image with one repo already cloned at `$HOME/work/<name>` and installed. |

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
correctness. That is what lets a repo be connected and dispatched in the same minute, with
provisioning catching up afterwards.

### Building the base: `scripts/build-golden.sh`

```bash
scripts/build-golden.sh
```

Creates a machine from the `factory-base` snapshot, installs that agent's CLI, the skills from
[agent-skills](https://github.com/mithril-studio/agent-skills), `fnm` and `jq`, warms the
package-manager download caches (node versions, pnpm/yarn, CPython — toolchains, never a
project's packages), writes the two files a golden owes the control plane
(`/usr/local/bin/factory-agent` and `/etc/factory/agent.json`, see `control/README.md` §2.3),
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

### Warming one from the control plane: `POST /api/repos/<owner>/<name>/golden`

Connecting a repo starts this automatically. It is a run like any other — `kind: provision`, a
streamed log you can tail, a cancel button, a VM the reconciler recognises — and it restores
`golden-copy`, clones, installs, captures `golden-<slug>`, **waits for the capture to be
written**, and only then destroys the machine.

That wait is load-bearing, and it cost a golden to learn. The SDK's `snapshots.create` returns
once boxd has *queued* the capture; destroying the machine at that point aborts it, and the
name is left in the fleet at `pending` with no version behind it — which `agents.available()`
correctly refuses to dispatch onto and which nothing can repair. The symptom is a repo the
Projects page calls warm whose every run still boots the base. `scripts/build-golden.sh` never
hit it because the `boxd snapshots save` CLI blocks until the capture lands. A capture that
never lands (`FACTORY_CAPTURE_TIMEOUT`, 20 minutes) is deleted rather than left looking like
progress.

It never blocks the repo it was started for: warm-ups have their own concurrency budget
(`FACTORY_MAX_PROVISION`), and the poller does not count one as work in flight.

Always from the base, even when the repo already has a golden. Updating one in place is
faster and is what the shell script below does, but it also carries forward whatever the last
capture got wrong: building from `golden-copy` every time makes re-provisioning the *repair*
for a bad snapshot rather than another way to inherit it. `DELETE` the same path to drop a
repo's golden and send its runs back to the base.

The install command is the one the repo names in its own `## Setup` section, never a guess. A
repo that names none still gets a golden — the clone, captured without an install, because the
clone is the half that can be done without inventing anything and it is worth minutes on its
own. What the control plane will not do is infer an install command from a lock file: that is
how one project's command becomes every project's. `control/preflight.py`'s reader and
`refresh-golden.sh`'s `awk`/`sed` pipeline are pinned against each other in
`control/golden_scripts_test.py`, so the two cannot install the same repo differently.

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
  `control/README.md` §2.3
- the agent authenticated (inherited by every fork; expires)
- `gh` authenticated, as the fallback for a deployment that sets no `GITHUB_TOKEN`
- skills installed from [agent-skills](https://github.com/mithril-studio/agent-skills)

Never `boxd machine share` a golden: sharing deletes the in-VM agent credentials.

### Knowing which goldens work

There used to be an hourly sweep that `exec`d into every golden and asked how far behind its
checkout was and whether a dependency manifest had moved. A golden is a snapshot now: there is
no machine to ask, and a stale warm tier is re-provisioned rather than diagnosed — so all of
that stopped being worth asking at once, and the thing that actually kills a golden was never
on the list. What kills one is credential expiry, and the only real test of a credential is
using it.

So the control plane grades by evidence instead. Every `FACTORY_AGENT_REFRESH` seconds (300
by default) it re-lists the golden snapshots — one API call, no VM — and records a row per
name in the `snapshots` table: which repo it was warmed for, its snapshot version, what its
last run did on it, and what that run's manifest announced (including which agent it runs).
`POST /api/goldens/refresh` runs one on demand.

The column that matters is `verified_at`: **when a run last finished on that snapshot having
produced usage.** A golden that emitted tokens authenticated, so its `claude` login and its
`gh` token both still worked as of that moment. It costs nothing to know, because the runs
were happening anyway. A golden with no `verified_at` is unproven, not broken — nothing has
used it yet.

Repairing one is still manual and still project-specific: re-authenticate on a fork and
re-snapshot it under the same name.

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
   success the repo is in the register, its lifecycle labels exist, **a golden starts warming**,
   and the poller picks it up on its next tick — **no restart and no `.env` edit.**
   Disconnecting keeps the repo's runs, because they are the ledger of what was spent and
   shipped rather than configuration.

   The warm-up does not hold the repo up. It runs under its own concurrency budget
   (`FACTORY_MAX_PROVISION`), the poller does not count it as work in flight, and the repo
   dispatches onto `golden-copy` from the moment it is registered. A repo that names no `##
   Setup` command still gets a golden — the clone, without the install, which is the half that
   can be done without guessing how the project installs. Adding a `## Setup` section and
   rebuilding bakes the install in too.

   `FACTORY_REPOS` is now the *seed* for that register: entries are added on boot if they are
   not already there, and removing one does not unwatch the repo.
3. Give the repo a `.factory.md` (below) and CI that reports at least one check run —
   without checks, auto-merge can never pass its gate and every pull request waits for a
   human.
4. Ask, before trusting any of it:

   ```bash
   .venv/bin/python -m control.preflight mithril-studio/legal-ai-app
   ```

   It reports on the repo — readable, pushable, has CI, has a `.factory.md`, has a `##
   Setup` section, labels — and on one thing outside it: whether a golden snapshot exists
   for this repo to resolve onto. Exit status 0 means ready. Same answers over HTTP at
   `/api/preflight?repo=owner/repo`, which is what the Check button calls.

   It boots nothing. Every check is a GitHub call or a snapshot listing, which is the whole
   point: these are the questions a run otherwise answers the expensive way, after forking a
   VM and spending forty minutes to find out the token cannot push.

## What a watched repo tells the factory: `.factory.md`

A repo can describe itself to the agent in a `.factory.md` at its root. The control plane
reads it from the base branch at dispatch and splices it into the build and review prompts,
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

It is also what warming this repo's own golden runs, so a repo that names one gets that install
done once into a snapshot instead of once per run. A repo that names none still gets a golden,
with the clone in it and no install — never one provisioned with a guess.

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
`discussion.md` §"Where rules live".

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
- **Hard shadows, never blur.** `shadow-hard` / `-sm` / `-accent` are solid offsets. The
  accent (terracotta) shadow marks the one thing on a screen worth acting on — the lead stat
  tile, the sign-in card — and nothing else.
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

- **Runs** — every run: state, id, agent, project, duration, token spend, cost, PR link.
  Start a run by repo + issue number.
- **Run detail** — live agent output over SSE, tool calls and errors highlighted. The single
  biggest visibility win: a run silent for six minutes is otherwise indistinguishable from
  one that has hung.
- **Plan** — open issues across watched repos with their factory state, in the order the
  poller works them.
- **Projects** — watched repos with run tallies and the golden each one boots, and where a
  repo is connected: name it, read what preflight says, connect. Per row: warm or rebuild its
  golden, drop it, or disconnect the repo.
- **Goldens** — two tables, because they stopped being one question when goldens became
  snapshots. What the factory *can boot*: every golden snapshot (`/api/goldens`), its version,
  its telemetry adapter, and whether a run has ever proved its credentials. What *is* running:
  the boxd machines (`/api/machines`), by role, orphans flagged, with a reconcile button.
- **Telemetry** — where the money and the time go: cost by token class (cache reads are most
  of the bill), spend by day, tools by wall time, and cost per shipped issue against spend on
  runs that shipped nothing.

## What V0 deliberately does not have

Named so they stay unbuilt until something proves they are needed:

- No test agent or review agent. One agent, a few skills.
- No warm VM pool — forks are ~0.2s.
- No `mem` binary. Memory is a skill writing JSONL; extraction comes once the format is
  proven against real records.
- No `ExecutionBackend` abstraction. The control plane talks to boxd concretely; the
  interface gets extracted when a second backend exists.
- No queue broker, no webhooks.
- No second agent. One base image, and which agent it runs is what the image announces about
  itself. A second agent needs a second base image and a way to choose between them; neither
  exists until something needs it.

## Docs

- `docs/architecture.md` — layers, topology, and the contracts between them
- `control/README.md` — control plane design notes
- `telemetry/README.md` — trace layer design notes
- [agent-skills](https://github.com/mithril-studio/agent-skills) — the skills the in-VM agent loads
