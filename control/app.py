"""FastAPI app: a JSON API plus the built React UI, served from one process.

The API is the contract; the UI in `web/` is a Vite/React/Tailwind/shadcn SPA that consumes
it and builds to `web/dist`. In production FastAPI serves those static assets and falls back
to `index.html` for client-side routes. In development, run `vite dev` (it proxies `/api` and
`/healthz` back here) so the SPA hot-reloads against this same process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from telemetry import store as telemetry

from . import agents, auth, db, github, goldens, poller, preflight, provision, repos, runner
from .config import ROOT, settings

DIST = ROOT / "web" / "dist"

# Nothing configured logging, so every `log.info` in this package went nowhere. uvicorn sets up
# its own `uvicorn.*` loggers and leaves the root alone, which is why the access log was the
# only thing in `var/uvicorn.log` — a poller that halted a repo, a golden refresh that found
# nothing, a reconciler destroying a leaked VM, all silent. Configured here rather than in each
# module because a library that configures logging on import steals it from whoever imports it;
# this module *is* the application.
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# The libraries are noisy at INFO and say nothing about this system. Raise FACTORY_LOG_LEVEL to
# DEBUG deliberately when the question is about HTTP or the database, not as a default.
for noisy in ("httpx", "httpcore", "aiosqlite", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await telemetry.init()
    # Before the poller, and before the first request: everything that asks which repos are
    # watched reads a cache, and the cache is empty until this has run.
    await repos.seed()
    poller.start()
    goldens.start()
    runner.start_reconciler()
    try:
        yield
    finally:
        await poller.stop()
        await goldens.stop()
        await runner.stop_reconciler()


app = FastAPI(title="software factory", lifespan=lifespan)


# --------------------------------------------------------------------------- auth


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Gate every /api route behind a valid session cookie, save the auth endpoints."""
    if not auth.is_public(request.url.path):
        if not auth.valid_token(request.cookies.get(auth.COOKIE_NAME)):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
    return await call_next(request)


class Login(BaseModel):
    email: str
    password: str


@app.post("/api/login")
async def api_login(body: Login, response: Response):
    if not auth.check_credentials(body.email, body.password):
        raise HTTPException(401, "invalid email or password")
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_token(),
        max_age=auth.SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
async def api_me(request: Request):
    return {"authenticated": auth.valid_token(request.cookies.get(auth.COOKIE_NAME))}


# --------------------------------------------------------------------------- config


async def _available() -> tuple[str, ...]:
    """Golden snapshot names in the fleet, or nothing if the fleet cannot be reached.

    Best-effort on purpose: this backs advice, and a boxd outage should degrade the advice
    rather than take the API down with it.
    """
    boxd = runner.client()
    try:
        return await agents.available(boxd)
    except Exception:  # noqa: BLE001 - no credentials, no network: report nothing found
        return ()
    finally:
        await boxd.close()


@app.get("/api/config")
async def api_config():
    """What the shell needs: the repos being watched, limits, and what is wrong.

    Two kinds of wrong, kept apart. `missing` is a setting nobody filled in; `problems` is a
    configuration that is complete but has nothing to run on — no base image yet. The second
    question needs the fleet, which is why it lives here (async) and not in
    `settings.missing()`, which is synchronous and gates starting a run.
    """
    available = await _available()
    return {
        "repos": list(repos.watched()),
        "max_concurrent": settings.max_concurrent,
        "max_attempts": settings.max_attempts,
        "poll_enabled": settings.poll_enabled,
        "poll_interval": settings.poll_interval,
        "missing": settings.missing(),
        "problems": settings.problems(available),
    }


# --------------------------------------------------------------------------- runs


@app.get("/api/runs")
async def api_runs():
    return await db.list_runs()


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str):
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    return run


@app.get("/api/runs/{run_id}/telemetry")
async def api_run_telemetry(run_id: str):
    """Per-call detail for one run: token totals, spend by model, tool time."""
    return await telemetry.usage_for_run(run_id)


