---
name: test-server
description: Deploy a branch to the factory's boxd test control plane and check it against a copy of production's data before it reaches Hetzner. Use when asked to "try this on the test server", "deploy to staging", "check this before prod", "refresh the test database", or to rebuild/repair `software-factory-test-server`. Also use before any change to the UI, the schema, or a page that reads run history — those break in ways an empty database cannot show.
---

# The test server

Production is a Hetzner box. Every change to it used to be checked by deploying it and
watching. This skill is the step in between: **`software-factory-test-server`**, a second
control plane on boxd carrying a copy of production's database, so a page can be looked at
with real runs, real telemetry and real money in it before anyone touches prod.

| | |
|---|---|
| Machine | boxd VM `software-factory-test-server` |
| URL | <https://software-factory-test-server.boxd.sh> |
| Checkout | `/home/boxd/software-factory`, uvicorn `0.0.0.0:8765` |
| Service | `factory.service` — logs in `var/uvicorn.log` |
| Login | `FACTORY_AUTH_EMAIL` / `FACTORY_AUTH_PASSWORD` in the box's `.env` |
| Data | a snapshot of prod's `var/factory.db`, reloaded on demand |

## §1 Deploying to it

```bash
scripts/deploy-test.sh --ref my-branch     # put a branch on it
scripts/deploy-test.sh                     # redeploy whatever it is already on
scripts/deploy-test.sh --refresh-db        # ...reload prod's data first
```

`--ref` checks the branch out on the box as a local branch called `deploy`, tracking
`origin/<ref>`. Without `--ref` the box hard-resets to that branch's upstream, so the
no-argument form is "pull whatever I last put here" — not "go back to main".

Deps are reinstalled only when `pyproject.toml`/`uv.lock` or `web/package*.json` moved
between the two commits, exactly as `deploy.sh` does for Hetzner. The UI is rebuilt every
time; `vite build` is five seconds and a stale `web/dist` is invisible until it confuses
somebody.

Schema changes need no migration step: `db.init()` and `telemetry.store.init()` are
idempotent and run on boot, so the restart at the end of the deploy is the whole migration.

**Verifying is not optional.** The script already asserts `systemctl is-active` and
`healthz: 200` before it claims success. Anything past that — a page renders, a run detail
loads — is a browser job, at the URL above.

## §2 Refreshing the data

`--refresh-db` stops the service, replaces `var/factory.db` and `var/logs/`, and restarts.
Two details in it are load-bearing, and both are the reason not to do this by hand:

1. **It takes a `sqlite3` `.backup()` on the Hetzner box**, not a file copy. Prod is serving
   while you copy; a byte-for-byte copy of a live SQLite file can land mid-write and give you
   a database that opens and then fails on the one page you were testing.
2. **It rewrites `log_path` and `transcript_path`.** Those columns are absolute and point at
   `/home/factory/...`, which does not exist on a boxd VM — the user is `boxd`. Skip the
   rewrite and every historical run's log 404s, which reads exactly like a bug in the code
   you were checking.

Telemetry lives in the same `var/factory.db`. There is no second file to copy.

**The copy is a snapshot, not a replica.** It diverges from prod the moment either side moves,
and nothing on the test box writes back. When a question depends on current prod data,
refresh first — do not assume the box is up to date because it was yesterday.

## §3 The rails

The box's `.env` sets:

```
FACTORY_POLL=0        # never claim a queued issue
FACTORY_AUTO_MERGE=0  # never merge a pull request
```

**Do not turn either on.** The test box holds the same GitHub PAT as prod, so with polling on
it would race the Hetzner control plane for the same `agent:queued` issue and one of them
would lose a run. With auto-merge on it would land a staging run's PR in `main`.

What it *does* do, and is meant to: a run dispatched from its UI forks a real golden, pushes
a real branch and opens a **real** pull request. That is the point — the run path is what you
came to test — but nothing merges, and the PR is yours to close.

`FACTORY_MAX_CONCURRENT=1`, `FACTORY_MAX_ATTEMPTS=1` and `FACTORY_KEEP_FAILED=1` are also
deliberate: on a box you are watching, one run at a time that leaves its VM alive to look at
beats three that clean up after themselves.

## §4 Credentials

`.env` on the box is **not** a copy of prod's, and should never become one. It is written on
the machine out of what boxd injects (`GITHUB_PAT_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`), so no
secret travels through a laptop. `deploy-test.sh` never touches the file, for the same reason
`deploy.sh` never touches Hetzner's.

`BOXD_API_KEY` is empty on purpose. Inside a boxd VM the SDK mints a token from the machine's
own identity, so the control plane can list snapshots and reconcile the fleet with no key at
all. Two consequences worth knowing before you debug them:

- `factory.service` sets `Environment=BOXD_VM_NAME=...`. systemd does **not** inherit boxd's
  injected environment, and `boxd._credentials.looks_like_a_machine()` checks exactly
  `BOXD_VM_ID` / `BOXD_VM_NAME` / `BOXD_PROXY_ENDPOINT`. Drop that line and the service starts
  fine and then fails every fleet call with `AuthenticationError`.
- `config.Settings.missing()` checks for the key literally, and it gates `POST /api/runs`. So
  reading the fleet works without one, and **dispatching a run from the UI does not**. To
  enable dispatch:

  ```bash
  scripts/test-server-key.sh
  ```

  The key goes from `boxd auth keys create` straight down a pipe into the VM and is shredded
  there once it is in `.env`; it never touches your disk. Minting is deliberately not part of
  provisioning — rebuilding a box should not silently create a credential.

Never `boxd machine share` this machine: sharing deletes the in-VM agent credentials, the
same rule that applies to a golden.

## §5 Rebuilding it

```bash
scripts/provision-test-server.sh          # machine, checkout, venv, UI, .env, systemd, port
scripts/deploy-test.sh --refresh-db       # then load the data
scripts/test-server-key.sh                # only if you want to dispatch runs from the UI
```

The script is idempotent — an existing machine is reused, an existing `.env` is left alone —
so it is also the repair path, not only the build path. It prints the generated login on a
first build; that is the only time it is shown, and after that the value lives in the box's
`.env`.

Throwing the box away is cheap and is often the right move: it holds a copy of prod's data
and none of its own.

```bash
boxd machine remove software-factory-test-server -y
```

## §6 Failure modes

| Symptom | Cause |
|---|---|
| `set: Illegal option -o pipefail` | `boxd machine exec` runs the command under `sh` (dash), not bash. Use `set -eu`. |
| `AuthenticationError: no credentials` in `var/uvicorn.log` | `BOXD_VM_NAME` missing from the unit (§4). |
| Historical run logs 404 | the `log_path` rewrite did not run — reload with `--refresh-db`. |
| The UI hangs, then answers on refresh | auto-suspend got switched back on. A suspended VM's clock stops, so a run it dispatched stalls. Keep `auto-suspend: off`; verify with `boxd machine get`. |
| A warm golden is ignored right after provisioning | a fresh snapshot sits `pending` for minutes; the control plane falls back to `golden-copy` until it has a ready version. Not a test-server problem. |
| `git` asks for a password on the box | the credential helper reads `GITHUB_PAT_TOKEN` from boxd's injected environment. Present in `machine exec` and interactive shells; absent under systemd, which never runs git. |

## §7 Cost

The machine has auto-suspend and auto-hibernate **off**, which is what keeps a dispatched run
from stalling — and means it bills while idle. Park it between sessions instead of switching
the timeouts back on:

```bash
boxd machine pause software-factory-test-server    # warm; resumes sub-millisecond
boxd machine resume software-factory-test-server
```
