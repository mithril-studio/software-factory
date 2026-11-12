"""FastAPI app: a JSON API plus the built React UI, served from one process.

In production FastAPI serves `web/dist` and falls back to `index.html` for client-side
routes. In development, run `vite dev`, which proxies `/api` back here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from telemetry import digest as telemetry_digest
from telemetry import store as telemetry

from . import agents, auth, db, github, poller, preflight, repos, runner
from .config import ROOT, settings

DIST = ROOT / "web" / "dist"

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for noisy in ("httpx", "httpcore", "aiosqlite", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("factory.app")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await telemetry.init()
    await repos.seed()
    # Before the poller: interrupted runs are closed and re-queued, and any VM they left
    # behind is destroyed, so a re-queued issue never runs beside its predecessor's machine.
    await runner.recover()
    try:
        await runner.reconcile()
    except Exception:  # noqa: BLE001 - boxd unreachable at boot: the timer sweeps later
        log.exception("startup reconcile failed")
    poller.start()
    runner.start_reconciler()
    try:
        yield
    finally:
        await poller.stop()
        await runner.stop_reconciler()
        await runner.shutdown()


app = FastAPI(title="software factory", lifespan=lifespan)


# --------------------------------------------------------------------------- auth

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def cross_site(request: Request) -> bool:
    """True for a state-changing request a browser sent from another site.

    The session cookie is `SameSite=Lax`, which does not cover sibling subdomains of a host
    that is not on the public suffix list (every `*.boxd.sh` machine is "same site"). So a
    non-GET must come from this origin. Clients that send neither header (curl, the health
    check) are not browsers and carry no ambient cookie to abuse.
    """
    if request.method in SAFE_METHODS:
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        return True
    return False


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Gate every /api route behind a session cookie, and refuse cross-site writes."""
    if request.url.path.startswith("/api/") and cross_site(request):
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
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
    """Golden snapshot names in the fleet, or nothing if the fleet cannot be reached."""
    boxd = runner.client()
    try:
        return await agents.available(boxd)
    except Exception:  # noqa: BLE001 - no credentials, no network: report nothing found
        return ()
    finally:
        await boxd.close()


@app.get("/api/config")
async def api_config():
    return {
        "repos": list(repos.watched()),
        "target_branch": settings.target_branch,
        "sequential": settings.sequential,
        "run_budget_eur": settings.run_budget_eur,
        "max_concurrent": settings.max_concurrent,
        "poll_enabled": settings.poll_enabled,
        "poll_interval": settings.poll_interval,
        # Nothing halts a repo any more: a failed issue is labelled and the queue moves on.
        "halt_on_failure": False,
        "missing": settings.missing(),
        "problems": settings.problems(await _available()),
    }


# --------------------------------------------------------------------------- runs


@app.get("/api/runs")
async def api_runs(limit: int = 100):
    return await db.list_runs(min(max(limit, 1), 500))


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str):
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    return run


