# Software Factory

Label a GitHub issue `agent:queued`. The factory restores an isolated [boxd](https://boxd.sh)
VM from a golden snapshot, runs a coding agent on the issue, opens a pull request into `test`,
and destroys the VM. The issue tells you what happened; you review and merge.

The control plane is deterministic and contains no LLM. All intelligence lives in the agent,
inside the VM.

## How a run works

1. Pick the lowest `agent:queued` issue on a repo with nothing in flight (and, by default, no
   factory PR still waiting to be merged)
2. Take `agent:queued` off, label it `agent:running`, comment where the PR will go
3. Restore a VM from a golden snapshot (about 0.2 seconds) and check out `factory/issue-<n>`
   from `test` — or continue that branch if its PR is still open
4. Run the agent with the issue as its prompt, streaming the log live and recording cost
5. Find the pull request, save the transcript, destroy the VM
6. Label the issue `agent:done` or `agent:failed`, with the PR link and the cost

To iterate, comment on the PR and re-add `agent:queued`: the next run reads the comments and
pushes to the same branch. Write issues in the shape of
[docs/ISSUE_TEMPLATE.md](docs/ISSUE_TEMPLATE.md).

## What's inside

| Folder | What it does |
|---|---|
| `control/` | Control plane: watch issues, restore VMs, dispatch the agent, report, reap |
| `telemetry/` | One row per model call, tool call, and memory read, priced and joined to outcomes |
| `web/` | React UI: runs with live logs, the queue, projects, goldens, cost |
| `scripts/` | Goldens, deploy, the CI gate list, health monitoring |
| `docs/` | Architecture, operations, and the issue template |

## Quickstart

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env   # set BOXD_API_KEY and FACTORY_AUTH_EMAIL/PASSWORD;
                       # leave FACTORY_REPOS empty — connect repos in the UI
npm --prefix web install && npm --prefix web run build
.venv/bin/uvicorn control.app:app --port 8765 --reload
```

Then open <http://localhost:8765> and log in with the credentials you set.

An optional [post-build verification step](docs/verification.md) runs the versioned browser
harness before VM teardown. It records required/advisory verdicts and a deterministic next
action; merging and re-queuing remain human decisions.

## Docs

Everything operational, including deploying, building golden VMs, and connecting repos, lives in [docs/OPERATIONS.md](docs/OPERATIONS.md). Architecture is in [docs/architecture.md](docs/architecture.md).

Skills the agent uses live in [mithril-studio/agent-skills](https://github.com/mithril-studio/agent-skills).

## Status

v1 (2026-11-12): back to the single primitive. The earlier review, merge, release and
planning pipeline is in git history; `docs/architecture.md` §4 says what comes next and how.
