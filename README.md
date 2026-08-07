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
| `telemetry/` | Trace layer: OTLP ingest, normalized usage | spec only |
| `docs/` | Architecture and the contracts between layers | — |

The folder is `control/` rather than `exec/` because `exec` is a Python keyword and cannot
be an importable package name.

Skills the agent uses live in a separate repo,
[mithril-studio/agent-skills](https://github.com/mithril-studio/agent-skills). They ship
**into the VM**, not onto the server — the agent's code, not the factory's — so they are
installed when a golden is built rather than deployed with the control plane.

## Quickstart

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env          # fill in BOXD_API_KEY and FACTORY_GOLDEN
.venv/bin/uvicorn control.app:app --port 8765 --reload
```

Then open <http://localhost:8765>. `GITHUB_TOKEN` is optional locally — it falls back to
`gh auth token`.

## What a run does

1. Fetch the issue from GitHub
2. Fork the golden VM (~0.2s), with idle-suspend disabled and a self-destruct timer set
3. `git fetch` and check out `factory/issue-<n>` from the default branch
4. Run `claude -p` with the issue as the prompt, streaming its event log back live
5. Look for the PR the agent opened; salvage the session transcript
6. Destroy the VM

Every step is a status transition on a row in `runs`, which is what makes the reconciler
possible: fleet state lives in a table, not in an agent's context window.

## The golden VM

Every run forks one long-lived machine. It must have:

- the repo cloned at `FACTORY_REPO_DIR` with a working `origin` remote
- dependencies installed
- `claude` authenticated (inherited by every fork — this credential expires, so re-auth and
  re-snapshot on a schedule)
- `gh` authenticated with push access to the repo
- skills installed from [agent-skills](https://github.com/mithril-studio/agent-skills):
  ```bash
  git clone --depth 1 https://github.com/mithril-studio/agent-skills /tmp/agent-skills \
    && /tmp/agent-skills/install.sh
  ```

Never `boxd machine share` a golden: sharing deletes the in-VM agent credentials.

## The UI

- **Runs** — every run, its status, duration, and PR link. Start a run by picking a repo and
  an issue.
- **Run detail** — live agent output over SSE, with tool calls and errors highlighted. The
  single biggest visibility win: a run that is silent for six minutes is otherwise
  indistinguishable from one that has hung.
- **Machines** — the boxd fleet, with orphaned run VMs flagged, and a reconcile button.

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
- `telemetry/README.md` — trace layer spec
- [agent-skills](https://github.com/mithril-studio/agent-skills) — the skills the in-VM agent loads