@app.get("/api/telemetry")
async def api_telemetry():
    """The fleet view.

    Four questions the run table alone could not answer: where the money goes by token
    class, what it is costing over time, which tools eat the wall clock, and what a
    merged pull request actually costs. All derived from the event rows — nothing here
    is a stored aggregate, so any of it can be re-cut without a migration.
    """
    return {
        "composition": await telemetry.cost_composition(),
        "spend_by_day": await telemetry.spend_by_day(),
        "tools": await telemetry.tool_leaderboard(),
        "economics": await telemetry.unit_economics(),
        "memory": await telemetry.memory_metrics_by_repo(),
    }


class StartRun(BaseModel):
    repo: str
    issue_number: int
    # 'build', 'review' (of a pull request a build already opened), or 'provision' (warm this
    # repo's golden). All three are runs on a restored VM, which is why one endpoint starts any
    # of them. A provision ignores `issue_number`; there is no issue behind it.
    kind: str = "build"
    pr_url: str | None = None
    branch: str | None = None
    # For a build: which attempt this is, so `VM_SCRIPT` resumes the branch instead of
    # resetting it to the base and throwing the previous attempt's commits away. For a
    # review: which review cycle.
    attempt: int = 1


@app.post("/api/runs")
async def api_start_run(body: StartRun):
    """Start a run. This is the only supported way to dispatch one by hand.

    It matters that this lives in the serving process: a run is an `asyncio` task holding a
    VM, so a run started from a throwaway script dies with that script — leaving a machine
    nobody reaps, a row that says `running` forever, and an issue mirroring a state that
    stopped being true. Anything that needs to start a run talks to this.
    """
    gaps = settings.missing()
    if gaps:
        raise HTTPException(400, f"configuration incomplete: {', '.join(gaps)}")
    repo = body.repo.strip()
    try:
        if body.kind == "review":
            if not body.pr_url or not body.branch:
                raise ValueError("a review needs pr_url and branch")
            run_id = await runner.create_review(
                repo, body.issue_number, body.pr_url, body.branch, cycle=body.attempt
            )
        elif body.kind == "build":
            run_id = await runner.create(repo, body.issue_number, attempt=body.attempt)
        elif body.kind == "provision":
            run_id = await provision.create(repo)
        else:
            raise ValueError(f"unknown run kind {body.kind!r}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not start run: {exc}") from exc
    return {"run_id": run_id}


@app.post("/api/runs/{run_id}/cancel")
async def api_cancel_run(run_id: str):
    return {"ok": await runner.cancel(run_id)}


@app.get("/api/runs/{run_id}/stream")
async def stream_log(run_id: str):
    """Tail a run's log over SSE until it reaches a terminal state."""
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    path = Path(run["log_path"])

    async def events():
        position = 0
        idle = 0
        while True:
            if path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                    position = handle.tell()
                if chunk:
                    idle = 0
                    for line in chunk.splitlines():
                        yield f"data: {json.dumps(line)}\n\n"
                else:
                    idle += 1
            current = await db.get_run(run_id)
            if current and current["status"] in db.TERMINAL and idle >= 1:
                yield f"event: done\ndata: {current['status']}\n\n"
                return
            await asyncio.sleep(0.6)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- plan / projects


@app.get("/api/issues")
async def api_issues(repo: str):
    """Open issues for the new-run picker."""
    try:
        return await github.list_open_issues(repo)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/plan")
async def api_plan(repo: str | None = None):
    """The work queue: open issues with their factory state.

    One repo if `repo` is given, otherwise every watched repo, concatenated.
    """
    targets = [repo] if repo else list(repos.watched())
    out: list[dict] = []
    for r in targets:
        try:
            out.extend(await github.plan(r))
        except Exception as exc:  # noqa: BLE001 - one bad repo shouldn't blank the page
            out.append(
                {
                    "repo": r,
                    "number": 0,
                    "title": f"(could not load: {exc})",
                    "url": "",
                    "state": "error",
                    "labels": [],
                }
            )
    return out


@app.post("/api/goldens/refresh")
async def api_goldens_refresh():
    """Re-list the golden snapshots now, rather than waiting for the next refresh."""
    return await goldens.refresh()


