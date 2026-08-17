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
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from telemetry import store as telemetry

from . import auth, db, github, poller, runner
from .config import ROOT, settings

DIST = ROOT / "web" / "dist"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await telemetry.init()
    poller.start()
    try:
        yield
    finally:
        await poller.stop()


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


@app.get("/api/config")
async def api_config():
    """What the shell needs: watched repos, the golden, limits, and any missing settings."""
    return {
        "repos": list(settings.repos),
        "golden": settings.golden,
        "max_concurrent": settings.max_concurrent,
        "max_attempts": settings.max_attempts,
        "poll_enabled": settings.poll_enabled,
        "poll_interval": settings.poll_interval,
        "missing": settings.missing(),
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
    }


class StartRun(BaseModel):
    repo: str
    issue_number: int
    golden: str | None = None


@app.post("/api/runs")
async def api_start_run(body: StartRun):
    gaps = settings.missing()
    if gaps:
        raise HTTPException(400, f"configuration incomplete: {', '.join(gaps)}")
    try:
        run_id = await runner.create(body.repo.strip(), body.issue_number, golden=body.golden)
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
    repos = [repo] if repo else list(settings.repos)
    out: list[dict] = []
    for r in repos:
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


@app.get("/api/projects")
async def api_projects():
    """Watched repos with their run tallies."""
    stats = await db.stats_by_repo()
    out = []
    for repo in settings.repos:
        s = stats.get(repo, {})
        out.append(
            {
                "repo": repo,
                "runs": s.get("runs", 0) or 0,
                "succeeded": s.get("succeeded", 0) or 0,
                "failed": s.get("failed", 0) or 0,
                "active": s.get("active", 0) or 0,
                "last_run": s.get("last_run"),
            }
        )
    return out


# --------------------------------------------------------------------------- agents / fleet


@app.get("/api/agents")
async def api_agents():
    """Boxd machines: the golden(s) runs fork from, and any live run VMs."""
    boxd = runner.client()
    try:
        machines = await boxd.machines.list()
    finally:
        await boxd.close()
    active = {r["vm_name"] for r in await db.active_runs() if r.get("vm_name")}
    out = []
    for m in machines:
        is_run = m.name.startswith("run-")
        out.append(
            {
                "name": m.name,
                "status": getattr(m, "status", None),
                "role": "run" if is_run else "golden",
                "is_golden": m.name == settings.golden,
                "orphan": is_run and m.name not in active,
            }
        )
    out.sort(key=lambda a: (a["role"] != "golden", a["name"]))
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
