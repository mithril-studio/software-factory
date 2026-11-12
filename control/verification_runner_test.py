"""Exercise real routing/storage with simulated verifier outcomes; no cloud or model calls."""
import asyncio
import base64
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml
import httpx

from . import db, runner, verification as v, verification_runner as vr
from .config import settings

ISSUE = {"number": 77, "title": "Complete lesson", "body": """## Acceptance criteria
```yaml
- id: AC1
  mode: probe
  statement: Completing the lesson awards 10 points.
```
"""}
CONFIG = {"version": 1, "max_retries": 1, "e2e": {
    "required": True, "setup": "true", "reset": "true", "start": "npm start",
    "cleanup": "true", "ready_url": "http://127.0.0.1:3100",
    "flows": [{"id": "lesson", "start": "/", "task": "Complete the lesson.", "criteria": ["AC1"]}]}}
PLAN = v.freeze("## Verification\n```yaml\n" + yaml.safe_dump(CONFIG) + "```\n", ISSUE, 2)
REPO, BRANCH, PR = "owner/app", "factory/issue-77", "https://github.com/owner/app/pull/1"


class Log:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(line)


def verdict(points):
    return {"checks": [{"id": "c1", "verdict": "pass" if points == 10 else "fail",
                         "steps": [1], "reason": f"The page shows {points} points."}]}