@app.get("/api/preflight")
async def api_preflight(repo: str):
    """Whether `repo` is ready to be dispatched to, and a golden exists to boot for it.

    Read-only: it reports, it never repairs. Answers here cost a second; the same answers
    found during a run cost a VM and forty minutes.
    """
    checks = await preflight.run(repo.strip())
    return {
        "repo": repo,
        "ready": not any(c for c in checks if not c.ok and c.fatal),
        "checks": [
            {"name": c.name, "ok": c.ok, "detail": c.detail, "fatal": c.fatal} for c in checks
        ],
    }


class ConnectRepo(BaseModel):
    repo: str


def _checks_json(checks) -> list[dict]:
    return [{"name": c.name, "ok": c.ok, "detail": c.detail, "fatal": c.fatal} for c in checks]


@app.post("/api/repos")
async def api_connect_repo(body: ConnectRepo):
    """Watch a repo from now on. The only supported way to connect one.

    Preflight runs first and a blocking failure refuses the connection, with its checks in the
    response so the caller can say *which* one. That is stricter than it looks: warnings do not
    block, and "no warm golden yet" is not a failure at all — a repo dispatches onto
    `golden-copy` and installs for itself, so connecting one never waits on provisioning.
    What does block is a repo the token cannot read or push to, which would spend a VM and an
    agent's whole context to discover the same thing.

    Preflight also creates the lifecycle labels on its way through, which makes this the whole
    of connecting a repo: no restart, no `.env` edit, no shell on the box.
    """
    repo = body.repo.strip()
    if not repos.valid(repo):
        raise HTTPException(400, f"{repo!r} is not owner/repo")
    if repo in repos.watched():
        raise HTTPException(409, f"{repo} is already watched")
    try:
        checks = await preflight.run(repo)
    except Exception as exc:  # noqa: BLE001 - an unreachable GitHub is a 400, not a 500
        raise HTTPException(400, f"could not check {repo}: {exc}") from exc
    blocking = [c for c in checks if not c.ok and c.fatal]
    if blocking:
        raise HTTPException(
            400,
            {
                "message": f"{repo} is not ready: " + ", ".join(c.name for c in blocking),
                "checks": _checks_json(checks),
            },
        )
    await repos.add(repo)
    # Warm a golden for it, if it says how to install itself. Best-effort and reported rather
    # than awaited: the repo is dispatchable the moment it is registered, so provisioning that
    # cannot start is a slower repo and not a failed connection. `provision` refuses when the
    # repo names no `## Setup` command, which is the ordinary reason this returns no run.
    provision_run, why = None, None
    try:
        provision_run = await provision.create(repo)
    except Exception as exc:  # noqa: BLE001 - the message is what the caller shows
        why = str(exc)
    return {
        "repo": repo,
        "checks": _checks_json(checks),
        "provision_run": provision_run,
        "provision_skipped": why,
    }


