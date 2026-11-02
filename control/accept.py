"""Browser acceptance: check a deployed release on its test server, the way a user would.

Every item in a release was reviewed against its acceptance criteria on a branch, in a VM, with
the repo's own tests. That proves the code does what the criteria say; it does not prove the
running app does — a migration that did not apply, an env var the test server lacks, a page
that renders in a unit test and crashes behind the real auth. This run is the layer
`discussion.md` calls "preview + smoke": an agent on a VM booted from `golden-tester` (the base
image plus Playwright and a browser — Playwright stays out of every build and review VM) opens
the test server and exercises each issue the release carried.

The agent reports; the control plane decides (`decide_acceptance`), failing closed the way
`runner.decide` does. Three endings:

- **accepted** — every issue passed, or had nothing a user could observe and said why.
- **rejected** — at least one issue failed in the browser. One repair issue is filed, citing
  those issues' own blocking criteria in a yaml block so it passes the dispatch gate by
  construction, labelled `agent:queued`. The release stays rejected — and its repo frozen —
  until a human reviews it and the repair has merged.
- **failed** — no usable verdict, an issue nobody reported on, an issue nobody could verify,
  or a rejection after `FACTORY_ACCEPT_MAX_REPAIRS` repairs in a row. Nothing concrete is known
  to be broken, or the factory has already tried, so it stops for a human without filing.

The test server is never rolled back on a rejection. The packet says so; a human decides.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

import yaml

from . import agents, db, github, runner
from .config import settings

log = logging.getLogger("factory.accept")

# The statuses an item may carry, and the two that let a release through.
ITEM_STATUSES = ("pass", "fail", "not_applicable", "cannot_verify")
PASSING = ("pass", "not_applicable")

# Read-only, like the reviewer's script: the release checked out detached at its sha, so the
# agent can read the code it is testing, and no branch it could push. Nothing here builds or
# installs — the thing under test is the server, not this checkout.
ACCEPT_SCRIPT = runner.PRELUDE + r"""
rm -f /tmp/factory-verdict.json
mkdir -p /tmp/acceptance
echo "FACTORY: checking out the release $FACTORY_ACCEPT_SHA, read-only"
git checkout --detach "$FACTORY_ACCEPT_SHA" || { echo "FACTORY: checkout failed" >&2; exit 92; }
echo "FACTORY: starting the acceptance tester against $FACTORY_ACCEPT_URL"
command -v factory-agent >/dev/null 2>&1 || { claude -p "$FACTORY_PROMPT" \
  --effort "$FACTORY_AGENT_EFFORT" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null; exit $?; }
echo "FACTORY-MANIFEST $(tr -d '\n' < /etc/factory/agent.json 2>/dev/null)"
exec factory-agent
"""

ACCEPT_PROMPT_TEMPLATE = """You are checking a release of a web app in a real browser, on the \
server it is deployed to. Whether it stays there depends on what you report.

Repository: {repo}
Release #{release_id}: commit {sha}, checked out read-only in your working directory so you can
read how things are meant to work. Do not build or run it here — the app under test is the
deployed one.
Test server: {url}

Every change below was already reviewed against its acceptance criteria on a branch, with the
repo's own tests. Your job is different: confirm each one works **for a user of the running
app**. The criteria tell you what each change was meant to do; check what of that a user or an
HTTP client can observe on the test server.

--- the issues this release carries ---
{items}
--- end issues ---

How to work:
- Playwright and Chromium are installed in the project at `~/tester`. Write short scripts there
  (`import {{ chromium }} from "playwright"`) and run them from that directory, in the
  foreground, with a timeout: `cd ~/tester && timeout 120 node check-7.mjs`. Headless.
- Save screenshots under /tmp/acceptance/ and cite their paths in your evidence.
- Create your own test data (an account, a record) when a check needs it; never change or delete
  data you did not create. The test server is shared.
- Do not modify the checkout, commit, push, or open, close or comment on anything on GitHub.
- Do not put work in the background to wait for it later; it will never report back.

The rule that matters: **every status needs evidence you produced on the running server** — what
you did and what you saw: the URL, the text on the page, the HTTP status, the screenshot path.
Reading the code and concluding it works is not evidence.

- `pass` — you exercised the change on the test server and it behaved as its criteria describe.
- `fail` — you exercised it and it did not. Say exactly what you did and what happened instead;
  this text becomes the repair issue another agent works from.
- `not_applicable` — the change has nothing a user or an HTTP client can observe (an internal
  refactor, a test-only change, a CI change). Say why in `evidence`; an empty reason is not
  accepted.
- `cannot_verify` — you tried and could not tell: the server is unreachable, a login is
  impossible. Say what stopped you. It counts as not accepted.

