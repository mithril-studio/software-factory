"""Execute a frozen plan using the factory's existing VM and build dispatch machinery."""
import asyncio
import base64
import io
import json
from pathlib import Path
import shlex
import tarfile

import yaml

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


async def transfer_evidence(boxd, machine_id, source_run):
    """Put the referenced evidence in the fix VM, not just server-only paths in a prompt."""
    saved = await db.get_run(source_run)
    receipt = json.loads(saved["verification"])
    root = (settings.log_dir / f"{source_run}-verification").resolve()
    files = {root / "receipt.json"}
    for row in receipt["results"]:
        for ref in row.get("evidence", []):
            file = (root / str(row["attempt"]) / ref).resolve()
            if not file.is_relative_to(root) or not file.is_file():
                raise ValueError("fix evidence is missing or outside the recording")
            files.add(file)
    if sum(p.stat().st_size for p in files) > 16 * 1024 * 1024:
        raise ValueError("fix evidence exceeds 16 MB; needs human review")
    for file in sorted(files):
        target = "/tmp/factory-evidence/" + str(file.relative_to(root))
        data = file.read_bytes()
        for offset in range(0, max(len(data), 1), 24000):
            chunk = base64.b64encode(data[offset:offset + 24000]).decode()
            mode = "wb" if offset == 0 else "ab"
            source = f"import pathlib, base64; p=pathlib.Path({target!r}); p.parent.mkdir(parents=True, exist_ok=True); p.open({mode!r}).write(base64.b64decode({chunk!r}))"
            response = await boxd.machines.exec(machine_id, python_command(source), timeout=30)
            if getattr(response, "exit_code", 0):
                raise RuntimeError("could not deliver evidence to fix VM")


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


async def interrupted(run_id, reason):
    saved = await db.get_run(run_id)
    plan = json.loads(saved.get("verification_plan") or "{}") if saved else {}
    if not plan.get("enabled"):
        return
    receipt = json.loads(saved.get("verification") or "{}")
    decision = {**receipt.get("decision", {}), "action": "escalate", "reason": reason,
                "plan_version": plan["version"]}
    await persist(run_id, plan, receipt.get("results", []), decision, receipt.get("artifacts", []))


async def attempt_run(run_id, repo, plan, branch, pr_url, log, cycle, sha, attempt):
    # Import locally: runner owns VM lifecycle helpers and calls this executor for opt-in runs.
    from . import runner as r

    boxd, machine = r.client(), None
    local = settings.log_dir / f"{run_id}-verification" / str(attempt)
    local.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        await db.update_run(run_id, status="forking")
        await r.headroom(boxd, log)
        source = await r.source_for(boxd, repo, log)
        machine = await r._provision(boxd, source, f"{r.REVIEW_PREFIX}{run_id[:8]}-{attempt}")
        await db.update_run(run_id, vm_id=machine.id, vm_name=machine.name, golden=source)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        base = await github.default_branch(repo)
        base_sha = await github.merge_base_sha(repo, base, sha)
        issue = plan["issue"]
        prompt = r.REVIEW_PROMPT_TEMPLATE.format(
            project_notes=plan["profile"], repo=repo, number=issue["number"], title=issue["title"],
            pr_url=pr_url, branch=branch, base=base, base_sha=base_sha,
            criteria=yaml.safe_dump(plan["criteria"], sort_keys=False), body=issue.get("body", ""))
        prompt += ("\nFor this verification plan, inspect criteria are advisory: report them, but never "
                   "request changes for an inspect result alone. Approve when required criteria are "
                   "met and there are no independent blocking findings. Put advisory findings in notes. "
                   "The factory runs the configured E2E stage separately; do not launch that stage yourself.")
        env = r.dispatch_env(repo=repo, branch=branch, base=base, prompt=prompt,
                             run_id=run_id, number=issue["number"], vm_name=machine.name, kind="review")
        env["FACTORY_VERIFY_SHA"] = sha
        # Pin before the agent starts, rather than trusting a branch that can move during provisioning.
        script = r.REVIEW_SCRIPT.replace(
            'git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BRANCH"',
            'git checkout --detach "$FACTORY_VERIFY_SHA"')
        await db.update_run(run_id, status="running")
        code, usage, manifest = await asyncio.wait_for(
            r._stream(boxd, machine.id, env, log, run_id, script=script), timeout=settings.run_timeout)
        identity = await boxd.machines.exec(machine.id, r.export_prelude(env) + r.PRELUDE +
            '\ntest "$(git rev-parse HEAD)" = "$FACTORY_VERIFY_SHA" && git diff --quiet HEAD && echo FACTORY-EXACT-COMMIT',
            env=env, timeout=60)
        if not (identity.stdout or "").rstrip().endswith("FACTORY-EXACT-COMMIT"):
            raise RuntimeError("reviewer changed the checkout under verification")
        saved = await db.get_run(run_id)
        await db.update_run(run_id, **{key: (saved.get(key) or 0) + (usage.get(key) or 0)
                                      for key in ("tokens_in", "tokens_out", "cost_usd")})
        verdict = await r._read_verdict(boxd, machine.id, log)
        (local / "review.json").write_text(json.dumps(verdict, indent=2))
        await db.update_run(run_id, verdict=json.dumps(verdict), exit_code=code)
        await r._salvage_transcript(boxd, machine.id, run_id, log, manifest)
        rows = policy.review_results(plan, sha, attempt, verdict, code)
        if plan["e2e"]:
            remote = f"/tmp/factory-verification/{run_id}/{attempt}"
            env["FACTORY_VERIFICATION_DIR"] = remote
            env["FACTORY_VERIFICATION_PLAN"] = json.dumps(plan)
            worker = Path(__file__).with_name("verification_worker.py").read_text()
            # PRELUDE locates the checkout; it does not reset changes made by a reviewer.
            script = r.PRELUDE + r.NODE_GUARD + "\n" + python_command(worker)
            try:
                await asyncio.wait_for(r._stream(boxd, machine.id, env, log, run_id, script=script),
                                       timeout=min(settings.run_timeout, 1800))
                await collect(boxd, machine, remote, local)
                rows += read_e2e(plan, sha, attempt, local)
            except Exception as exc:
                # A killed model may still have recorded useful steps. Keep them too.
                try:
                    await collect(boxd, machine, remote, local)
                except Exception:
                    pass
                rows += [policy.result(plan, sha, attempt, c, "error", reason=str(exc))
                         for c in plan["checks"] if c["verifier"] == "e2e"]
                log.write(f"[verification] browser execution/collection failed: {exc}")
        return rows, str(local)
    except Exception as exc:
        log.write(f"[verification] attempt {attempt} could not finish: {exc}")
        rows = [policy.result(plan, sha, attempt, c, "error", reason=str(exc))
                for c in plan["checks"]]
        return rows, str(local)
    finally:
        # Evidence is persisted before VM destruction, including when transport fails.
        (local / "results.json").write_text(json.dumps(rows, indent=2))
        await r.reap(boxd, machine, log)
        await boxd.close()


