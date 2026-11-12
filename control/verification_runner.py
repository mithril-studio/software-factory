"""Execute a frozen plan using the factory's existing VM and build dispatch machinery."""
import asyncio
import base64
import io
import json
from pathlib import Path
import shlex
import tarfile


from . import db, github, verification as policy
from .config import settings

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def python_command(source):
    encoded = base64.b64encode(source.encode()).decode()
    return "python3 -c " + shlex.quote(f"import base64; exec(base64.b64decode('{encoded}'))")


def unpack(encoded, destination):
    """Only regular recording files, bounded both compressed and expanded."""
    if len(encoded) > MAX_ARTIFACT_BYTES * 2:
        raise ValueError("recording archive too large")
    raw = base64.b64decode(encoded, validate=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 5000 or sum(m.size for m in members) > MAX_ARTIFACT_BYTES:
            raise ValueError("recording archive exceeds limits")
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not member.isfile():
                raise ValueError("unsafe recording archive member")
        for member in members:
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())


async def collect(boxd, machine, remote, local):
    source = f"""import base64, io, pathlib, tarfile
root = pathlib.Path({remote!r})
files = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()]
if sum(p.stat().st_size for p in files) > {MAX_ARTIFACT_BYTES}: raise RuntimeError('recording too large')
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode='w:gz') as tar:
    for p in files: tar.add(p, arcname=str(p.relative_to(root)), recursive=False)
print(base64.b64encode(buf.getvalue()).decode())
"""
    response = await boxd.machines.exec(machine.id, python_command(source), timeout=60)
    unpack((response.stdout or "").strip(), local)


def read_e2e(plan, sha, attempt, root):
    """The policy sees only verdicts backed by files successfully salvaged from the VM."""
    rows = []
    worker = json.loads((root / "worker.json").read_text())
    for flow in plan["e2e"]["flows"]:
        entry = worker.get("flows", {}).get(flow["id"], {})
        path = root / flow["id"]
        verdict, steps = None, set()
        error = worker.get("error") or entry.get("error")
        if not error:
            try:
                verdict = json.loads((path / "verdict.json").read_text())
                for line in (path / "steps.jsonl").read_text().splitlines():
                    step = json.loads(line)
                    n = step["n"]
                    if type(n) is int and (path / "steps" / f"{n:02}.jpg").is_file():
                        steps.add(n)
            except (OSError, ValueError, KeyError, TypeError):
                verdict = None
        rows.extend(policy.e2e_results(plan, sha, attempt, flow, verdict, steps))
        if error:
            for row in rows:
                if row["check_id"].startswith(f"e2e:{flow['id']}:"):
                    row["reason"] = str(error)
    return rows


async def persist(run_id, plan, results, decision, artifacts):
    receipt = {"schema": 1, "plan_version": plan["version"], "results": results,
               "decision": decision, "artifacts": artifacts}
    await db.update_run(run_id, verification=json.dumps(receipt))
    root = settings.log_dir / f"{run_id}-verification"
    root.mkdir(parents=True, exist_ok=True)
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2))


async def execute(run_id, plan, sha, boxd, machine, env, log):
    """One post-build check in the existing run; no dispatch, retry, merge or new run state."""
    from . import runner
    local = settings.log_dir / f"{run_id}-verification" / "1"
    local.mkdir(parents=True, exist_ok=True)
    remote = f"/tmp/factory-verification/{run_id}"
    rows = []
    initial = policy.decide(plan, sha, rows)
    await persist(run_id, plan, rows, initial, [str(local)])
    worker_env = {**env, "FACTORY_VERIFY_SHA": sha, "FACTORY_VERIFICATION_DIR": remote,
                  "FACTORY_VERIFICATION_PLAN": json.dumps(plan),
                  "FACTORY_VERIFY_CHECKOUT": f"/tmp/factory-verify-app/{run_id}"}
    worker = Path(__file__).with_name("verification_worker.py").read_text()
    script = r'''
set -e
source_dir="${FACTORY_WORKDIR:-$HOME/work}/${FACTORY_REPO##*/}"
git -C "$source_dir" fetch origin "$FACTORY_VERIFY_SHA"
git -C "$source_dir" worktree add --detach "$FACTORY_VERIFY_CHECKOUT" "$FACTORY_VERIFY_SHA"
cd "$FACTORY_VERIFY_CHECKOUT"
''' + runner.NODE_GUARD + "\n" + python_command(worker)
    problem = None
    try:
        code, _, _ = await asyncio.wait_for(
            runner._stream(boxd, machine.id, worker_env, log, run_id, script=script),
            timeout=min(settings.run_timeout, 1800))
        if code:
            problem = f"verification worker exited {code}"
    except Exception as exc:
        problem = str(exc) or type(exc).__name__
    finally:
        # Salvage partial recordings even after a timeout/cancellation, before runner reaps.
        try:
            await collect(boxd, machine, remote, local)
        except Exception as exc:
            problem = f"evidence collection failed: {exc}"
        if problem:
            rows = [policy.result(plan, sha, 1, c, "error", reason=problem) for c in plan["checks"]]
        else:
            try:
                rows = read_e2e(plan, sha, 1, local)
            except Exception as exc:
                rows = [policy.result(plan, sha, 1, c, "error", reason=str(exc)) for c in plan["checks"]]
        decision = policy.decide(plan, sha, rows, completed=True)
        await persist(run_id, plan, rows, decision, [str(local)])
    # Reject even a good result when the PR moved while the browser was testing.
    current = await github.branch_sha(env["FACTORY_REPO"], env["FACTORY_BRANCH"])
    if current != sha:
        decision = {**decision, "action": "escalate", "reason": "head_changed"}
        await persist(run_id, plan, rows, decision, [str(local)])
    log.write(f"[verification] {decision['action']}: {decision['reason']}")
    return decision
