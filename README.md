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

The control plane lives on the long-lived boxd VM **`software-factory`** — a plain checkout
under `~/software-factory` running uvicorn, not a container. Deploy by pulling:

```bash
ssh software-factory.boxd.sh
cd ~/software-factory
git pull
.venv/bin/pip install -e .                 # only when dependencies or packages change
npm --prefix web install && npm --prefix web run build
pkill -f 'uvicorn control.app' ; sleep 1
nohup .venv/bin/uvicorn control.app:app --host 0.0.0.0 --port 8765 >> uvicorn.log 2>&1 &
```

Schema changes apply themselves on boot (`db.init()` and `telemetry.store.init()` are both
idempotent), so there is no migration step. To load telemetry for runs that finished before
that layer existed:

```bash
.venv/bin/python -m telemetry.backfill      # replays every salvaged transcript
```

## What a run does

1. Fetch the issue from GitHub
2. Fork the golden VM (~0.2s), with idle-suspend disabled and a self-destruct timer set
3. Clone the assigned repo (or reuse a checkout the golden already has), then `git fetch` and
   check out `factory/issue-<n>` from the default branch
4. Run `claude -p` with the issue as the prompt, streaming its event log back live
5. Look for the PR the agent opened; salvage the session transcript
6. Destroy the VM

Every step is a status transition on a row in `runs`, which is what makes the reconciler
possible: fleet state lives in a table, not in an agent's context window.

## The golden VM

Every run forks one long-lived machine. It must have:

- `git`, `gh` and the language toolchains the watched repos need — but *not* a repo: a golden
  is an agent image, and each run clones the repo it was assigned into `$HOME/work/<name>`
  (`FACTORY_WORKDIR` moves that). A per-repo golden may still carry a warm checkout there,
  which the run reuses instead of cloning
- dependencies installed, for the repo a warm golden was built for
- `claude` authenticated (inherited by every fork — this credential expires, so re-auth and
  re-snapshot on a schedule)
- `gh` authenticated with push access to the repo
- skills installed from [agent-skills](https://github.com/mithril-studio/agent-skills):
  ```bash
  git clone --depth 1 https://github.com/mithril-studio/agent-skills /tmp/agent-skills \
    && /tmp/agent-skills/install.sh
  ```

Never `boxd machine share` a golden: sharing deletes the in-VM agent credentials.

### Keeping a golden warm

A per-repo golden carries a warm checkout and a warm install, and that stays true only while
the install still matches the repo — nothing kept it true. (The prompt no longer promises the
agent any of it: it says the download caches are warm and sends it to install once. A stale
golden is now a slower run rather than a wrong one.) The control plane sweeps every golden
hourly (`FACTORY_GOLDEN_SWEEP`) and records what it finds: how far behind the checkout is,
whether the tree is dirty, and whether a dependency manifest moved in the commits it is
missing. The Projects page shows it, and `POST /api/goldens/sweep` runs one on demand.

The sweep **observes only**. A run checks its own branch out from `origin/<base>` anyway, so
the code on a golden is never what goes stale — the install is, and how to redo that is
project-specific. Repairing is one command:

```bash
scripts/sync-golden.sh factory-golden /home/boxd/repo main 'npm ci && npm run build'
```

## Adding a repo

1. Build or fork a golden for it: the repo cloned at `FACTORY_REPO_DIR`, dependencies
   installed, `claude` and `gh` authenticated, skills installed (see below).
2. Add it to `FACTORY_REPOS` as `owner/repo=golden-name`.
3. Give the repo a `.factory.md` (below) and CI that reports at least one check run —
   without checks, auto-merge can never pass its gate and every pull request waits for a
   human.
4. Ask, before trusting any of it:

   ```bash
   .venv/bin/python -m control.preflight mithril-studio/legal-ai-app
   ```

   It reports on the repo (readable, pushable, has CI, has a profile, labels) and on the
   golden its runs would actually fork (checkout is the right repo, clean, on the base
   branch, how far behind, toolchain against `.nvmrc`, `claude`, `gh`, skills). Exit status
   0 means ready. Same answers over HTTP at `/api/preflight?repo=owner/repo`.

   These are the questions a run answers the expensive way — after forking a VM and spending
   forty minutes finding out that the checkout belongs to another project.

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

The single highest-value line in the file. A golden is an agent image, not a project image:
it carries the toolchains and a warm package-manager download cache, and nothing installed
for your repo. The prompt sends the agent to run this command once, in the foreground, before
it touches any code — so naming it here is the difference between one install and a run that
spends turns discovering it needs one, guessing a command, and re-reading its whole context
between each guess.

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

- **Runs** — every run: state, id, agent, project, duration, token spend, cost, PR link.
  Start a run by repo + issue number.
- **Run detail** — live agent output over SSE, tool calls and errors highlighted. The single
  biggest visibility win: a run silent for six minutes is otherwise indistinguishable from
  one that has hung.
- **Plan** — open issues across watched repos with their factory state, in the order the
  poller works them.
- **Projects** — watched repos with run tallies.
- **Agents** — the boxd fleet: the golden runs fork from, live run VMs, orphans flagged, with
  a reconcile button.
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
- No issue polling yet. Runs are started from the UI.
- No queue broker, no webhooks.

## Docs

- `docs/architecture.md` — layers, topology, and the contracts between them
- `control/README.md` — control plane design notes
- `telemetry/README.md` — trace layer design notes
- [agent-skills](https://github.com/mithril-studio/agent-skills) — the skills the in-VM agent loads
