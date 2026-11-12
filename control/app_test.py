"""The HTTP surface: who may call it, from where, and what starting a run by hand does.

Driven through FastAPI's TestClient without entering the lifespan, so nothing polls, no VM is
touched and GitHub is never called. The database is a throwaway file.

Run it directly, no framework needed:

    .venv/bin/python -m control.app_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from control import app as app_module
from control import auth, db, repos, runner
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "boxd_api_key", "test-key")
object.__setattr__(settings, "github_token", "test-token")
asyncio.run(db.init())
asyncio.run(repos.add("acme/api"))

auth.AUTH_EMAIL, auth.AUTH_PASSWORD = "me@example.com", "secret"
client = TestClient(app_module.app, base_url="https://factory.test")
COOKIE = {auth.COOKIE_NAME: auth.issue_token()}

# ---------- the session gate
check("auth: /api without a session is 401", client.get("/api/runs").status_code, 401)
check("auth: a forged cookie is 401",
      client.get("/api/runs", cookies={auth.COOKIE_NAME: "1.abc"}).status_code, 401)
check("auth: a valid session gets through", client.get("/api/runs", cookies=COOKIE).status_code, 200)
check("auth: /healthz is public", client.get("/healthz").status_code, 200)

# With no login configured the signing key is derivable from public source, so a cookie signed
# with it must open nothing.
auth.AUTH_PASSWORD = ""
check("auth: an unconfigured deployment accepts no token, even a correctly signed one",
      client.get("/api/runs", cookies=COOKIE).status_code, 401)
auth.AUTH_PASSWORD = "secret"

# ---------- cross-site writes
# Every *.boxd.sh machine is "same site" to the cookie, so SameSite=Lax alone lets a page on a
# sibling machine (including the test server the agents deploy to) POST with our session.
evil = {"Origin": "https://test-acme.boxd.sh", "Sec-Fetch-Site": "same-site"}
check("csrf: a sibling-site POST is refused even with a session",
      client.post("/api/reconcile", cookies=COOKIE, headers=evil).status_code, 403)
check("csrf: a foreign Origin alone is refused",
      client.post("/api/reconcile", cookies=COOKIE,
                  headers={"Origin": "https://evil.example"}).status_code, 403)
check("csrf: so is login, so a session cannot be planted",
      client.post("/api/login", json={"email": "a", "password": "b"}, headers=evil).status_code, 403)
check("csrf: a cross-site GET is still allowed (it changes nothing)",
      client.get("/api/runs", cookies=COOKIE, headers=evil).status_code, 200)

calls = []


async def fake_create(repo, number):
    calls.append((repo, number))
    if number == 99:
        raise runner.Busy(f"{repo} already has a run in flight")
    return "run-1"


real_create, runner.create = runner.create, fake_create
try:
    same = {"Origin": "https://factory.test", "Sec-Fetch-Site": "same-origin"}
    resp = client.post("/api/runs", cookies=COOKIE, headers=same,
                       json={"repo": "acme/api", "issue_number": 7})
    check("start: a same-origin request starts a run", (resp.status_code, resp.json()),
          (200, {"run_id": "run-1"}))
    resp = client.post("/api/runs", cookies=COOKIE, json={"repo": "acme/api", "issue_number": 7})
    check("start: a client sending no browser headers (curl) is not a browser, so it passes",
          resp.status_code, 200)
    resp = client.post("/api/runs", cookies=COOKIE, json={"repo": "acme/api", "issue_number": 99})
    check("start: a busy repo is a 409 that says so",
          (resp.status_code, "in flight" in resp.text), (409, True))
    resp = client.post("/api/runs", cookies=COOKIE, json={"repo": "other/repo", "issue_number": 1})
    check("start: an unwatched repo is refused before anything starts",
          (resp.status_code, len(calls)), (400, 3))
finally:
    runner.create = real_create

# ---------- the queue: a GitHub failure is an error, and queueing is a label
from control import github  # noqa: E402

labelled = []


async def broken_plan(repo, limit=100):
    raise OSError("Temporary failure in name resolution")


async def fake_add_labels(repo, number, labels):
    labelled.append((repo, number, labels))


real_plan, real_add = github.plan, github.add_labels
github.plan, github.add_labels = broken_plan, fake_add_labels
try:
    resp = client.get("/api/plan?repo=acme/api", cookies=COOKIE)
    check("queue: an unreadable repo is a 502, not an empty queue",
          (resp.status_code, "name resolution" in resp.text), (502, True))
    resp = client.post("/api/plan/queue", cookies=COOKIE, json={"repo": "acme/api", "number": 5})
    check("queue: queueing labels the issue agent:queued",
          (resp.status_code, labelled), (200, [("acme/api", 5, [github.LABEL_QUEUED])]))
    resp = client.post("/api/plan/queue", cookies=COOKIE, json={"repo": "other/repo", "number": 1})
    check("queue: an unwatched repo is refused and nothing is labelled",
          (resp.status_code, len(labelled)), (400, 1))
finally:
    github.plan, github.add_labels = real_plan, real_add

# ---------- the log stream resumes instead of replaying
log_path = Path(tmp.name) / "r1.log"
log_path.write_text("one\ntwo\nthree\n")
asyncio.run(db.claim_run(id="r1", repo="acme/web", issue_number=1, status="queued",
                         log_path=str(log_path), created_at=db.utcnow()))
asyncio.run(db.finish_run("r1", "succeeded"))


def stream(last_id=None):
    headers = {"Last-Event-ID": str(last_id)} if last_id is not None else {}
    body = client.get("/api/runs/r1/stream", cookies=COOKIE, headers=headers).text
    data = [ln[6:] for ln in body.splitlines() if ln.startswith("data: ") and ln != "data: succeeded"]
    ids = [int(ln[4:]) for ln in body.splitlines() if ln.startswith("id: ")]
    return data, ids, body


data, ids, body = stream()
check("stream: every line arrives", data, ['"one"', '"two"', '"three"'])
check("stream: each carries the byte offset after it", ids, [4, 8, 14])
check("stream: and the run's end is announced", "event: done" in body)
data, _, _ = stream(last_id=4)
check("stream: a reconnect with Last-Event-ID resumes after it", data, ['"two"', '"three"'])
data, _, _ = stream(last_id="garbage")
check("stream: a malformed Last-Event-ID starts from the top", len(data), 3)

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