Also report as findings anything that is plainly broken on the server even if no issue above
touches it (a page that 500s, a login that loops), with evidence.

When you are done, write **/tmp/factory-verdict.json** and nothing else:

{{
  "verdict": "accept" | "reject",
  "items": [
    {{"issue": <number>, "status": "pass" | "fail" | "not_applicable" | "cannot_verify",
      "evidence": "what you did and what you saw"}}
  ],
  "findings": ["one line each — what is broken, where, and how you saw it"],
  "notes": ["anything advisory"]
}}

Include every issue above by number. Say `accept` only if every item is `pass` or
`not_applicable` and nothing in `findings` should stop the release.
"""


def format_items(items: list[dict]) -> str:
    """The release's items as the prompt lists them: title, summary, criteria."""
    blocks = []
    for item in items:
        lines = [f"#{item['issue_number']} {item.get('issue_title') or ''}".rstrip()]
        if item.get("pr_url"):
            lines.append(f"  pull request: {item['pr_url']}")
        if item.get("summary"):
            lines.append(f"  summary: {item['summary']}")
        criteria = _criteria(item)
        if criteria:
            lines.append("  criteria:")
            lines += [
                f"    - {c.get('id')} [{c.get('mode')}] {c.get('statement')}" for c in criteria
            ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "(this release records no factory-built issues; check the app " \
                                   "is up and its main pages work, and report under findings)"


def _criteria(item: dict) -> list[dict]:
    try:
        parsed = json.loads(item.get("criteria") or "[]")
    except (TypeError, ValueError):
        return []
    return [c for c in parsed if isinstance(c, dict)] if isinstance(parsed, list) else []


def decide_acceptance(
    verdict: dict | None, issues: list[int]
) -> tuple[bool, str, list[int]]:
    """Turn a tester's verdict into a decision. Returns (accepted, why, failed issues).

    The control plane decides, as with reviews: an `accept` cannot survive an item the tester
    itself marked `fail`, an issue it never mentioned, or one it could not verify. `failed`
    lists only issues the tester *saw* fail — the one ending concrete enough to file a repair
    for. Everything else unaccepted comes back with an empty list, which the caller turns into
    a stop for a human rather than a repair nobody could act on.
    """
    if not isinstance(verdict, dict):
        return False, "no usable verdict from the acceptance run", []
    reported: dict[int, dict] = {}
    for item in verdict.get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            reported[int(item.get("issue"))] = item
        except (TypeError, ValueError):
            continue
    failed = [n for n in issues if (reported.get(n) or {}).get("status") == "fail"]
    if failed:
        return False, "failed in the browser: " + ", ".join(f"#{n}" for n in failed), failed
    missing = [n for n in issues if n not in reported]
    if missing:
        return False, "the tester did not report on " + ", ".join(f"#{n}" for n in missing), []
    unverified = []
    for n in issues:
        item = reported[n]
        status = item.get("status")
        evidence = str(item.get("evidence") or "").strip()
        if status not in PASSING or not evidence:
            unverified.append(n)
    if unverified:
        return False, "could not verify " + ", ".join(f"#{n}" for n in unverified), []
    if verdict.get("verdict") != "accept":
        findings = [str(f) for f in verdict.get("findings") or []][:3]
        return False, "the tester rejected the release" + (
            f": {'; '.join(findings)}" if findings else ""), []
    return True, f"{len(issues)} issue(s) accepted in the browser", []


def repair_issue(
    release: dict, items: list[dict], verdict: dict | None, failed: list[int]
) -> tuple[str, str]:
    """The repair issue for a rejected release: (title, body).

    Its acceptance criteria are the failed issues' own blocking criteria, re-cited with their
    ids prefixed by the issue number so two issues' `AC1` stay distinct. Those criteria let
    their issues merge, so they are runnable by construction — and the issue therefore passes
    `runner.gate_reason`, which is what lets the factory build it without a human rewriting it.
    `inspect` criteria are left out: they never block, and a repair is about what failed.
    """
    reported = {}
    if isinstance(verdict, dict):
        for item in verdict.get("items") or []:
            if isinstance(item, dict):
                try:
                    reported[int(item.get("issue"))] = item
                except (TypeError, ValueError):
                    pass
    by_number = {i["issue_number"]: i for i in items}
    cited, seen = [], []
    for number in failed:
        item = by_number.get(number) or {"issue_number": number}
        seen.append(
            f"- #{number} {item.get('issue_title') or ''}".rstrip() + "\n  "
            + (" ".join(str((reported.get(number) or {}).get("evidence") or "(no evidence)").split()))
        )
        for c in _criteria(item):
            if c.get("mode") not in runner.BLOCKING_MODES:
                continue
            cited.append({**c, "id": f"I{number}-{c.get('id')}"})
    findings = [str(f) for f in (verdict or {}).get("findings") or []] if isinstance(verdict, dict) else []
    url = release.get("test_url") or "the test server"
    numbers = ", ".join(f"#{n}" for n in failed)
    title = f"Repair release #{release['id']}: {numbers} failed in the browser"
    body = (
        f"<!-- factory: repair of release #{release['id']} -->\n\n"
        f"## Task\n\n"
        f"Release #{release['id']} of {release['repo']} (commit "
        f"`{(release.get('test_sha') or '')[:12]}`) was deployed to {url} and failed browser "
        f"acceptance on {numbers}. The changes those issues made are already on the integration "
        f"branch; find why they do not work on the running server and fix that. The criteria "
        f"below are the ones those issues merged with — keep them true.\n\n"
        f"## What the browser saw\n\n" + "\n".join(seen) + "\n"
        + ("\nOther findings:\n" + "\n".join(f"- {f}" for f in findings) + "\n" if findings else "")
        + "\n## Acceptance criteria\n\n```yaml\n"
        + yaml.safe_dump(cited, sort_keys=False, allow_unicode=True)
        + "```\n"
    )
    return title, body


# --------------------------------------------------------------------------- the run


async def create(release_id: int) -> str:
    """Start an acceptance run against a deployed release. Returns the run id.

    Refuses with `ValueError` — noted on the release, which stays `deployed` — when the
    `golden-tester` image does not exist: without a browser there is nothing to run.
    """
    release = await db.get_release(release_id)
    if release is None:
        raise ValueError(f"no such release: {release_id}")
    if release["status"] != "deployed":
        raise ValueError(f"release {release_id} is {release['status']}, not deployed")
    boxd = runner.client()
    try:
        agents.forget()
        known = {s.name for s in await boxd.snapshots.list()}
    finally:
        await boxd.close()
    if agents.TESTER_SNAPSHOT not in known:
        why = (f"no {agents.TESTER_SNAPSHOT} snapshot; build it with scripts/build-golden.sh "
               f"--name {agents.TESTER_SNAPSHOT} --playwright --no-login")
        await db.set_release_error(release_id, f"not accepted: {why}")
        raise ValueError(why)

    await db.transition_release(release_id, "accepting", error=None)
    try:
        return await _start(release_id, release)
    except BaseException as exc:
        # See `deploy.create`: a release left `accepting` with no task freezes its repo.
        await _fail_release(release_id, f"acceptance could not start: {exc!r}"[:300])
        raise


async def _start(release_id: int, release: dict) -> str:
    run_id = uuid.uuid4().hex
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()
    await db.create_run(
        id=run_id,
        repo=release["repo"],
        issue_number=0,
        issue_title=f"accept release {release_id} on {release.get('test_url') or 'the test server'}",
        status="queued",
        kind="accept",
        agent=agents.DEFAULT_AGENT,
        base_sha=release["test_sha"],
        release_id=release_id,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )
    runner.track(run_id, asyncio.create_task(_guarded(run_id, release_id)))
    return run_id


async def _guarded(run_id: str, release_id: int) -> None:
    run_log = runner.RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with runner.semaphore():
            verdict = await _execute(run_id, release_id, run_log)
        await db.update_run(run_id, status="succeeded", finished_at=db.utcnow())
        await conclude(release_id, verdict, run_log)
    except asyncio.CancelledError:
        run_log.write("[factory] acceptance cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        await _fail_release(release_id, "acceptance cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - a crashed tester is a failed release, not an outage
        reason = (
            f"timed out after {settings.run_timeout}s"
            if isinstance(exc, asyncio.TimeoutError)
            else f"over budget: {exc}" if isinstance(exc, runner.BudgetExceeded)
            else f"crashed: {str(exc)[:200] or type(exc).__name__}"
        )
        run_log.write(f"[factory] acceptance failed: {reason}")
        await db.update_run(run_id, status="failed", error=reason, finished_at=db.utcnow())
        await _fail_release(release_id, f"acceptance {reason}")
    finally:
        await runner._salvage_usage(run_id, run_log)
        run_log.close()


async def _fail_release(release_id: int, error: str) -> None:
    try:
        await db.transition_release(release_id, "failed", error=error[:1000])
    except ValueError as exc:
        log.warning("release %s could not be failed: %s", release_id, exc)


async def _execute(run_id: str, release_id: int, run_log: runner.RunLog) -> dict | None:
    """Boot `golden-tester`, point an agent at the test server, read back its verdict."""
    release = await db.get_release(release_id)
    items = await db.release_items(release_id)
    repo, sha = release["repo"], release["test_sha"]
    boxd = runner.client()
    machine = None
    try:
        await db.update_run(run_id, status="forking", started_at=db.utcnow())
        vm_name = f"{runner.ACCEPT_PREFIX}{run_id[:8]}"
        run_log.write(f"[factory] accepting release {release_id} of {repo} at {release['test_url']}")
        await runner.headroom(boxd, run_log)
        await db.update_run(run_id, golden=agents.TESTER_SNAPSHOT)
        run_log.write(f"[factory] provisioning {vm_name} from {agents.TESTER_SNAPSHOT}")
        machine = await runner._provision(boxd, agents.TESTER_SNAPSHOT, vm_name)
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        run_log.write(f"[factory] {machine.name} ready ({machine.id})")

        await db.update_run(run_id, status="running")
        prompt = ACCEPT_PROMPT_TEMPLATE.format(
            repo=repo, release_id=release_id, sha=sha,
            url=release.get("test_url") or "(no URL recorded — see the repo's ## Deploy section)",
            items=format_items(items),
        )
        env = runner.dispatch_env(
            repo=repo,
            branch=settings.release_branch,
            base=settings.release_branch,
            prompt=prompt,
            run_id=run_id,
            number=0,
            vm_name=machine.name,
            kind="accept",
        )
        env["FACTORY_ACCEPT_SHA"] = sha
        env["FACTORY_ACCEPT_URL"] = release.get("test_url") or ""
        exit_code, usage, manifest = await asyncio.wait_for(
            runner._stream(boxd, machine.id, env, run_log, run_id, script=ACCEPT_SCRIPT),
            timeout=settings.run_timeout,
        )
        run_log.write(f"[factory] tester exited {exit_code}")
        verdict = await runner._read_json_file(boxd, machine.id, runner.VERDICT_PATH, run_log)
        await runner._salvage_transcript(boxd, machine.id, run_id, run_log, manifest)
        await db.update_run(
            run_id,
            exit_code=exit_code,
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            cost_usd=usage.get("cost_usd"),
            verdict=json.dumps(verdict) if verdict is not None else None,
        )
        return verdict
    finally:
        await runner.reap(boxd, machine, run_log, keep=settings.keep_failed)
        await boxd.close()


async def repairs_in_a_row(repo: str, before_id: int) -> int:
    """How many repair issues acceptance has filed for `repo` since it last accepted a release.

    Releases that never reached acceptance — red CI, a failed deploy — neither count nor reset
    the run: they say nothing about whether the last repair worked.
    """
    count = 0
    for release in await db.list_releases(repo=repo, limit=50):
        if release["id"] >= before_id:
            continue
        if release.get("repair_issue"):
            count += 1
        elif release.get("accepted_at"):
            break
    return count


async def conclude(release_id: int, verdict: dict | None, run_log: runner.RunLog) -> str:
    """Decide what the verdict means for the release, and act on it. Returns the new status."""
    release = await db.get_release(release_id)
    items = await db.release_items(release_id)
    accepted, why, failed = decide_acceptance(verdict, [i["issue_number"] for i in items])
    run_log.write(f"[factory] acceptance: {'accepted' if accepted else 'not accepted'} — {why}")
    if accepted:
        await db.transition_release(release_id, "accepted", accepted_at=db.utcnow())
        return "accepted"
    if not failed:
        await _fail_release(release_id, f"acceptance: {why} — needs a human")
        return "failed"
    repairs = await repairs_in_a_row(release["repo"], release_id)
    if repairs >= settings.accept_max_repairs:
        await _fail_release(
            release_id,
            f"acceptance: {why}, after {repairs} repair(s) in a row — needs a human",
        )
        return "failed"
    title, body = repair_issue(release, items, verdict, failed)
    # Queued only when the factory could actually build it. By construction it can — the
    # criteria are the ones the failed issues merged with — but an issue from before criteria
    # were mandatory has none, and a queued issue the poller would only block helps nobody.
    buildable = runner.gate_reason(body) is None
    issue = await github.create_issue(
        release["repo"], title, body, [github.LABEL_QUEUED] if buildable else []
    )
    number = issue.get("number")
    run_log.write(
        f"[factory] filed repair #{number}"
        + ("" if buildable else " unqueued: the failed issues carry no blocking criterion")
    )
    await db.transition_release(
        release_id, "rejected", repair_issue=number,
        error=f"acceptance: {why}; repair #{number} filed"
              + ("" if buildable else " for a human (no blocking criteria to cite)"),
    )
    return "rejected"
