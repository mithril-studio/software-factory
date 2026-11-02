"""A deploy either lands the release healthy or leaves the test server as it was.

The test server is the one machine in this system that persists and that people use, so a
deploy that half-happens is worse than one that never starts: a checkout at the new commit
serving the old build, or a service restarted onto code that does not boot. The script's whole
contract is "deployed and healthy, or rolled back and healthy, or say loudly that it is
neither" — and that is only worth anything if it holds against a real shell and real git, so
most of this file runs `DEPLOY_SCRIPT` under `/bin/sh` against throwaway repositories, with
`systemctl` and `sudo` replaced by shims and the health URL a `file://` path.

Two properties are about the factory rather than the script. A deploy must never delete the
machine it deploys to (`runner.cancel` deletes a run's VM), and the reconciler must never mistake
the test server for an orphaned run VM.

Run it directly, no framework needed:

    .venv/bin/python -m control.deploy_test
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from control import db, deploy, github, preflight, runner
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def run(coro):
    return asyncio.run(coro)


# ---------- the facts a repo writes

PROFILE = """# acme/api

## Setup
`npm ci`

## Deploy

- machine: test-acme-api
- path: /home/boxd/app
- install: `npm ci`
- build: `npm run build`
- `migrate`: `npm run db:migrate -- --yes`
- service: acme-api
- health: http://localhost:3000/healthz
- url: https://test-acme-api.boxd.sh
- machine: ignored-second-value
- colour: blue