async def route(run_id, repo, plan, branch, pr_url, log, cycle, receipt):
    """Called only after the decision is durable. A repeated call cannot dispatch twice."""
    from . import runner as r

    decision = receipt["decision"]
    action = decision["action"]
    number = plan["issue"]["number"]
    if action == "eligible_to_merge":
        if await github.pr_head_sha(repo, int(pr_url.rstrip("/").split("/")[-1])) != decision["commit"]:
            decision = {**decision, "action": "escalate", "reason": "head_changed"}
        elif settings.auto_merge:
            merged = await r._merge(repo, pr_url, await github.default_branch(repo), log,
                                    issue=plan["issue"], branch=branch, cycle=cycle,
                                    expected_sha=decision["commit"])
            if not merged.merged:
                decision = {**decision, "action": "escalate", "reason": merged.why}
                if merged.ci_failure and cycle < plan["max_cycles"]:
                    decision.update(action="request_fixes", reason=merged.ci_failure)
                    receipt["results"].append({"verifier": "ci", "reason": merged.ci_log or merged.ci_failure})
    if decision != receipt["decision"]:
        await persist(run_id, plan, receipt["results"], decision, receipt["artifacts"])
    action = decision["action"]
    if action == "request_fixes":
        detail = json.dumps({"decision": decision, "results": receipt["results"],
                             "evidence": receipt["artifacts"]}, indent=2)
        child = await r.create(repo, number, review_cycle=cycle + 1,
                               prior_error=decision["reason"], prior_log=detail,
                               verification_plan=plan, verification_source=run_id,
                               dispatch_key=f"{run_id}:fix")
        log.write(f"[verification] fix scheduled: {child}")
    elif action == "eligible_to_merge":
        await r._mirror_issue(repo, number, github.LABEL_DONE, [github.LABEL_RUNNING], log)
    else:
        await db.update_run(run_id, error=f"verification blocked: {decision['reason']}")
        await r._mirror_issue(repo, number, github.LABEL_BLOCKED,
                              [github.LABEL_RUNNING, github.LABEL_QUEUED], log)
    log.write(f"[verification] {action}: {decision['reason']}")


async def execute(run_id, repo, plan, branch, pr_url, log, cycle):
    sha = await github.pr_head_sha(repo, int(pr_url.rstrip("/").split("/")[-1]))
    rows, artifacts = [], []
    for attempt in range(1, plan["max_retries"] + 2):
        initial = policy.decide(plan, sha, rows, attempt=attempt, cycle=cycle)
        await persist(run_id, plan, rows, initial, artifacts)
        current, artifact = await attempt_run(run_id, repo, plan, branch, pr_url, log, cycle, sha, attempt)
        rows += current
        artifacts.append(artifact)
        decision = policy.decide(plan, sha, rows, attempt=attempt, cycle=cycle, completed=True)
        await persist(run_id, plan, rows, decision, artifacts)
        log.write(f"[verification] attempt {attempt}: {decision['action']} ({decision['reason']})")
        if decision["action"] != "retry_verification":
            break
    await route(run_id, repo, plan, branch, pr_url, log, cycle,
                {"decision": decision, "results": rows, "artifacts": artifacts})