def recording(path, points):
    (path / "lesson" / "steps").mkdir(parents=True)
    (path / "review.json").write_text("{}")
    (path / "worker.json").write_text(json.dumps({"flows": {"lesson": {"exit_code": 0}}}))
    (path / "lesson" / "verdict.json").write_text(json.dumps(verdict(points)))
    (path / "lesson" / "steps.jsonl").write_text('{"n": 1}\n')
    (path / "lesson" / "steps" / "01.jpg").write_bytes(b"simulated-image")


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = {key: getattr(settings, key) for key in ("db_path", "log_dir", "auto_merge", "merge_require_checks")}
        object.__setattr__(settings, "db_path", root / "runs.db")
        object.__setattr__(settings, "log_dir", root)
        object.__setattr__(settings, "auto_merge", True)
        try:
            await db.init()
            for run_id in ("review1", "review2", "retry", "stale"):
                await db.create_run(id=run_id, repo=REPO, issue_number=77, status="running",
                                    kind="review", created_at=db.utcnow(), verification_plan=json.dumps(PLAN))
            log = Log()
            good_review = {"verdict": "approve", "criteria": [{"id": "AC1", "status": "met",
                                                               "evidence": "unit test passed"}]}
            observed = []

            async def simulated(run_id, repo, plan, branch, pr, log, cycle, sha, attempt):
                observed.append((run_id, sha, attempt))
                path = root / f"{run_id}-verification" / str(attempt)
                recording(path, 0 if run_id == "review1" else 10)
                rows = v.review_results(plan, sha, attempt, good_review)
                rows += vr.read_e2e(plan, sha, attempt, path)
                if run_id == "retry" and attempt == 1:
                    rows = [{**r, "outcome": "error", "reason": "model timeout"} for r in rows]
                return rows, str(path)

            with patch.object(vr, "attempt_run", side_effect=simulated), \
                 patch.object(runner.github, "pr_head_sha", new=AsyncMock(return_value="buggy")) as head, \
                 patch.object(runner.github, "get_issue", new=AsyncMock(return_value=ISSUE)), \
                 patch.object(runner.github, "default_branch", new=AsyncMock(return_value="main")), \
                 patch.object(runner, "_guarded", new=AsyncMock()) as builder, \
                 patch.object(runner, "_mirror_issue", new=AsyncMock()), \
                 patch.object(runner, "_merge", new=AsyncMock(return_value=runner.MergeAttempt(True, None, "merged"))) as merge:
                await vr.execute("review1", REPO, PLAN, BRANCH, PR, log, 1)
                saved = await db.get_run("review1")
                receipt = json.loads(saved["verification"])
                assert receipt["decision"]["action"] == "request_fixes"
                assert not merge.called
                child = next(r for r in await db.list_runs() if r["kind"] == "build")
                assert json.loads(child["verification_plan"]) == PLAN
                assert child["cycle"] == 2
                assert "0 points" in builder.call_args.args[6]
                # Replaying the persisted decision must not create a second fix task/row.
                await vr.route("review1", REPO, PLAN, BRANCH, PR, log, 1, receipt)
                assert len([r for r in await db.list_runs() if r["kind"] == "build"]) == 1
                assert builder.call_count == 1
                # Verify evidence really gets copied into the next VM, with bounded command sizes.
                transport = SimpleNamespace(machines=SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(exit_code=0))))
                await vr.transfer_evidence(transport, "fix-vm", "review1")
                assert transport.machines.exec.call_count >= 3
                assert all(len(call.args[1]) < 50000 for call in transport.machines.exec.call_args_list)
                head.return_value = "fixed"
                await vr.execute("review2", REPO, PLAN, BRANCH, PR, log, 2)
                assert json.loads((await db.get_run("review2"))["verification"])["decision"]["action"] == "eligible_to_merge"
                assert merge.call_args.kwargs["expected_sha"] == "fixed"
                assert observed[:2] == [("review1", "buggy", 1), ("review2", "fixed", 1)]
                await vr.execute("retry", REPO, PLAN, BRANCH, PR, log, 1)
                assert [x[2] for x in observed if x[0] == "retry"] == [1, 2]
                # A new push between verdict and routing invalidates the result.
                receipt = json.loads((await db.get_run("review2"))["verification"])
                head.return_value = "new-push"
                before = merge.call_count
                await vr.route("stale", REPO, PLAN, BRANCH, PR, log, 1, receipt)
                assert merge.call_count == before
                assert json.loads((await db.get_run("stale"))["verification"])["decision"]["reason"] == "head_changed"

            # Exact-commit enforcement in the real merger, even with CI disabled.
            object.__setattr__(settings, "merge_require_checks", False)
            with patch.object(runner.github, "pr_head_sha", new=AsyncMock(return_value="fixed")), \
                 patch.object(runner.github, "merge_pr", new=AsyncMock()) as merge_api:
                outcome = await runner._merge(REPO, PR, "main", log, issue=ISSUE, branch=BRANCH, expected_sha="fixed")
                assert outcome.merged and merge_api.call_args.kwargs["sha"] == "fixed"
                merge_api.reset_mock()
                outcome = await runner._merge(REPO, PR, "main", log, issue=ISSUE, branch=BRANCH, expected_sha="old")
                assert not outcome.merged and not merge_api.called

            # Exercise actual VM orchestration, adapter, artifact transport, and teardown order.
            await db.create_run(id="vmtest", repo=REPO, issue_number=77, status="running",
                                created_at=db.utcnow(), verification_plan=json.dumps(PLAN))
            sample = root / "sample"
            recording(sample, 10)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for file in sample.rglob("*"):
                    if file.is_file():
                        tar.add(file, arcname=str(file.relative_to(sample)))
            order = []

            async def remote_exec(machine_id, command, **kwargs):
                if "FACTORY-EXACT-COMMIT" in command:
                    return SimpleNamespace(stdout="FACTORY-EXACT-COMMIT\n")
                order.append("collect")
                return SimpleNamespace(stdout=base64.b64encode(buf.getvalue()).decode())

            async def stream(*args, **kwargs):
                order.append("stream")
                if len([x for x in order if x == "stream"]) == 1:
                    assert 'git checkout --detach "$FACTORY_VERIFY_SHA"' in kwargs["script"]
                return 0, {"cost_usd": 0.1}, {}

            async def reap(*args, **kwargs):
                order.append("reap")
                assert (root / "vmtest-verification" / "1" / "lesson" / "steps" / "01.jpg").exists()

            transport = SimpleNamespace(machines=SimpleNamespace(exec=remote_exec,
                wait_until_ready=AsyncMock()), close=AsyncMock())
            with patch.object(runner, "client", return_value=transport), \
                 patch.object(runner, "headroom", new=AsyncMock()), \
                 patch.object(runner, "source_for", new=AsyncMock(return_value="golden")), \
                 patch.object(runner, "_provision", new=AsyncMock(return_value=SimpleNamespace(id="vm", name="review-vm"))), \
                 patch.object(runner, "_stream", side_effect=stream), \
                 patch.object(runner, "_read_verdict", new=AsyncMock(return_value=good_review)), \
                 patch.object(runner, "_salvage_transcript", new=AsyncMock()), \
                 patch.object(runner, "reap", side_effect=reap), \
                 patch.object(runner.github, "default_branch", new=AsyncMock(return_value="main")), \
                 patch.object(runner.github, "merge_base_sha", new=AsyncMock(return_value="base")):
                rows, path = await vr.attempt_run("vmtest", REPO, PLAN, BRANCH, PR, log, 1, "fixed", 1)
                assert v.decide(PLAN, "fixed", rows)["action"] == "eligible_to_merge"
                assert order.index("collect") < order.index("reap")
                assert json.loads((Path(path) / "results.json").read_text()) == rows

            # Cancellation/restart leaves a durable explanation, never replays dispatch.
            fake = SimpleNamespace(machines=SimpleNamespace(list=AsyncMock(return_value=[])), close=AsyncMock())
            with patch.object(runner, "client", return_value=fake), patch.object(runner, "_mirror_issue", new=AsyncMock()):
                await runner.reconcile()
            assert json.loads((await db.get_run("retry"))["verification"])["decision"]["reason"] == "controller_interrupted"

            from .app import app
            from . import auth
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as web:
                url = "/api/runs/review1/verification/receipt.json"
                assert (await web.get(url)).status_code == 401
                web.cookies.set(auth.COOKIE_NAME, auth.issue_token())
                response = await web.get(url)
                assert response.status_code == 200
                assert "attachment" in response.headers["content-disposition"]
                assert response.headers["x-content-type-options"] == "nosniff"
                (root / "private.txt").write_text("outside the recording")
                (root / "review1-verification" / "link.txt").symlink_to(root / "private.txt")
                assert (await web.get("/api/runs/review1/verification/link.txt")).status_code == 404
                assert (await web.get("/api/runs/review1/verification/%2e%2e%2fprivate.txt")).status_code == 404
        finally:
            for key, value in old.items():
                object.__setattr__(settings, key, value)


def archives():
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("../escape", "/absolute"):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                info = tarfile.TarInfo(name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
            try:
                vr.unpack(base64.b64encode(buf.getvalue()).decode(), Path(tmp))
                raise AssertionError("unsafe archive accepted")
            except ValueError:
                pass


asyncio.run(main())
archives()
print("planted failure -> idempotent fix dispatch -> fresh commit pass: ALL PASS")
print("bounded retry, stale commit, restart, evidence transfer and archive checks: ALL PASS")
