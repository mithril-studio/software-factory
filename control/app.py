"""FastAPI app: the UI and the API are the same process.

Server-rendered HTML, plain fetch for polling, Server-Sent Events for live agent output.
No build step, no frontend dependency, no separate deploy — and every endpoint that renders
a page also answers JSON for scripting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import db, github, poller, runner
from .config import settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    poller.start()
    try:
        yield
    finally:
        await poller.stop()


app = FastAPI(title="software factory", lifespan=lifespan)


def _duration(run: dict) -> str:
    import datetime as dt

    start, end = run.get("started_at"), run.get("finished_at")
    if not start:
        return "—"
    try:
        begin = dt.datetime.fromisoformat(start)
        stop = dt.datetime.fromisoformat(end) if end else dt.datetime.now(dt.timezone.utc)
        total = int((stop - begin).total_seconds())
    except (TypeError, ValueError):
        return "—"
    return f"{total // 60}m {total % 60}s" if total >= 60 else f"{total}s"


templates.env.filters["duration"] = _duration


# --------------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    runs = await db.list_runs()
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "runs": runs,
            "missing": settings.missing(),
            "golden": settings.golden,
            "active": sum(1 for r in runs if r["status"] not in db.TERMINAL),
            "max_concurrent": settings.max_concurrent,
            "watching": settings.repos if settings.poll_enabled else (),
            "poll_interval": settings.poll_interval,
        },
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: str):
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "run": run})


@app.get("/runs/{run_id}/card", response_class=HTMLResponse)
async def run_card(request: Request, run_id: str):
    """The run detail page polls this for status without disturbing the log stream."""
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    return templates.TemplateResponse(
        request,
        "_card.html",
        {
            "run": run})


@app.get("/machines", response_class=HTMLResponse)
async def machines_page(request: Request):
    boxd = runner.client()
    try:
        machines = await boxd.machines.list()
    finally:
        await boxd.close()
    active = {r["vm_name"] for r in await db.active_runs() if r.get("vm_name")}
    rows = [
        {
            "name": m.name,
            "status": m.status,
            "is_run": m.name.startswith("run-"),
            "orphan": m.name.startswith("run-") and m.name not in active,
        }
        for m in machines
    ]
    return templates.TemplateResponse(
        request,
        "machines.html",
        {
            "machines": rows, "quota": len(rows)}
    )


# --------------------------------------------------------------------------- actions


@app.post("/runs")
async def start_run(repo: str = Form(...), issue_number: int = Form(...)):
    gaps = settings.missing()
    if gaps:
        raise HTTPException(400, f"configuration incomplete: {', '.join(gaps)}")
    try:
        run_id = await runner.create(repo.strip(), issue_number)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not start run: {exc}") from exc
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    await runner.cancel(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/reconcile")
async def do_reconcile():
    return JSONResponse(await runner.reconcile())


# --------------------------------------------------------------------------- streams


@app.get("/runs/{run_id}/stream")
async def stream_log(run_id: str):
    """Tail the run log over SSE until the run reaches a terminal state."""
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


# --------------------------------------------------------------------------- json api


@app.get("/api/runs")
async def api_runs():
    return await db.list_runs()


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str):
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    return run


@app.get("/api/issues")
async def api_issues(repo: str):
    """Backs the issue picker in the new-run form."""
    try:
        return await github.list_open_issues(repo)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.get("/healthz")
async def healthz():
    return {"ok": True, "configured": not settings.missing()}