@app.post("/api/repos/{owner}/{name}/golden")
async def api_provision_golden(owner: str, name: str):
    """Warm this repo's golden now: restore the base, clone, install, capture, destroy.

    Also how a stale one is refreshed — provisioning always builds from `golden-copy` rather
    than updating the repo's existing snapshot in place, so re-running this is the repair for a
    golden that has gone wrong as well as the way to make one.
    """
    repo = f"{owner}/{name}"
    if repo not in repos.watched():
        raise HTTPException(404, f"{repo} is not watched")
    try:
        return {"run_id": await provision.create(repo)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/repos/{owner}/{name}/golden")
async def api_delete_golden(owner: str, name: str):
    """Delete this repo's golden. Its runs fall back to the base and install for themselves."""
    repo = f"{owner}/{name}"
    if repo not in repos.watched():
        raise HTTPException(404, f"{repo} is not watched")
    try:
        return {"repo": repo, "deleted": await provision.unprovision(repo)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/repos/{owner}/{name}")
async def api_disconnect_repo(owner: str, name: str):
    """Stop watching a repo. Its runs stay — they are the ledger, not the configuration.

    Two path segments rather than one encoded parameter, because `owner/repo` carries a slash
    and a client that forgets to encode it would otherwise hit a route that does not exist and
    read the 404 as "already gone".
    """
    repo = f"{owner}/{name}"
    if not await repos.remove(repo):
        raise HTTPException(404, f"{repo} is not watched")
    return {"repo": repo, "removed": True}


@app.get("/api/projects")
async def api_projects():
    """Watched repos with their run tallies and the snapshot their runs boot."""
    watched = repos.rows()
    stats = await db.stats_by_repo()
    # Derived the same way a dispatch derives it, rather than by reading a name off a config
    # value — otherwise this column would say what the repo was configured with where the
    # fleet views say what would actually boot.
    boxd = runner.client()
    try:
        sources = {r["repo"]: await runner.source_for(boxd, r["repo"]) for r in watched}
    except Exception:  # noqa: BLE001 - a boxd outage costs a column, not the page
        sources = {}
    finally:
        await boxd.close()
    out = []
    for row in watched:
        repo = row["repo"]
        golden = sources.get(repo, "")
        s = stats.get(repo, {})
        out.append(
            {
                "repo": repo,
                "added_at": row.get("added_at"),
                # What provisioning a warm golden for this repo did, which is not the same
                # question as what its runs boot: a repo whose provisioning failed still
                # dispatches onto the base, so this is a report and never a gate.
                "provision_status": row.get("provision_status") or "none",
                "golden": golden,
                # Whether the snapshot that boots is this repo's own warm tier rather than
                # the base image. Decided here, where the naming contract lives.
                "warm": bool(golden) and golden == agents.golden_name(repo),
                "runs": s.get("runs", 0) or 0,
                "succeeded": s.get("succeeded", 0) or 0,
                "failed": s.get("failed", 0) or 0,
                "active": s.get("active", 0) or 0,
                "last_run": s.get("last_run"),
            }
        )
    return out


# --------------------------------------------------------------------------- goldens / fleet

# Two different questions, and they stopped having the same answer when goldens became
# snapshots. What the factory *can boot* is a list of snapshots (`/api/goldens`); what is
# *running* is a list of machines (`/api/machines`). The old endpoint answered the second and
# was named for the first, which is why nothing in the fleet view could tell you a golden
# existed until a run had already failed to find it.


@app.get("/api/goldens")
async def api_goldens():
    """The golden snapshots this deployment can dispatch onto, as the refresh loop last saw them.

    Served from the table rather than from boxd, so this stays cheap enough to poll and says
    the same thing a dispatch would: `goldens.refresh()` is the one place that reads the fleet.
    """
    return agents.api_rows(await db.snapshots())


@app.get("/api/machines")
async def api_machines():
    """Boxd machines: the VMs runs are happening on, plus anything else in the fleet."""
    boxd = runner.client()
    try:
        machines = await boxd.machines.list()
    finally:
        await boxd.close()
    active = {r["vm_name"] for r in await db.active_runs() if r.get("vm_name")}
    out = []
    for m in machines:
        role = runner.vm_role(m.name)
        out.append(
            {
                "name": m.name,
                "status": getattr(m, "status", None),
                "role": role,
                # A run VM with no run behind it any more. What reconcile reaps.
                "orphan": role != "other" and m.name not in active,
            }
        )
    out.sort(key=lambda m: (m["role"] == "other", m["name"]))
    return out


@app.post("/api/reconcile")
async def api_reconcile():
    return JSONResponse(await runner.reconcile())


@app.get("/healthz")
async def healthz():
    return {"ok": True, "configured": not settings.missing()}


# --------------------------------------------------------------------------- static SPA

# Mount built assets, then fall back to index.html for client-side routes. Registered last so
# it never shadows an /api route. If the app hasn't been built, say so instead of 404ing.
if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")


@app.get("/{full_path:path}")
async def spa(full_path: str):
    index = DIST / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse(
        {"detail": "UI not built. Run `npm --prefix web install && npm --prefix web run build`."},
        status_code=503,
    )
