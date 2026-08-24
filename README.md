# Software Factory

GitHub issues in, pull requests out.

A control plane watches your repos for queued issues. For each one it forks an isolated [boxd](https://boxd.sh) VM, runs a coding agent inside it, waits for the pull request, and destroys the VM. You watch it all happen live in a web UI.

The control plane is deterministic and contains no LLM. All intelligence lives in the agent, inside the VM.

## How a run works

1. Fetch the issue from GitHub
2. Restore a VM from a golden snapshot (about 0.2 seconds)
3. Clone the repo and check out a branch for the issue
4. Run the agent with the issue as its prompt, streaming output back live
5. Find the PR the agent opened, save the transcript
6. Destroy the VM

Every step is a status transition on a database row. Fleet state lives in a table, not in an agent's context window, which is what makes reconciliation possible.

## What's inside

| Folder | What it does |
|---|---|
| `control/` | Control plane: watch issues, fork VMs, dispatch agents, reap, serve the UI |
| `telemetry/` | One row per model call and tool call, priced and joined to outcomes |
| `web/` | React UI: live run logs, projects, golden VMs, cost breakdowns |
| `docs/` | Architecture, operations, and the contracts between layers |

## Quickstart

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env               # fill in BOXD_API_KEY and FACTORY_REPOS
npm --prefix web install && npm --prefix web run build
.venv/bin/uvicorn control.app:app --port 8765 --reload
```

Then open <http://localhost:8765>.

## Docs

Everything operational, including deploying, building golden VMs, and connecting repos, lives in [docs/OPERATIONS.md](docs/OPERATIONS.md). Architecture is in [docs/architecture.md](docs/architecture.md).

Skills the agent uses live in [mithril-studio/agent-skills](https://github.com/mithril-studio/agent-skills).

## Status

V0, working. Runs in production against real repos.