@app.get("/api/runs/{run_id}/verification/{artifact:path}")
async def api_verification_artifact(run_id: str, artifact: str):
    if not await db.get_run(run_id):
        raise HTTPException(404, "run not found")
    root = (settings.log_dir / f"{run_id}-verification").resolve()
    path = (root / artifact).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "artifact not found")
    # Never execute HTML captured from an app under the factory's origin.
    return FileResponse(path, filename=path.name, media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/runs/{run_id}/telemetry")
async def api_run_telemetry(run_id: str):
    return await telemetry.usage_for_run(run_id)


@app.get("/api/telemetry")
async def api_telemetry():
    return {
        "composition": await telemetry.cost_composition(),
        "spend_by_day": await telemetry.spend_by_day(),
        "tools": await telemetry.tool_leaderboard(),
        "economics": await telemetry.unit_economics(),
        "memory": await telemetry.memory_metrics_by_repo(),
    }


@app.get("/api/digest")
async def api_digest(repo: str | None = None, days: int = 14):
    return await telemetry_digest.build(repo=repo, days=min(max(days, 1), 365))


class StartRun(BaseModel):
    repo: str
    issue_number: int


@app.post("/api/runs")
async def api_start_run(body: StartRun):
    """Start a run by hand. Same path as the poller, so the same one-per-repo rule holds."""
    gaps = settings.missing()
    if gaps:
        raise HTTPException(400, f"configuration incomplete: {', '.join(gaps)}")
    repo = body.repo.strip()
    if repo not in repos.watched():
        raise HTTPException(400, f"{repo} is not watched")
    try:
        return {"run_id": await runner.create(repo, body.issue_number)}
    except runner.Busy as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not start run: {exc}") from exc


@app.post("/api/runs/{run_id}/cancel")
async def api_cancel_run(run_id: str):
    return {"ok": await runner.cancel(run_id)}


@app.get("/api/runs/{run_id}/stream")
async def stream_log(run_id: str, request: Request):
    """Tail a run's log over SSE until the run is terminal.

    Each event's id is the byte offset after it, so a reconnecting EventSource (which sends
    `Last-Event-ID`) resumes where it left off instead of replaying the whole log.
    """
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    path = Path(run["log_path"])
    try:
        start = max(int(request.headers.get("last-event-id") or 0), 0)
    except ValueError:
        start = 0

    async def events():
        position = start
        idle = 0
        while True:
            if path.exists():
                with path.open("rb") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                # Only whole lines, so an offset never splits one.
                cut = chunk.rfind(b"\n") + 1
                if cut:
                    idle = 0
                    for raw in chunk[:cut].splitlines(keepends=True):
                        position += len(raw)
                        line = raw.decode("utf-8", errors="replace").rstrip("\n")
                        yield f"id: {position}\ndata: {json.dumps(line)}\n\n"
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


# --------------------------------------------------------------------------- queue and repos


@app.get("/api/issues")
async def api_issues(repo: str):
    """Open issues for the start-a-run picker."""
    try:
        return await github.list_open_issues(repo)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/plan")
async def api_plan(repo: str | None = None):
    """The queue: open issues with their factory state, for one repo or every watched one.

    For one repo, a GitHub failure is a 502: an empty list would say the repo has no open
    issues, which is how a DNS outage once passed for a quiet queue.
    """
    if repo:
        try:
            return await github.plan(repo)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not read issues for {repo}: {exc}") from exc
    out: list[dict] = []
    for target in repos.watched():
        try:
            out.extend(await github.plan(target))
        except Exception as exc:  # noqa: BLE001 - one unreadable repo must not blank the rest
            log.warning("could not read issues for %s: %r", target, exc)
    return out


class QueueIssue(BaseModel):
    repo: str
    number: int


@app.post("/api/plan/queue")
async def api_queue_issue(body: QueueIssue):
    """Label an issue `agent:queued`, exactly what doing it on GitHub does. The poller picks
    it up on its next pass; a failed issue queued again continues from its pushed commits."""
    repo = body.repo.strip()
    if repo not in repos.watched():
        raise HTTPException(400, f"{repo} is not watched")
    try:
        await github.add_labels(repo, body.number, [github.LABEL_QUEUED])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not label {repo}#{body.number}: {exc}") from exc
    return {"ok": True}


def _checks_json(checks) -> list[dict]:
    return [{"name": c.name, "ok": c.ok, "detail": c.detail, "fatal": c.fatal} for c in checks]


@app.get("/api/github/repos")
async def api_github_repos():
    """The repos this token can see, for the connect picker. Never a gate."""
    watched = set(repos.watched())
    try:
        found = await github.list_repos()
    except Exception as exc:  # noqa: BLE001 - the message is what the picker shows
        return {"repos": [], "error": f"could not list your GitHub repos: {exc}"}
    return {
        "repos": [
            {**r, "connected": r["full_name"] in watched} for r in found if not r["archived"]
        ],
        "error": None,
    }


@app.get("/api/preflight")
async def api_preflight(repo: str):
    repo = repo.strip()
    if not repos.valid(repo):
        raise HTTPException(400, f"{repo!r} is not owner/repo")
    try:
        checks = await preflight.run(repo)
    except Exception as exc:  # noqa: BLE001 - an unreachable GitHub is a 400, not a 500
        raise HTTPException(400, f"could not check {repo}: {exc}") from exc
    return {
        "repo": repo,
        "ready": not any(c for c in checks if not c.ok and c.fatal),
        "checks": _checks_json(checks),
    }


class ConnectRepo(BaseModel):
    repo: str


@app.post("/api/repos")
async def api_connect_repo(body: ConnectRepo):
    """Watch a repo from now on. A blocking preflight failure refuses the connection."""
    repo = body.repo.strip()
    if not repos.valid(repo):
        raise HTTPException(400, f"{repo!r} is not owner/repo")
    if repo in repos.watched():
        raise HTTPException(409, f"{repo} is already watched")
    try:
        checks = await preflight.run(repo)
    except Exception as exc:  # noqa: BLE001
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
    return {"repo": repo, "checks": _checks_json(checks)}


class RepoSettings(BaseModel):
    # Where this repo's pull requests go. Empty or null reverts to FACTORY_TARGET_BRANCH.
    base_branch: str | None = None


@app.patch("/api/repos/{owner}/{name}")
async def api_repo_settings(owner: str, name: str, body: RepoSettings):
    repo = f"{owner}/{name}"
    try:
        row = await repos.set_target_branch(repo, body.base_branch)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"repo": repo, "base_branch": row.get("base_branch")}