## Verify
- path: not/this/one
"""
facts = preflight.deploy_facts(PROFILE)
check("every deploy key is read", sorted(facts), sorted(preflight.DEPLOY_KEYS))
check("backticks are unwrapped", facts["build"], "npm run build")
check("a value may carry colons", facts["health"], "http://localhost:3000/healthz")
check("the first value of a key wins", facts["machine"], "test-acme-api")
check("the section ends at the next heading", facts["path"], "/home/boxd/app")
check("unknown keys are ignored", "colour" in facts, False)
check("a complete section has no problem", preflight.deploy_problem(facts), None)
check("no section, no facts", preflight.deploy_facts("## Setup\n`npm ci`\n"), {})
partial = {k: v for k, v in facts.items() if k not in ("health", "migrate")}
check("a missing required key is named", "health" in (preflight.deploy_problem(partial) or ""), True)
check("migrate is optional", preflight.missing_deploy_facts({k: v for k, v in facts.items() if k != "migrate"}), [])
check("a machine with a run prefix is refused — the reconciler would reap it",
      "run prefix" in (preflight.deploy_problem({**facts, "machine": "run-acme"}) or ""), True)
check("preflight warns, never blocks",
      all(not c.fatal for c in preflight.deploy_checks(PROFILE.replace("- health:", "- x:"))), True)
object.__setattr__(settings, "deploy_enabled", False)
check("with deploys off, a repo without a section is not nagged", preflight.deploy_checks("## Setup"), [])

# ---------- the exit codes

for code in (deploy.OK, deploy.REFUSED, deploy.BUILD_FAILED, deploy.UNHEALTHY, deploy.ROLLBACK_FAILED):
    check(f"exit {code} has a meaning of its own", "exited" in deploy.deploy_outcome(code)[1], False)
check("only 0 is deployed", [c for c in range(256) if deploy.deploy_outcome(c)[0]], [0])
check("an unknown code says what it was", deploy.deploy_outcome(3), (False, "the deploy script exited 3"))

# ---------- the script's shape

check("the script only ever fast-forwards", "git merge --ff-only \"$DEPLOY_SHA\"" in deploy.DEPLOY_SCRIPT, True)
check("the rollback resets to where it started", "git reset --hard \"$BEFORE\"" in deploy.DEPLOY_SCRIPT, True)
check("the script never pushes", "git push" in deploy.DEPLOY_SCRIPT, False)
check("deploys claim no issue and do not block the queue", "deploy" in db.UNCLAIMED_KINDS, True)
check("the test server is not a run VM to the reconciler", runner.is_run_vm("test-acme-api"), False)
check("cancel never deletes a deploy's machine", "deploy" in runner.PERSISTENT_VM_KINDS, True)
check("and checks the kind before deleting",
      "PERSISTENT_VM_KINDS" in __import__("inspect").getsource(runner.cancel), True)

# ---------- the script, for real

if not (shutil.which("git") and shutil.which("curl") and Path("/bin/sh").exists()):
    print("skip the shell scenarios: git, curl or /bin/sh missing")
else:
    tmp = Path(tempfile.mkdtemp(prefix="deploy-test-"))
    shims = tmp / "bin"
    shims.mkdir()
    (shims / "sudo").write_text('#!/bin/sh\n[ "$1" = "-n" ] && shift\nexec "$@"\n')
    (shims / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {tmp}/systemctl.log\n')
    for shim in shims.iterdir():
        shim.chmod(0o755)
    env_base = {
        **os.environ,
        "PATH": f"{shims}:{os.environ['PATH']}",
        "HOME": str(tmp),
        "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, env=env_base, check=True,
                              capture_output=True, text=True).stdout.strip()

    def scenario(name, *, build="true", health_in_new=True, dirty=False, branch="test",
                 diverge=False, health_wait="2", install=None, hold_lock=False):
        """A fresh origin with A then B on `test`, and a server checkout sitting at A."""
        root = tmp / name
        origin, work, server = root / "origin.git", root / "work", root / "server"
        root.mkdir()
        git("init", "-q", "--bare", "-b", "test", str(origin), cwd=root)
        git("clone", "-q", str(origin), str(work), cwd=root)
        (work / "health.txt").write_text("ok\n")
        (work / "app.txt").write_text("A\n")
        git("add", "-A", cwd=work)
        git("commit", "-qm", "A", cwd=work)
        git("push", "-q", "origin", "HEAD:test", cwd=work)
        a = git("rev-parse", "HEAD", cwd=work)
        git("clone", "-q", "-b", "test", str(origin), str(server), cwd=root)
        (work / "app.txt").write_text("B\n")
        if not health_in_new:
            (work / "health.txt").unlink()
        git("add", "-A", cwd=work)
        git("commit", "-qm", "B", cwd=work)
        git("push", "-q", "origin", "HEAD:test", cwd=work)
        b = git("rev-parse", "HEAD", cwd=work)
        if dirty:
            (server / "app.txt").write_text("edited by hand\n")
        if branch != "test":
            git("checkout", "-qb", branch, cwd=server)
        if diverge:
            (server / "local.txt").write_text("x\n")
            git("add", "-A", cwd=server)
            git("commit", "-qm", "local", cwd=server)
        env = {
            **env_base,
            **deploy.deploy_env(b, {
                "path": str(server), "install": install or f"echo install >> {root}/steps",
                "build": build, "service": "acme-api",
                "health": f"file://{server}/health.txt",
            }),
            "DEPLOY_HEALTH_WAIT": health_wait,
        }
        env["TMPDIR"] = str(root)
        holder = None
        if hold_lock:
            holder = subprocess.Popen(["flock", str(root / "factory-deploy.lock"), "sleep", "30"])
            import time
            time.sleep(0.3)
        proc = subprocess.run(["/bin/sh", "-c", deploy.DEPLOY_SCRIPT], env=env, capture_output=True,
                              text=True, timeout=120)
        if holder:
            holder.kill()
        head = git("rev-parse", "HEAD", cwd=server)
        return proc, head, a, b, root

    proc, head, a, b, root = scenario("happy")
    check("a clean deploy exits 0", proc.returncode, 0)
    check("and the checkout is at the release", head, b)
    check("the service was restarted", "restart acme-api" in (tmp / "systemctl.log").read_text(), True)
    check("every step announced itself", proc.stdout.count("FACTORY-DEPLOY:") >= 6, True)

    # Fails on B only: the rollback rebuilds A with the same command, and that must succeed.
    proc, head, a, b, _ = scenario("build-fails", build="grep -qx A app.txt")
    check("a failing build exits 96", proc.returncode, deploy.BUILD_FAILED)
    check("and the checkout is back where it was", head, a)

    proc, head, a, b, _ = scenario("unhealthy", health_in_new=False)
    check("a release that does not come up healthy exits 97", proc.returncode, deploy.UNHEALTHY)
    check("and is rolled back to the version that does", head, a)

    proc, head, a, b, _ = scenario("both-broken", build="test -f never-there")
    check("a rollback that cannot rebuild either exits 98", proc.returncode, deploy.ROLLBACK_FAILED)

    proc, head, a, b, _ = scenario("dirty", dirty=True)
    check("a hand-edited checkout is refused with 95", proc.returncode, deploy.REFUSED)
    check("and left exactly as it was", head, a)

    proc, head, a, b, _ = scenario("wrong-branch", branch="hotfix")
    check("a checkout on the wrong branch is refused", proc.returncode, deploy.REFUSED)

    proc, head, a, b, _ = scenario("diverged", diverge=True)
    check("a checkout that cannot fast-forward is refused", proc.returncode, deploy.REFUSED)
    check("and not reset", head != a and head != b, True)

    # Found in review: every step was eval'd in one shell, so `cd web && npm ci` moved the
    # build into web/web, and the rollback failed the same way — exit 98 on a healthy app.
    proc, head, a, b, _ = scenario(
        "cd-in-steps", install="mkdir -p sub && cd sub && touch installed",
        build="cd sub && test -f installed")
    check("a step that changes directory does not move the next one", proc.returncode, 0)

    if shutil.which("flock"):
        proc, head, a, b, _ = scenario("locked", hold_lock=True)
        check("a deploy still running on the machine refuses a second one",
              (proc.returncode, head), (deploy.REFUSED, a))

    shutil.rmtree(tmp, ignore_errors=True)

# ---------- the factory side: create refuses, and both endings land on the release

dbdir = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(dbdir.name) / "factory.db")
object.__setattr__(settings, "log_dir", Path(dbdir.name) / "logs")
settings.log_dir.mkdir(parents=True, exist_ok=True)
run(db.init())

profile_text = {"value": PROFILE}


async def fake_file(repo, path, ref, strict=False):
    return profile_text["value"]


github.file = fake_file


def promoted_release():
    async def go():
        r = await db.create_release("acme/api", test_sha="b" * 40)
        await db.transition_release(r["id"], "promoted", promoted_at=db.utcnow())
        return r["id"]
    return run(go())


rid = promoted_release()
profile_text["value"] = "## Setup\n`npm ci`\n"
try:
    run(deploy.create(rid))
    refused = None
except ValueError as exc:
    refused = str(exc)
check("a repo with no ## Deploy is refused", "missing" in (refused or ""), True)
row = run(db.get_release(rid))
check("the release stays promoted", row["status"], "promoted")
check("with the reason noted, so the schedule does not retry every tick",
      "not deployed" in (row["error"] or ""), True)


# Found in review: a 502 reading the profile used to read as "no ## Deploy section", which is
# noted on the release and never retried.
async def flaky_file(repo, path, ref, strict=False):
    raise RuntimeError("502 Bad Gateway")


github.file = flaky_file
outage_rid = promoted_release()
try:
    run(deploy.create(outage_rid))
    outage = None
except ValueError:
    outage = "refused"
except RuntimeError:
    outage = "raised"
check("a GitHub outage raises rather than refusing", outage, "raised")
check("and notes nothing, so the next tick tries again", run(db.get_release(outage_rid))["error"], None)
check("the release is still promoted", run(db.get_release(outage_rid))["status"], "promoted")
github.file = fake_file
run(db.transition_release(outage_rid, "reviewed"))


class FakeMachine:
    def __init__(self, name, status="running"):
        self.name, self.status, self.id = name, status, f"id-{name}"


class FakeMachines:
    def __init__(self, fleet):
        self.fleet = fleet

    async def list(self):
        return self.fleet


class FakeBoxd:
    def __init__(self, fleet):
        self.machines = FakeMachines(fleet)

    async def close(self):
        pass


exit_code = {"value": 0}


async def fake_stream(boxd, machine_id, script, env, run_log):
    run_log.write(f"FACTORY-DEPLOY: pretending on {machine_id}")
    return exit_code["value"]


deploy.provision._stream = fake_stream
fleet = [FakeMachine("test-acme-api")]
runner.client = lambda: FakeBoxd(fleet)
profile_text["value"] = PROFILE


async def deploy_and_wait(release_id):
    run_id = await deploy.create(release_id)
    while runner._tasks.get(run_id):
        await asyncio.sleep(0.01)
    return run_id


run_id = run(deploy_and_wait(rid))
row = run(db.get_release(rid))
check("a successful deploy marks the release deployed", row["status"], "deployed")
check("with the sha and the test URL", (row["deployed_sha"], row["test_url"]),
      ("b" * 40, "https://test-acme-api.boxd.sh"))
deploy_run = run(db.get_run(run_id))
check("the deploy is a run of its own kind", (deploy_run["kind"], deploy_run["status"]), ("deploy", "succeeded"))
check("naming the test server", deploy_run["vm_name"], "test-acme-api")
check("but holding no vm id that cancel could delete", deploy_run["vm_id"], None)

rid = promoted_release()
exit_code["value"] = deploy.UNHEALTHY
run(deploy_and_wait(rid))
row = run(db.get_release(rid))
check("a rolled-back deploy fails the release", row["status"], "failed")
check("and says it was rolled back", "rolled back" in (row["error"] or ""), True)

rid = promoted_release()
fleet[0] = FakeMachine("test-acme-api", status="stopped")
run(deploy_and_wait(rid))
row = run(db.get_release(rid))
check("a stopped test server is not started; the release fails", row["status"], "failed")
check("saying so", "never starts" in (row["error"] or ""), True)

rid = promoted_release()
fleet.clear()
run(deploy_and_wait(rid))
check("a missing test server fails the release", "never creates" in (run(db.get_release(rid))["error"] or ""), True)

# Found in review: a failure after the release moved to `deploying` but before the task
# existed left it there, freezing the repo until a restart.
rid = promoted_release()
fleet.append(FakeMachine("test-acme-api"))
real_create_run = db.create_run


async def broken_create_run(**fields):
    raise RuntimeError("disk full")


db.create_run = broken_create_run
try:
    run(deploy.create(rid))
except RuntimeError:
    pass
db.create_run = real_create_run
check("a deploy that cannot start fails its release instead of leaving it deploying",
      run(db.get_release(rid))["status"], "failed")

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
