# Software Factory

GitHub issues in, reviewed pull requests out.

A control plane watches your repos for queued issues. For each one it restores an isolated [boxd](https://boxd.sh) VM from a golden snapshot, runs a coding agent inside it, reviews and merges the pull request, and destroys the VM. You watch it all happen live in a web UI.

The control plane is deterministic and contains no LLM. All intelligence lives in the agent, inside the VM.

## How a run works

1. Fetch the issue from GitHub
2. Restore a VM from a golden snapshot (about 0.2 seconds)
3. Clone the repo and check out a branch for the issue
4. Run the agent with the issue as its prompt, streaming output back live
5. Find the PR the agent opened; save the transcript and any memory candidates
6. Destroy the VM
7. Review the PR against the issue's acceptance criteria on a second VM, wait for CI,
   and merge — a rejecting review or red CI dispatches a fix run instead

Every step is a status transition on a database row. Fleet state lives in a table, not in an agent's context window, which is what makes reconciliation possible.

## What's inside

| Folder | What it does |
|---|---|
| `control/` | Control plane: watch issues, restore VMs, dispatch agents, review, merge, reap |
| `telemetry/` | One row per model call, tool call, and memory read, priced and joined to outcomes |
| `web/` | React UI: live run logs, projects, golden VMs, cost breakdowns |
| `scripts/` | Goldens, deploy, the CI gate list, health monitoring |
| `docs/` | Architecture, operations, and the contracts between layers |

## Quickstart

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env   # set BOXD_API_KEY and FACTORY_AUTH_EMAIL/PASSWORD;
                       # leave FACTORY_REPOS empty — connect repos in the UI
npm --prefix web install && npm --prefix web run build
.venv/bin/uvicorn control.app:app --port 8765 --reload
```

Then open <http://localhost:8765> and log in with the credentials you set.

## Docs

Everything operational, including deploying, building golden VMs, and connecting repos, lives in [docs/OPERATIONS.md](docs/OPERATIONS.md). Architecture is in [docs/architecture.md](docs/architecture.md).

Skills the agent uses live in [mithril-studio/agent-skills](https://github.com/mithril-studio/agent-skills).

## Status

V0, working. Runs in production against real repos.