@app.delete("/api/repos/{owner}/{name}")
async def api_disconnect_repo(owner: str, name: str):
    repo = f"{owner}/{name}"
    if not await repos.remove(repo):
        raise HTTPException(404, f"{repo} is not watched")
    return {"repo": repo, "removed": True}


@app.get("/api/projects")
async def api_projects():
    """Watched repos with their run tallies and the snapshot their runs boot."""
    stats = await db.stats_by_repo()
    available = await _available()
    out = []
    for row in repos.rows():
        repo = row["repo"]
        golden = agents.resolve_snapshot(repo, available) or ""
        s = stats.get(repo, {})
        out.append(
            {
                "repo": repo,
                "added_at": row.get("added_at"),
                "golden": golden,
                "warm": bool(golden) and golden == agents.golden_name(repo),
                "target_branch": repos.target_branch(repo),
                "base_branch": row.get("base_branch"),
                "runs": s.get("runs", 0) or 0,
                "succeeded": s.get("succeeded", 0) or 0,
                "failed": s.get("failed", 0) or 0,
                "active": s.get("active", 0) or 0,
                "last_run": s.get("last_run"),
            }
        )
    return out


# --------------------------------------------------------------------------- fleet


@app.get("/api/goldens")
async def api_goldens():
    """Golden snapshots in the fleet, as boxd lists them right now."""
    boxd = runner.client()
    try:
        names = await agents.listed(boxd)
    finally:
        await boxd.close()
    return [
        {"name": n, "repo": agents.parse_golden(n), "status": agents.status(n),
         "version": agents.version(n)}
        for n in names
    ]


@app.get("/api/machines")
async def api_machines():
    """Boxd machines: run VMs, plus anything else in the fleet."""
    boxd = runner.client()
    try:
        machines = await boxd.machines.list()
    finally:
        await boxd.close()
    active = {r["vm_name"] for r in await db.active_runs() if r.get("vm_name")}
    out = [
        {
            "name": m.name,
            "status": getattr(m, "status", None),
            "role": "run" if runner.is_ours(m.name) else "other",
            "orphan": runner.is_ours(m.name) and m.name not in active,
        }
        for m in machines
    ]
    out.sort(key=lambda m: (m["role"] == "other", m["name"]))
    return out


@app.post("/api/reconcile")
async def api_reconcile():
    return JSONResponse(await runner.reconcile())


@app.get("/healthz")
async def healthz():
    return {"ok": True, "configured": not settings.missing()}


# --------------------------------------------------------------------------- static SPA

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
