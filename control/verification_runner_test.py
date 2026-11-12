"""Verify exact-commit browser evidence before teardown, without reviving merge/retry loops."""
import asyncio
import base64
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from . import db, github, runner, verification as v, verification_runner as vr
from .config import settings
from .verification_test import CONFIG, plan


def archive(points):
    files = {"worker.json": json.dumps({"flows": {"lesson": {"exit_code": 0}}}),
             "lesson/verdict.json": json.dumps({"checks": [{"id": "c1", "verdict": "pass" if points == 10 else "fail",
                "steps": [1], "reason": f"The page shows {points} points."}]}),
             "lesson/steps.jsonl": '{"n": 1}\n', "lesson/steps/01.jpg": "simulated image"}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return base64.b64encode(out.getvalue()).decode()


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = {k: getattr(settings, k) for k in ("db_path", "log_dir")}
        object.__setattr__(settings, "db_path", root / "runs.db")
        object.__setattr__(settings, "log_dir", root)
        try:
            await db.init()
            log = SimpleNamespace(write=lambda message: None)
            machine = SimpleNamespace(id="vm")
            for run_id, points, required, expected in [("bug", 0, True, "request_fixes"),
                    ("fixed", 10, True, "ready_for_review"), ("advisory", 0, False, "ready_for_review")]:
                frozen = json.loads(json.dumps(plan))
                frozen["checks"][0]["required"] = required
                await db.claim_run(id=run_id, repo=run_id, issue_number=1, status="running", created_at=db.utcnow())
                remote = SimpleNamespace(machines=SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(stdout=archive(points)))))
                env = {"FACTORY_REPO": "a/b", "FACTORY_BRANCH": "factory/issue-1"}
                with patch.object(runner, "_stream", new=AsyncMock(return_value=(0, {}, {}))) as stream, \
                     patch.object(github, "branch_sha", new=AsyncMock(return_value=run_id)):
                    decision = await vr.execute(run_id, frozen, run_id, remote, machine, env, log)
                assert decision["action"] == expected
                script = stream.call_args.kwargs["script"]
                assert 'worktree add --detach "$FACTORY_VERIFY_CHECKOUT" "$FACTORY_VERIFY_SHA"' in script
                assert (root / f"{run_id}-verification/1/lesson/steps/01.jpg").is_file()
                saved = json.loads((await db.get_run(run_id))["verification"])
                assert saved["decision"] == decision
                assert saved["results"][0]["commit"] == run_id
            # The worker must not run on a branch different from the collected result.
            await db.claim_run(id="stale", repo="stale", issue_number=1, status="running", created_at=db.utcnow())
            with patch.object(runner, "_stream", new=AsyncMock(return_value=(0, {}, {}))), \
                 patch.object(github, "branch_sha", new=AsyncMock(return_value="new-push")):
                decision = await vr.execute("stale", plan, "old", remote, machine, env, log)
            assert decision["action"] == "escalate" and decision["reason"] == "head_changed"
            await db.init()
            assert json.loads((await db.get_run("bug"))["verification"])["decision"]["action"] == "request_fixes"
            from .app import app
            from . import auth
            auth.AUTH_EMAIL, auth.AUTH_PASSWORD = "test@example.test", "test-only"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as web:
                url = "/api/runs/bug/verification/receipt.json"
                assert (await web.get(url)).status_code == 401
                web.cookies.set(auth.COOKIE_NAME, auth.issue_token())
                response = await web.get(url)
                assert response.status_code == 200 and "attachment" in response.headers["content-disposition"]
                (root / "private.txt").write_text("outside")
                (root / "bug-verification/link.txt").symlink_to(root / "private.txt")
                assert (await web.get("/api/runs/bug/verification/link.txt")).status_code == 404
        finally:
            for key, value in old.items():
                object.__setattr__(settings, key, value)


asyncio.run(main())
# Bad screenshot references can never turn into a pass.
raw = {"checks": [{"id": "c1", "verdict": "pass", "steps": [99]}]}
assert v.e2e_results(plan, "sha", 1, CONFIG["e2e"]["flows"][0], raw, {1})[0]["outcome"] == "inconclusive"
print("post-build failure/pass/advisory, stale-head, durable evidence and access checks: ALL PASS")
