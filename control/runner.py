"""Fork a VM, run an agent in it, collect the result, reap the VM.

This module is the whole factory. It contains no model call: it decides nothing about the
work itself, only about machine lifecycle. All intelligence is the agent inside the VM.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml
from boxd import AsyncBoxd, _mappers as _boxd_mappers

from telemetry.recorder import Recorder

from . import agents, db, github
from .config import settings

# boxd reports a snapshot's timestamps in **milliseconds**, but the SDK's snapshot mapper reads
# them as seconds (`_epoch`, where the machine mapper alongside it correctly uses `_epoch_ms`),
# so `snapshots.list()` dies in `datetime.fromtimestamp` with "year 58577 is out of range".
# That takes golden discovery with it, and with it every dispatch: `agents.available` cannot
# name a single `golden-*`, so preflight's `fleet readable` check fails fatally and no issue
# can be picked up. Verified against SDK 0.2.2, 0.2.4 and 0.2.5 — all three fail
# identically, so there is no version to pin `boxd>=0.2.2` back to.
#
# Nothing in this repo reads a snapshot's timestamp, so the narrowest fix is to stop the
# mapper crashing rather than to reimplement the listing: an epoch beyond any date boxd could
# plausibly report is milliseconds, and is scaled back down. Delete this once the SDK maps
# snapshot timestamps with `_epoch_ms`; the patch is idempotent and self-identifying so that
# removing it is a one-line change and leaving it in is harmless.
_EPOCH_SECONDS_CEILING = 32_503_680_000  # 3000-01-01, far past anything boxd will report.


def _tolerate_millisecond_epochs() -> None:
    """Make the boxd SDK's epoch mappers accept milliseconds as well as seconds."""
    for name in ("_epoch", "_epoch_always"):
        original = getattr(_boxd_mappers, name, None)
        if original is None or getattr(original, "_factory_patched", False):
            continue

        def scaled(value, _original=original):
            if value and abs(value) > _EPOCH_SECONDS_CEILING:
                value = value // 1000
            return _original(value)

        scaled._factory_patched = True
        setattr(_boxd_mappers, name, scaled)


_tolerate_millisecond_epochs()


# `log` is the per-run RunLog throughout this file, so the module logger takes another name.
_log = logging.getLogger("factory.runner")

# Run ids of in-flight runs -> their asyncio task, so the UI can cancel them.
_tasks: dict[str, asyncio.Task] = {}
_semaphore: asyncio.Semaphore | None = None


def semaphore() -> asyncio.Semaphore:
    """Created lazily so it binds to the running loop, not import-time."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_concurrent)
    return _semaphore


# Every VM a run creates is named from its run id with one of these prefixes: `run-` for a
# build, `rev-` for a review, `prov-` for warming a repo's golden. Reconcile and the fleet view
# both key off them, so they live here rather than being spelled out at each site — a prefix
# known to one and not the other is a VM nobody reaps and nobody recognises.
RUN_PREFIX = "run-"
REVIEW_PREFIX = "rev-"
PROVISION_PREFIX = "prov-"
VM_PREFIXES = (RUN_PREFIX, REVIEW_PREFIX, PROVISION_PREFIX)


def is_run_vm(name: str) -> bool:
    """True for a machine this factory created for a run: build, review or provisioning."""
    return name.startswith(VM_PREFIXES)


def vm_role(name: str) -> str:
    """What a machine in the fleet is: `run`, `review`, `provision`, or `other`.

    Read off the same prefixes `is_run_vm` sweeps on, so the fleet view and the reaper can
    never disagree about what belongs to the factory. `other` covers everything the factory
    did not create — a control plane, somebody's scratch machine — and, now that goldens are
    snapshots, it is where a golden still held as a machine shows up: a rollback artefact
    rather than a category.
    """
    if name.startswith(REVIEW_PREFIX):
        return "review"
    if name.startswith(PROVISION_PREFIX):
        return "provision"
    if name.startswith(RUN_PREFIX):
        return "run"
    return "other"


def client() -> AsyncBoxd:
    return AsyncBoxd(api_key=settings.boxd_api_key)


async def machine_id(boxd: AsyncBoxd, source: str) -> str:
    """Resolve a machine's id.

    The boxd SDK forks by machine **id**, not name — forking by name fails with
    `source VM not found` even though the machine lists fine. A golden is named, not
    numbered, so resolve it here. Accepts an id too, so either form works.
    """
    for m in await boxd.machines.list():
        if m.id == source or m.name == source:
            return m.id
    raise RuntimeError(f"golden VM {source!r} not found in the boxd fleet")


async def _is_snapshot(boxd: AsyncBoxd, source: str) -> bool:
    """Does `source` name a snapshot in this account?

    Asked of the fleet rather than of the name, because both kinds of source are named the
    same way: `golden-copy` is a snapshot on the new path and was a machine on the old one. A name alone cannot tell them apart, and guessing wrong picks the wrong API.
    """
    return any(s.id == source or s.name == source for s in await boxd.snapshots.list())


async def _provision(boxd: AsyncBoxd, source: str, vm_name: str):
    """Give a run its VM, from a snapshot when `source` is one and by forking when it is not.

    Goldens are moving from long-lived machines to snapshots: a running golden holds one of
    the account's 20 concurrent VM slots forever, a snapshot holds none. The fork path stays
    for as long as any deployment still points at a machine — it is the rollback.

    Both paths set the same two timers, and both are load-bearing. Without
    `auto_suspend_timeout=0` the default idle suspend freezes a VM in the middle of a long,
    silent build. Without `auto_destroy_timeout` a control plane that dies leaks the slot for
    good. The snapshot path passes nothing else: `create(from_snapshot=...)` rejects `env`,
    `image`, `cmd`, `restart_policy`, `shared` and `networks` with a ValueError, since
    restoring a capture replays a machine rather than configuring a new one. The per-run
    environment arrives later, through `stream_exec(env=...)`, exactly as it does on the fork
    path.
    """
    if await _is_snapshot(boxd, source):
        return await boxd.machines.create(
            name=vm_name,
            from_snapshot=source,
            auto_suspend_timeout=0,
            auto_destroy_timeout=settings.auto_destroy,
        )
    return await boxd.machines.fork(
        await machine_id(boxd, source),
        vm_name,
        auto_suspend_timeout=0,
        auto_destroy_timeout=settings.auto_destroy,
    )


async def reap(boxd: AsyncBoxd, machine, log: RunLog, *, keep: bool = False) -> None:
    """Destroy a run's VM. Idempotent, and it never raises.

    Every path that creates a machine ends here, including the ones that got there by
    crashing. A machine that outlives its run holds one of the account's 20 slots until the
    `auto_destroy` timer fires two hours later, and three of those is a factory that cannot
    dispatch — so a failure to reap is logged and handed to the reconciler rather than raised
    into a `finally` that is already handling something else.
    """
    if machine is None:
        return
    if keep:
        log.write(f"[factory] keeping {machine.name} for inspection (FACTORY_KEEP_FAILED=1)")
        return
    try:
        await boxd.machines.delete(machine.id)
        log.write(f"[factory] destroyed {machine.name}")
    except Exception as exc:  # noqa: BLE001 - already gone is fine, and a leak is the reaper's
        log.write(f"[factory] could not destroy {machine.name}: {exc!r}; reconcile will sweep it")


async def headroom(boxd: AsyncBoxd, log: RunLog) -> None:
    """Refuse to provision when the fleet is at its cap, after trying to make room.

    Nothing counted machines against the quota before this: concurrency was bounded by
    `FACTORY_MAX_CONCURRENT`, which says nothing about the goldens, the control plane, or a
    machine somebody left running by hand. Past the cap boxd refuses the create, and the run
    that finds out is the one that dies.

    So ask first, and sweep before giving up — an orphaned run VM is exactly the thing
    `reconcile` exists to reclaim, and a factory that reaps and then proceeds is better than
    one that stops at a limit it could have cleared itself.
    """
    if settings.max_machines <= 0:
        return
    count = len(await boxd.machines.list())
    if count < settings.max_machines:
        return
    log.write(f"[factory] fleet at {count}/{settings.max_machines}; sweeping for orphans")
    swept = await reconcile()
    count = len(await boxd.machines.list())
    if count >= settings.max_machines:
        raise RuntimeError(
            f"the boxd fleet is at {count}/{settings.max_machines} machines and sweeping "
            f"reclaimed {len(swept.get('destroyed') or [])}; nothing can be provisioned until "
            "something is destroyed"
        )
    log.write(f"[factory] reclaimed to {count}/{settings.max_machines}")


async def source_for(boxd: AsyncBoxd, repo: str, log: RunLog | None = None) -> str:
    """Which golden this run boots from: this repo's own, or the base image behind it.

    Public because a run is not the only thing that needs the answer — the projects page and
    preflight both report on the snapshot a repo's runs would actually boot, and a second
    derivation of that would be a second thing to keep in step. Without a `log` the fallback
    is noted on the module logger instead of in a run's own log.

    One fallback, and it is the ordinary case rather than an error path: a repo nobody has
    provisioned a golden for yet boots `golden-copy` and installs for itself. That is what
    lets a repo be connected and dispatched in the same minute, with provisioning catching up
    afterwards.

    When the snapshot fleet answers to nothing at all, the base name is handed to `_provision`
    anyway. On the rollback path `golden-copy` is still a *machine*, and forking it is exactly
    what should happen.
    """
    note = log.write if log is not None else _log.info
    names = await agents.available(boxd)
    source = agents.resolve_snapshot(repo, names)
    if not source:
        source = agents.BASE_SNAPSHOT
        note(f"[factory] no golden snapshot in the fleet; trying machine {source}")
    elif source == agents.BASE_SNAPSHOT:
        note(f"[factory] no warm golden for {repo}; booting {source} and installing for itself")
    return source


# --------------------------------------------------------------------------- prompt


PROMPT_TEMPLATE = """You are working autonomously in an isolated VM on a checked-out git \
repository. Resolve the GitHub issue below, then open a pull request.

Repository: {repo}
Issue #{number}: {title}

--- issue body ---
{body}
--- end issue body ---

How to work:
1. Load the `memory` skill first and prime yourself from `.mem/` if it exists. What past
   runs learned about this repo is the most valuable context you have.
2. Install this project's dependencies, before you touch any code. Run the setup command
   named under "This project" below, in the foreground, with an explicit timeout — e.g.
   `timeout 900 <the setup command>`. Run it **once**: if it worked, a second install spends
   minutes of wall clock arriving where you already are, and if it failed, read the error and
   fix that rather than run the same command again.
3. Make the change. Stay in scope: resolve this issue, nothing more.
   If the issue carries an `## Acceptance criteria` block, that is the contract. Every
   criterion must be true when you are done, and a reviewing agent will afterwards run each
   one rather than take your word for it. For `mode: test` criteria, write the test at the
   path given in `verify`, and make sure it **fails before your change and passes after** —
   a test that passes either way proves nothing and will be rejected.
4. Commit and push as you go — after each meaningful step, not once at the end:
   git add -A && git commit -m "<message>" && git push -u origin {branch}
   The branch is what survives; this VM is destroyed when you exit. If this run dies
   half-way, whatever you pushed is what the next attempt continues from, so small
   commits that build on each other are worth far more than one perfect commit you
   never got to make.
5. Verify with the repo's own fast checks — the ones named under "This project" below,
   and only those. Do not add a test framework or test runner that isn't already in the repo.
6. Record anything durable you learned into `.mem/`, following the memory skill.
7. Push the final commit and open a pull request with `gh pr create --fill --base {base}`,
   referencing the issue in the body so it links (e.g. "Closes #{number}").

This project — the package manager's download caches on this machine are warm, so installing
is fast, but nothing is installed for this repo yet:

{project_notes}

True of every run here, whatever the project notes say:
- Run long commands in the foreground with an explicit timeout, e.g.
  `timeout 600 <the build command>`. Do not put work in the background to wait for it later:
  background tasks and scheduled wake-ups do not deliver notifications in this
  environment, so a run that waits for one waits forever and dies having produced nothing.

If you cannot resolve the issue, still push what you have and open a draft PR explaining
what blocked you. A run that ends with no PR gives the human nothing to look at.
"""


# --------------------------------------------------------------------------- criteria

# The acceptance-criteria block an issue carries, per the factory-compose issue template
# (mithril-studio/agent-skills, factory-skills/factory-compose): a
# fenced yaml list under an "## Acceptance criteria" heading.
_CRITERIA_BLOCK = re.compile(
    r"^##\s*Acceptance criteria\s*?\n+```ya?ml\n(.*?)^```", re.S | re.M | re.I
)

# Modes whose verdict may block a merge. `inspect` needs human judgement, so it is reported
# and never blocks — an agent's opinion is not a gate.
BLOCKING_MODES = ("test", "probe", "structure")


def parse_criteria(body: str) -> list[dict]:
    """Extract an issue's acceptance criteria. Returns [] when there are none to run.

    Deliberately done here rather than by the reviewing agent: what the criteria *are* is not
    a judgement call, and an agent that reads its own contract can also misread it. A malformed
    block yields [] — which skips review rather than inventing criteria, so the failure mode is
    "unreviewed like before", never "reviewed against something nobody wrote".
    """
    match = _CRITERIA_BLOCK.search(body or "")
    if not match:
        return []
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if not {"id", "mode", "statement"} <= set(item):
            continue
        if item["mode"] not in (*BLOCKING_MODES, "inspect"):
            continue
        out.append(item)
    return out


def _run_link(run_id: str) -> str | None:
    return f"{settings.base_url}/runs/{run_id}" if settings.base_url else None


async def _mirror_issue(
    repo: str,
    number: int,
    add: str | None,
    remove: list[str],
    log: RunLog,
    comment: str | None = None,
) -> None:
    """Reflect run state onto the issue as a label, optionally leaving a comment.

    Labels mirror the runs table for humans reading GitHub; they are never read back as
    truth. So this is best-effort by design — a GitHub hiccup is logged and swallowed, never
    allowed to fail an otherwise good run.
    """
    try:
        for label in remove:
            await github.remove_label(repo, number, label)
        if add:
            await github.add_labels(repo, number, [add])
        if comment:
            await github.add_comment(repo, number, comment)
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] issue update skipped: {exc!r}")


RETRY_TEMPLATE = """

--- retry context ---
This is attempt {attempt} of {max_attempts}. Earlier attempts on this issue failed
({prior_error}). Below is the tail of the previous attempt's log. Read it, work out the root
cause, and fix *that* — do not blindly repeat what failed.

You are already checked out on {branch}, including any commits an earlier attempt pushed to
it — read them first (`git log --oneline origin/{base}..HEAD` and `git diff origin/{base}`)
and continue from there. Do not start over, and do not force-push over that work.

previous attempt log (tail):
{prior_log}
--- end retry context ---
"""


# The file a watched repo carries to describe itself: how it is set up, how it is verified,
# what must not be touched. It lives in the repo rather than here because it
# describes that repo — one project's `npm run test:integration` is another project's
# nonsense, and a control plane that asserts one project's setup as universal sends every
# other project's agent to verify something that does not exist.
PROFILE_PATH = ".factory.md"

# What a repo without a profile gets. Deliberately says nothing specific: a wrong fact costs
# more than a missing one, because the agent acts on it before it can find out.
DEFAULT_PROJECT_NOTES = """- Nothing is installed for this repo yet. Work out the package manager from the lock file
  at the root and run its frozen-lockfile install once — `package-lock.json` -> `npm ci`,
  `pnpm-lock.yaml` -> `pnpm install --frozen-lockfile`, `yarn.lock` -> `yarn install
  --immutable`, `uv.lock` -> `uv sync --frozen`, `poetry.lock` -> `poetry install --sync`,
  `Cargo.lock` -> `cargo fetch`, `go.sum` -> `go mod download`. Frozen, so the install is the
  one the lock file describes and your run does not silently upgrade the project. Do not
  delete or regenerate a lock file, and do not add a package the issue does not need.
- This repo carries no `.factory.md`, so nothing more specific about it is known here. Read
  its own rules files (`CLAUDE.md`, `AGENTS.md`, `CONTEXT.md`, the README) and `.mem/` to
  find out how it is built and verified, and run those checks.
- Do not run an end-to-end or browser suite, and do not install browsers — CI covers that on
  your pull request."""


async def project_notes(repo: str, ref: str) -> str:
    """The repo's own `.factory.md` at `ref`, or the generic default when it has none.

    Best-effort by design: a GitHub hiccup here must not fail a run that would otherwise
    work, and the default is a safe thing to say about any repo.
    """
    try:
        text = await github.file(repo, PROFILE_PATH, ref)
    except Exception:  # noqa: BLE001 - a profile is an improvement, never a precondition
        _log.exception("could not read %s from %s", PROFILE_PATH, repo)
        return DEFAULT_PROJECT_NOTES
    return text.strip() if text and text.strip() else DEFAULT_PROJECT_NOTES


def build_prompt(
    repo: str,
    issue: dict,
    branch: str,
    base: str,
    notes: str = DEFAULT_PROJECT_NOTES,
    attempt: int = 1,
    prior_error: str | None = None,
    prior_log: str | None = None,
) -> str:
    prompt = PROMPT_TEMPLATE.format(
        repo=repo,
        number=issue["number"],
        title=issue["title"],
        body=issue["body"] or "(no description given)",
        branch=branch,
        base=base,
        project_notes=notes,
    )
    if attempt > 1:
        prompt += RETRY_TEMPLATE.format(
            attempt=attempt,
            max_attempts=settings.max_attempts,
            prior_error=prior_error or "reason not captured",
            prior_log=prior_log or "(previous log unavailable)",
            branch=branch,
            base=base,
        )
    return prompt


def _log_tail(run_id: str, max_chars: int = 4000) -> str:
    """The tail of a run's log — the failure context handed to the next attempt."""
    path = settings.log_dir / f"{run_id}.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(previous log unavailable)"
    return text[-max_chars:]


# The script the VM runs. Values arrive as environment variables so nothing needs shell
# quoting here — the prompt in particular can contain anything at all.
# Runs under /bin/sh (dash) via boxd exec, NOT bash — keep it POSIX. No `set -o pipefail`
# (a bash-ism dash rejects, which kills the script on line 1); errors are caught per-command.
REVIEW_PROMPT_TEMPLATE = """You are reviewing a pull request that another agent wrote, alone \
in a VM, with nobody else looking at it. Whether it merges depends on what you report.

Repository: {repo}
Issue #{number}: {title}
Pull request: {pr_url}
You are checked out on the branch `{branch}`. The base branch is `{base}`.

--- the issue's acceptance criteria ---
{criteria}
--- end criteria ---

--- the issue body the change was written against ---
{body}
--- end issue body ---

The criteria are the contract; the body is context for reading the change. It tells you what
the step was for, where its author expected the work to land, and where its lane ended. Use it
to judge scope — do not mine it for extra gates. A criterion is the only thing that blocks on
its own; prose in the body is not one. Where the body and the criteria disagree, the criteria
win: record the disagreement in `notes` rather than acting on it.

Your job is to find out whether each criterion is true. Not whether the code looks correct,
not whether you would have written it that way — whether the criterion holds.

The rule that matters: **every verdict needs evidence you produced by running something.**
Evidence is a command and its output, a test name that passed, or a `file:line` you read. A
criterion you did not verify is `cannot_verify`, and `cannot_verify` counts the same as
`not_met`. Never mark something `met` because the code appears to do it — you have a whole VM
here, so run it instead.

How to check each criterion, by its `mode`:

- `test` — run the test named in `verify` on this branch. It must pass. Then confirm it would
  have caught the problem, by running it against the code as it was before this change:

      git rev-parse HEAD > /tmp/head.txt
      git checkout {base_sha} 2>/dev/null
      git checkout $(cat /tmp/head.txt) -- <the test file(s) from verify>
      <run the same test command>          # this MUST fail
      git checkout $(cat /tmp/head.txt)
      git checkout -B {branch} $(cat /tmp/head.txt)

  A new test that passes against the old code proves nothing: either the criterion was already
  satisfied and this change did not do it, or the test asserts nothing. Report the criterion
  `not_met` if that happens, and say which of the two it looked like. Skip this step only for
  criteria marked `regression: true`, which exist to prove old behaviour still works.
- `probe` and `structure` — run the command in `verify`. Exit status 0 is `met`, anything else
  is `not_met`. Quote the command and its output as evidence.
- `inspect` — read what `verify` points at and report what you found. This is the one mode that
  cannot block a merge, so be useful rather than cautious: say what is there and what is missing.

Also run the repo's own fast checks once, as described here:

{project_notes}

If any of them fail, that is a finding regardless of the criteria.

Then look for two specific things and report them as findings if present:

1. **Scope creep.** Map every changed file (`git diff --name-only {base_sha}...HEAD`) to a
   criterion, to the issue's `## Task`, or to its `## Where this goes` map if it has one.
   Files that map to none of those are findings. Two things about that map, when present:
   - It is **advisory**. It was written before the code existed, and the issue itself tells the
     builder to follow the repo where the two disagree. So a changed file that is not on the
     map is a reason to look closer, not a finding by itself.
   - A file that is **on** the map but was never touched belongs in `notes`: either the step is
     unfinished or the map was wrong. Both are worth knowing; neither blocks on its own.
   Anything the issue's `## Boundaries` section puts in its `Never:` lane **is** a finding if
   the change does it. That is the issue's own written rule, not an opinion you formed.
2. **Rules broken.** Check the change against the repo's own written rules — `CLAUDE.md`,
   `docs/adr/*`, `.mem/`. Only rules that are actually written down. Do not invent a
   convention and then report the code for violating it; if it is not written anywhere, it is
   not a finding.

When you are done, write your verdict to **/tmp/factory-verdict.json** and nothing else:

```json
{{
  "verdict": "approve" | "request_changes",
  "criteria": [
    {{"id": "AC1", "status": "met" | "not_met" | "cannot_verify",
      "evidence": "the command you ran and what it printed, or file:line"}}
  ],
  "findings": ["one line each — what is wrong and where"],
  "notes": ["anything advisory that should not block"]
}}
```

Include every criterion by its `id`, including `inspect` ones. Say `request_changes` if any
criterion is not met or you found something that should block; `approve` only if you actually
verified everything. Do not modify the branch, do not commit, and do not open or close
anything on GitHub — writing that file is the entirety of your output.
"""


# The setup both dispatch scripts open with: get a checkout of the assigned repo, make git
# willing to work in a directory it did not create, say who is committing, and get the remote
# refs. It lives in one constant because both scripts need it identical — a review VM that
# resolves the branch differently from the build VM that wrote it reviews something nobody
# built.
#
# The run brings its own repo. `golden-copy` carries tooling and auth and no checkout at all,
# so this clones — and the warm tier is the same script taking the other branch: a
# `golden-<repo-slug>` with the repo already in place skips the clone, which is all "warm" has
# ever meant. Writing it as one script rather than two is what lets a repo be dispatched before
# it has been provisioned.
#
# `--filter=blob:none` rather than `--depth 1`: reviews need real ancestry (they diff against
# a merge base) and agents run `git log`, but the file contents of history nobody reads can be
# fetched on demand.
PRELUDE = r"""
[ -n "$FACTORY_REPO" ] || { echo "FACTORY: no repo assigned" >&2; exit 90; }
workdir="${FACTORY_WORKDIR:-$HOME/work}"
dir="$workdir/${FACTORY_REPO##*/}"

# The pre-clone override, kept for one release: a golden built the old way holds its single
# checkout at FACTORY_REPO_DIR. Taken only when it really is a checkout of the assigned repo,
# because the alternative — working in whatever repo that directory happens to hold — is a run
# that pushes one repo's branch into another.
if [ -n "$FACTORY_REPO_DIR" ] && [ -d "$FACTORY_REPO_DIR/.git" ] && \
   git -C "$FACTORY_REPO_DIR" remote get-url origin 2>/dev/null | grep -qi "$FACTORY_REPO"; then
  dir="$FACTORY_REPO_DIR"
fi

if [ -d "$dir/.git" ]; then
  echo "FACTORY: reusing the checkout at $dir"
else
  echo "FACTORY: cloning $FACTORY_REPO into $dir"
  mkdir -p "$workdir" || { echo "FACTORY: cannot create $workdir" >&2; exit 90; }
  gh repo clone "$FACTORY_REPO" "$dir" -- --filter=blob:none \
    || { echo "FACTORY: clone of $FACTORY_REPO failed" >&2; exit 90; }
fi

cd "$dir" || { echo "FACTORY: workspace unusable: $dir" >&2; exit 90; }
git config --global --add safe.directory "$dir" 2>/dev/null || true
git config user.name  "software-factory" 2>/dev/null || true
git config user.email "factory@users.noreply.github.com" 2>/dev/null || true
echo "FACTORY: fetching origin"
git fetch --prune origin || { echo "FACTORY: git fetch failed" >&2; exit 91; }
"""

# Fail fast when the machine's toolchain does not match what the repo pins. This is not
# cosmetic: two npm majors disagree about what belongs in a lock file, so a mismatched golden
# has its agents silently write lock files CI cannot install — which surfaces two steps later
# as an unrelated-looking CI failure rather than as the version problem it is. That exact
# mismatch killed CI on fourteen consecutive commits before anyone noticed. Skipped when the
# repo pins nothing or the runtime is absent, so this stays harmless for non-Node projects.
#
# Shared by both scripts but deliberately not part of PRELUDE: it reads the pin out of the
# working tree, so it has to run after each script has checked its own branch out. Read the pin
# before the checkout and you assert against whatever commit the machine happened to be sitting
# on, which is exactly the stale answer this guard exists to catch.
#
# It repairs before it refuses. A warm golden is pre-matched to its own repo's pin, but the
# base image serves every repo the deployment watches and they do not agree on a Node version.
# So on the base a mismatch is ordinary and the fix is to install what the repo asks for. Only a mismatch that survives the install is fatal — and the old advice,
# "the golden needs rebuilding", is no longer the right sentence for it.
NODE_GUARD = r"""
if [ -f .nvmrc ] && command -v node > /dev/null 2>&1; then
  want=$(tr -dc '0-9.' < .nvmrc | cut -d. -f1)
  have=$(node -v | tr -d 'v' | cut -d. -f1)
  if [ -n "$want" ] && [ "$want" != "$have" ] && command -v fnm > /dev/null 2>&1; then
    echo "FACTORY: node $have is not the pinned $want; installing it"
    eval "$(fnm env 2>/dev/null)" 2>/dev/null || true
    fnm use --install-if-missing > /dev/null 2>&1 || true
    have=$(node -v | tr -d 'v' | cut -d. -f1)
  fi
  if [ -n "$want" ] && [ "$want" != "$have" ]; then
    echo "FACTORY: node $have does not match the pinned $want and could not be repaired" >&2
    exit 93
  fi
fi
"""

VM_SCRIPT = PRELUDE + r"""
# On a retry, resume the branch a previous attempt pushed to rather than resetting to the base
# and throwing that work away. The VM is always fresh; the branch is what carries work forward.
# Only on a retry: a first attempt always starts from the base, so re-queueing an issue whose
# branch is still lying around from an earlier merged PR gets a clean start, not a resurrection.
if [ "$FACTORY_ATTEMPT" -gt 1 ] && git rev-parse --verify --quiet "origin/$FACTORY_BRANCH" > /dev/null; then
  echo "FACTORY: resuming $FACTORY_BRANCH from origin/$FACTORY_BRANCH"
  git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BRANCH" || { echo "FACTORY: checkout failed" >&2; exit 92; }
else
  echo "FACTORY: checking out $FACTORY_BRANCH from origin/$FACTORY_BASE"
  git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BASE" || { echo "FACTORY: checkout failed" >&2; exit 92; }
fi
""" + NODE_GUARD + r"""
echo "FACTORY: starting agent"
# The rollback for this step, and nothing more: a golden captured before the wrapper existed
# still launches the way the control plane used to launch it, and exits with the agent's own
# status. It goes away once every golden carries factory-agent.
command -v factory-agent >/dev/null 2>&1 || { claude -p "$FACTORY_PROMPT" \
  --effort "$FACTORY_AGENT_EFFORT" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null; exit $?; }
echo "FACTORY-MANIFEST $(tr -d '\n' < /etc/factory/agent.json 2>/dev/null)"
exec factory-agent
"""


def dispatch_env(
    repo: str,
    branch: str,
    base: str,
    prompt: str,
    run_id: str,
    number: int,
    vm_name: str,
    attempt: int | None = None,
    kind: str = "build",
) -> dict:
    """Everything a run VM is told, for either kind of run.

    One builder because the two paths must agree: a review VM that clones a different repo,
    or authenticates as somebody else, than the build VM whose work it is checking is not
    reviewing that work. The differences between them are the two arguments — a build run
    counts attempts, a review run labels itself in the trace — and everything else is shared
    by construction rather than by two people remembering to edit both.

    `GH_TOKEN` is the control plane's own durable credential and covers the clone, the push
    and `gh pr create` from one source. The golden's `gh` login stays as the fallback for a
    deployment that has not set one, which is why an empty token is left out entirely rather
    than exported as an empty string that would shadow it.
    """
    env = {
        # Which repo this run is for. The golden no longer knows.
        "FACTORY_REPO": repo,
        "FACTORY_WORKDIR": settings.workdir,
        # The pre-clone override, honoured only when it holds this repo. See PRELUDE.
        "FACTORY_REPO_DIR": settings.repo_dir,
        "FACTORY_BRANCH": branch,
        "FACTORY_BASE": base,
        "FACTORY_PROMPT": prompt,
        "FACTORY_AGENT_EFFORT": settings.agent_effort,
        # How long a single shell command may run before the agent's own tooling moves it to
        # the background. The default (120s) is shorter than an ordinary build or test run
        # here, and a backgrounded command is what the agent then waits on forever — in
        # headless mode (`claude --print`) there is no loop to deliver the notification it
        # expects. Generous values keep everything legitimate in the foreground; a genuinely
        # stuck command is bounded by FACTORY_RUN_TIMEOUT.
        "BASH_DEFAULT_TIMEOUT_MS": str(settings.bash_default_timeout * 1000),
        "BASH_MAX_TIMEOUT_MS": str(settings.bash_max_timeout * 1000),
        # Correlation key: every run carries its id so traces can attach without changing
        # the dispatch contract later.
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"run.id={run_id},issue={repo}#{number},repo={repo},vm={vm_name}"
            + (",kind=review" if kind == "review" else "")
        ),
    }
    if attempt is not None:
        env["FACTORY_ATTEMPT"] = str(attempt)
    if settings.github_token:
        env["GH_TOKEN"] = settings.github_token
    # Durable auth for the agent itself, overriding the golden's expiring OAuth.
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
    return env


# A shell name, so a key that could not be one is never pasted into a command.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def export_prelude(env: dict) -> str:
    """Shell that turns the assignments `stream_exec(env=...)` makes into a real environment.

    boxd's wire protocol has no env field. Its SDK says so and works around it by prefixing
    the command with `K=v` assignments (`boxd.resources.machines._exec_init`), which is
    correct for the one-line command that comment has in mind and quietly wrong for a script.
    In front of a *multi-line* command the prefix lands on a line of its own, and a line of
    bare assignments sets shell variables rather than exporting them. The script itself then
    reads `$FACTORY_REPO` perfectly well while every process it starts inherits none of them.

    That is not a theoretical gap. It is what stopped the factory on 2026-08-19: `gh repo
    clone` could not see `GH_TOKEN`, failed with "please run gh auth login", and the prelude
    exited 90 before the agent ever started — while the same log line printed the repo name
    it had just read from the very variable `gh` could not see, which is what made it look
    like the environment had arrived.

    Names come from the dict, so a variable added to `dispatch_env` cannot be forgotten here.
    """
    names = [k for k in env if _ENV_NAME.fullmatch(k)]
    if not names:
        return ""
    # Leading newline so the SDK's prefix stays a line of pure assignments; without it the
    # assignments would attach to this `export` as a command prefix and the fix would depend
    # on how the shell scopes assignments to a special builtin.
    return "\nexport " + " ".join(names) + "\n"


# --------------------------------------------------------------------------- manifest

# What a golden says about itself, on one line, immediately before it becomes the agent.
# The prefix is what lets the stream parser tell the announcement from the agent's own
# output without guessing: every other line on stdout belongs to the agent.
MANIFEST_PREFIX = "FACTORY-MANIFEST"

# Where a transcript lives when the manifest names no path. Claude Code's own layout, kept
# as the default so a golden built before any of this still gives its transcript up.
CLAUDE_TRANSCRIPT_GLOB = '"$HOME"/.claude/projects/*/*.jsonl'


def parse_manifest(line: str) -> dict:
    """The manifest a golden announced, or `{}` when the line does not carry a usable one.

    Never raises, for the same reason the transcript salvage is best-effort: the manifest
    says where the transcript is and which agent to credit, and a run whose real work
    succeeded must not be failed by a hand-edited `/etc/factory/agent.json`, a wrapper that
    forgot to strip the file's newlines, or an image that echoed the prefix and nothing
    after it. `{}` is a complete answer — every reader of a manifest has a default.
    """
    _, _, payload = str(line or "").partition(MANIFEST_PREFIX)
    try:
        manifest = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def transcript_glob(manifest: dict | None = None) -> str:
    """The shell glob that finds the agent's session transcript inside the VM.

    The manifest wins when it names one, because only the golden knows where its agent
    writes; an agent that is not Claude Code does not keep a `~/.claude` directory at all.
    """
    named = (manifest or {}).get("transcript")
    return named.strip() if isinstance(named, str) and named.strip() else CLAUDE_TRANSCRIPT_GLOB


# --------------------------------------------------------------------------- log


VERDICT_PATH = "/tmp/factory-verdict.json"

# Review runs check out the PR branch and change nothing. No `-B` from the base, no push: a
# reviewer that can write to the branch is a reviewer that can make its own findings go away.
REVIEW_SCRIPT = PRELUDE + r"""
rm -f /tmp/factory-verdict.json
echo "FACTORY: checking out $FACTORY_BRANCH for review"
git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BRANCH" || { echo "FACTORY: checkout failed" >&2; exit 92; }
""" + NODE_GUARD + r"""
echo "FACTORY: starting reviewer"
command -v factory-agent >/dev/null 2>&1 || { claude -p "$FACTORY_PROMPT" \
  --effort "$FACTORY_AGENT_EFFORT" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null; exit $?; }
echo "FACTORY-MANIFEST $(tr -d '\n' < /etc/factory/agent.json 2>/dev/null)"
exec factory-agent
"""


def decide(verdict: dict | None, criteria: list[dict]) -> tuple[bool, str, list[str]]:
    """Turn a reviewer's verdict into a merge decision. Returns (approved, why, findings).

    The control plane decides, not the agent. The agent reports per-criterion status with
    evidence; this recomputes the outcome from those statuses, so an "approve" cannot survive a
    criterion the agent itself marked `not_met`. It can still request changes for something
    outside the criteria — a reviewer may block for a reason nobody thought to write down, but
    it may not wave one through.

    Fails closed everywhere: no verdict file, unparseable JSON, or a criterion the reviewer
    simply didn't mention all mean "not approved".
    """
    if not isinstance(verdict, dict):
        return False, "no usable verdict from the reviewer", []

    findings = [str(f) for f in (verdict.get("findings") or [])][:20]
    reported = {
        str(c.get("id")): c
        for c in (verdict.get("criteria") or [])
        if isinstance(c, dict) and c.get("id")
    }

    blocking = [c for c in criteria if c.get("mode") in BLOCKING_MODES]
    unmet, missing = [], []
    for criterion in blocking:
        cid = str(criterion["id"])
        got = reported.get(cid)
        if got is None:
            missing.append(cid)
        elif got.get("status") != "met":
            unmet.append(f"{cid} {got.get('status', 'unknown')}")

    if missing:
        return False, f"reviewer did not report on {', '.join(missing)}", findings
    if unmet:
        return False, f"criteria not met: {', '.join(unmet)}", findings
    if verdict.get("verdict") != "approve":
        return False, "reviewer requested changes", findings
    return True, f"{len(blocking)} criteria met", findings


def merge_outcome(auto_merge: bool, merged: bool, ci_failure: str | None) -> str:
    """What an approved review should do with the pull request: "done", "fix" or "human".

    Kept separate from the code that acts on it because the interesting part is the routing,
    not the doing, and the routing is where this went wrong: every ending that was not a
    merge used to be labelled `agent:done`, which left broken pull requests sitting open
    under issues that claimed to be finished.

    - "done"  — merged, or auto-merge is off and a human was always going to take it from here
    - "fix"   — CI ran and came back red, which is a defect a fix run can be sent back for
    - "human" — not merged and no failure to work from: pending, unreachable, or refused
    """
    if merged or not auto_merge:
        return "done"
    return "fix" if ci_failure else "human"


class RunLog:
    """Append-only, human-readable log for one run. The UI tails this file over SSE."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("a", encoding="utf-8", buffering=1)

    def write(self, line: str) -> None:
        self._fh.write(line.rstrip("\n") + "\n")

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def _format_system(event: dict) -> list[str]:
    if event.get("subtype") != "init":
        return []
    return [f"[agent] session {event.get('session_id', '?')} started"]


def _format_assistant(event: dict) -> list[str]:
    lines: list[str] = []
    for block in event.get("message", {}).get("content", []) or []:
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                lines.extend(text.splitlines())
        elif btype == "tool_use":
            name = block.get("name", "tool")
            supplied = block.get("input", {}) or {}
            hint = (
                supplied.get("command")
                or supplied.get("file_path")
                or supplied.get("pattern")
                or supplied.get("skill")
                or ""
            )
            hint = str(hint).replace("\n", " ")
            lines.append(f"[tool] {name}{': ' + hint[:160] if hint else ''}")
    return lines


def _format_user(event: dict) -> list[str]:
    return [
        "[tool] -> error"
        for block in event.get("message", {}).get("content", []) or []
        if block.get("type") == "tool_result" and block.get("is_error")
    ]


def _format_finished(event: dict) -> list[str]:
    usage = event.get("usage", {}) or {}
    tokens = ""
    if usage:
        tokens = (
            f" | tokens in={usage.get('input_tokens', '?')} "
            f"out={usage.get('output_tokens', '?')}"
        )
    return [
        f"[agent] finished: {event.get('subtype', 'done')}"
        f" | {event.get('num_turns', '?')} turns{tokens}"
    ]


# Which event types this log knows how to read, and how. A table rather than a chain of
# comparisons for one reason: whether an event is *recognised* is now a question the
# stream has to ask, so that an event from a runtime whose shape nobody has written a
# formatter for is logged raw instead of vanishing (see `stream_lines`). Every key here
# is Claude Code's vocabulary — a second runtime adds its own names, and nothing outside
# this table has to change.
FORMATTERS = {
    "system": _format_system,
    "assistant": _format_assistant,
    "user": _format_user,
    "result": _format_finished,
}

# How much of an unrecognised event goes into the log. Enough to see what it was; not so
# much that one chatty runtime buries the run in its own protocol.
RAW_EVENT_MAX = 300


def format_event(event: dict) -> list[str]:
    """Turn one agent event into readable log lines, or none if it says nothing.

    Written defensively: the event schema is not a stable contract, so an event this
    does not recognise produces nothing rather than raising. `stream_lines` is what
    decides such an event is worth a raw line anyway.
    """
    formatter = FORMATTERS.get(event.get("type"))
    return formatter(event) if formatter else []


def stream_lines(event: dict, raw: str) -> list[str]:
    """What one JSON line from the agent contributes to the run log.

    The fallback is the whole point. An agent whose events no formatter recognises used
    to stream a *completely empty* log to the UI while working perfectly — the failure
    that hides itself at exactly the moment somebody is watching a new agent to see
    whether it works. A truncated raw line is unlovely and always better than silence.

    A recognised event that produces no lines stays silent, because that is a formatter
    saying "nothing here worth reading", not an unread event: half of a normal run is
    tool results nobody needs to see.
    """
    if event.get("type") in FORMATTERS:
        return format_event(event)
    raw = raw.strip()
    return [f"[agent] {raw[:RAW_EVENT_MAX]}{'…' if len(raw) > RAW_EVENT_MAX else ''}"]


# --------------------------------------------------------------------------- run


@dataclass(frozen=True)
class MergeAttempt:
    """What came of trying to merge one pull request.

    A record rather than a tuple because it carries four facts that are easy to confuse, and
    two of them are only meaningful in one ending each:

    - `merged`     — did it land
    - `ci_failure` — set *only* when checks ran and came back red, which is the one ending a
                     fix run can act on. Everything else leaves it None, because "we could
                     not find out" and "CI says no" call for different responses.
    - `why`        — always populated, in plain language, for the issue comment and the runs
                     table. This used to be dropped for every non-CI ending, which is how a
                     stranded pull request came to be recorded as "cause unknown" when GitHub
                     had in fact said exactly what was wrong.
    - `head_sha`   — the commit the checks were verified on, so the caller can fetch that
                     commit's failing logs rather than whatever is at the head by then.
    """

    merged: bool
    ci_failure: str | None
    why: str
    head_sha: str | None = None


async def _merge(repo: str, pr_url: str, base: str, log: RunLog) -> MergeAttempt:
    """Merge a PR once its checks are green. Never raises — a merge we skip is always safer
    than one we force, and the PR simply stays open for a human.

    The merge itself retries while GitHub is unavailable (see `github.merge_pr`) and is
    pinned to the sha the checks passed on, so "auto-merge once the tests are done" holds
    even across an outage, and can still only ever land the commit that was tested.
    """
    try:
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        sha: str | None = None
        green, why, failed = True, "checks not required", []
        if settings.merge_require_checks:
            # The merge API waits for nothing, so without this the PR is merged seconds after
            # `gh pr create` — before CI has even started. Every run forks from main, so a red
            # main propagates into all of them.
            log.write(f"[factory] waiting for checks on PR #{pr_number}")
            sha = await github.pr_head_sha(repo, pr_number)
            green, why, failed = await github.checks_green(
                repo, sha, timeout=settings.merge_check_timeout
            )
        if not green:
            log.write(f"[factory] not merging PR #{pr_number}, left open: {why}")
            return MergeAttempt(False, why if failed else None, why, sha)
        await github.merge_pr(repo, pr_number, sha=sha)
        log.write(f"[factory] merged PR #{pr_number} into {base} ({why})")
        return MergeAttempt(True, None, why, sha)
    except Exception as exc:  # noqa: BLE001
        # Keep the reason. GitHub nearly always says what was wrong, and throwing that away
        # left a human with an unmerged pull request and nothing to go on.
        detail = " ".join(str(exc).split())[:200] or type(exc).__name__
        why = f"the merge call failed — {detail}"
        log.write(f"[factory] merge failed (PR left open): {exc!r}")
        return MergeAttempt(False, None, why, None)


async def _fix_cycle(
    repo: str,
    number: int,
    cycle: int,
    log: RunLog,
    *,
    reason: str,
    detail: str,
    going_back: str,
    giving_up: str,
) -> None:
    """Send a pull request back for another pass, or stop if the cycle budget is spent.

    Both things that can send one back — a reviewer that requested changes, and a CI run that
    came back red — want identical mechanics: reuse the branch so the fix builds on commits
    already pushed rather than starting over, carry the reason into the next prompt, and cap
    how many times the factory may try again unsupervised. Only the wording differs, so only
    the wording is a parameter.
    """
    if cycle < settings.max_review_cycles:
        log.write(f"[factory] {reason}; fix run {cycle + 1} scheduled")
        await _mirror_issue(repo, number, None, [], log, comment=going_back)
        await create(
            repo,
            number,
            attempt=cycle + 1,
            prior_error=reason,
            prior_log=detail,
            review_cycle=cycle + 1,
        )
    else:
        log.write(f"[factory] {reason}; cycle budget spent, halting")
        await _mirror_issue(
            repo,
            number,
            github.LABEL_BLOCKED,
            [github.LABEL_RUNNING, github.LABEL_QUEUED],
            log,
            comment=giving_up,
        )


async def _fail_run(
    run_id: str,
    repo: str,
    issue: dict,
    attempt: int,
    reason: str,
    log: RunLog,
    pr_url: str | None = None,
) -> None:
    """Mark a run failed, then either schedule a retry or halt the issue.

    Retry ceiling is `settings.max_attempts`. The retry is created *before* this run is
    marked terminal, so the repo never looks idle to the poller in the gap — which would let
    the next issue in the sequence jump the queue.
    """
    number = issue["number"]
    scheduled = False
    if attempt < settings.max_attempts:
        try:
            await create(
                repo,
                number,
                attempt=attempt + 1,
                prior_error=reason,
                prior_log=_log_tail(run_id),
            )
            scheduled = True
        except Exception as exc:  # noqa: BLE001 - can't retry -> fall through and halt
            log.write(f"[factory] could not schedule retry: {exc!r}")

    await db.update_run(
        run_id, status="failed", pr_url=pr_url, error=reason, finished_at=db.utcnow()
    )

    if scheduled:
        nxt = attempt + 1
        log.write(f"[factory] attempt {attempt} failed ({reason}); retry {nxt}/{settings.max_attempts} scheduled")
        await _mirror_issue(
            repo,
            number,
            None,
            [],
            log,
            comment=f"Attempt {attempt} failed ({reason}). Retrying — attempt {nxt} of {settings.max_attempts}.",
        )
    else:
        log.write(f"[factory] attempt {attempt} failed ({reason}); no retries left, halting")
        await _mirror_issue(
            repo,
            number,
            github.LABEL_FAILED,
            [github.LABEL_RUNNING, github.LABEL_QUEUED],
            log,
            comment=f"Failed after {attempt} attempt(s) ({reason}). Halting — needs a human.",
        )


async def create(
    repo: str,
    issue_number: int,
    attempt: int = 1,
    prior_error: str | None = None,
    prior_log: str | None = None,
    review_cycle: int = 1,
) -> str:
    """Register a run and schedule it. Returns the run id immediately.

    Which snapshot the run boots is resolved when it actually starts, not now, so a golden
    provisioned while the run sat in the queue is still picked up.

    The `agent` column is seeded with the one agent this deployment runs and then overwritten
    by whatever the golden announces in its manifest on the way in. It is a record of what did
    the work, not an instruction — nothing about dispatch reads it, and no caller chooses it.

    `attempt` > 1 marks a retry: the previous attempt's error and log tail are woven into
    the prompt so the agent diagnoses the failure instead of repeating it. Retries reuse the
    same branch, so the whole chain resolves into one pull request.
    """
    issue = await github.get_issue(repo, issue_number)
    run_id = uuid.uuid4().hex
    branch = f"factory/issue-{issue_number}"
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()

    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=issue_number,
        issue_title=issue["title"],
        branch=branch,
        status="queued",
        attempt=attempt,
        agent=agents.DEFAULT_AGENT,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )

    task = asyncio.create_task(
        _guarded(
            run_id, repo, issue, branch,
            attempt, prior_error, prior_log, review_cycle,
        )
    )
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


async def create_review(
    repo: str,
    issue_number: int,
    pr_url: str,
    branch: str,
    cycle: int = 1,
) -> str:
    """Register and schedule a review of the pull request a build run opened."""
    issue = await github.get_issue(repo, issue_number)
    run_id = uuid.uuid4().hex
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()

    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=issue_number,
        issue_title=issue["title"],
        branch=branch,
        status="queued",
        kind="review",
        attempt=cycle,
        agent=agents.DEFAULT_AGENT,
        pr_url=pr_url,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )

    task = asyncio.create_task(
        _guarded_review(run_id, repo, issue, branch, pr_url, cycle)
    )
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


async def _salvage_usage(run_id: str, log: RunLog) -> None:
    """Backstop the run's ledger entry from the telemetry rows.

    A run only reports its own usage in the final `result` event, so a timeout, a crash
    or a cancelled task used to leave `cost_usd = NULL` — real money spent, recorded as
    nothing, on exactly the runs worth understanding. The per-call rows were written as
    the run went, so the spend is still there; this reads it back and fills the gap.

    Only ever fills a gap: a run that reported its own numbers keeps them, so the
    runtime's figure stays authoritative wherever it exists and the derived one is
    strictly a fallback. Best effort by design — never raises.
    """
    try:
        run = await db.get_run(run_id)
        if not run or run.get("cost_usd") is not None:
            return
        derived = await Recorder(run_id).totals()
        if not derived:
            return
        await db.update_run(run_id, **derived)
        log.write(
            f"[factory] usage recovered from telemetry: "
            f"${derived.get('cost_usd') or 0:.2f}, "
            f"{derived.get('tokens_out') or 0} output tokens"
        )
    except asyncio.CancelledError:
        # Called from a `finally` that may already be unwinding a cancelled run. The
        # caller has re-raised, so cancellation still propagates; give up on the number
        # rather than block the teardown.
        return
    except Exception as exc:  # noqa: BLE001 - a missing number must not mask the failure
        log.write(f"[factory] usage salvage skipped: {exc!r}")


async def _guarded_review(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    pr_url: str,
    cycle: int,
) -> None:
    log = RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with semaphore():
            await _execute_review(run_id, repo, issue, branch, pr_url, log, cycle)
    except asyncio.CancelledError:
        log.write("[factory] review cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:  # noqa: BLE001
        # A review that crashes must not merge anything, and must not silently strand the PR
        # either. Leave it open, labelled, for a human.
        log.write(f"[factory] review failed: {exc!r}")
        reason = (
            f"timed out after {settings.run_timeout}s"
            if isinstance(exc, asyncio.TimeoutError)
            else f"crashed: {str(exc)[:200] or type(exc).__name__}"
        )
        await db.update_run(
            run_id, status="failed", error=reason, finished_at=db.utcnow()
        )
        await _mirror_issue(
            repo,
            issue["number"],
            github.LABEL_BLOCKED,
            [github.LABEL_RUNNING],
            log,
            comment=f"Review run {reason}. {pr_url} is open and unreviewed — needs a human.",
        )
    finally:
        await _salvage_usage(run_id, log)
        log.close()


async def _guarded(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    attempt: int,
    prior_error: str | None,
    prior_log: str | None,
    review_cycle: int = 1,
) -> None:
    log = RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with semaphore():
            await _execute(
                run_id, repo, issue, branch, log,
                attempt, prior_error, prior_log, review_cycle,
            )
    except asyncio.CancelledError:
        # A human stopped this run. Do not retry — cancellation is a decision, not a failure.
        log.write("[factory] run cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:  # noqa: BLE001 - the UI is where failures get reported
        log.write(f"[factory] run failed: {exc!r}")
        # str() on an asyncio.TimeoutError is empty, which used to record the least useful
        # error in the table ("crashed: ") for the one failure mode that says exactly what
        # happened. Name it, and fall back to the exception class for anything else silent.
        if isinstance(exc, asyncio.TimeoutError):
            reason = f"timed out after {settings.run_timeout}s"
        else:
            reason = f"crashed: {str(exc)[:200] or type(exc).__name__}"
        await _fail_run(run_id, repo, issue, attempt, reason, log)
    finally:
        await _salvage_usage(run_id, log)
        log.close()


async def _execute(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    log: RunLog,
    attempt: int = 1,
    prior_error: str | None = None,
    prior_log: str | None = None,
    review_cycle: int = 1,
) -> None:
    boxd = client()
    machine = None
    reaped = False
    number = issue["number"]
    try:
        base = await github.default_branch(repo)
        notes = await project_notes(repo, base)
        prompt = build_prompt(
            repo, issue, branch, base, notes, attempt, prior_error, prior_log
        )

        # ---- claim: mirror the pickup onto the issue for anyone watching on GitHub
        which = f" (attempt {attempt} of {settings.max_attempts})" if attempt > 1 else ""
        started = f"Factory run started on branch `{branch}`{which}."
        link = _run_link(run_id)
        if link:
            started += f"\n\nLive log: {link}"
        await _mirror_issue(
            repo, number, github.LABEL_RUNNING, [github.LABEL_QUEUED], log, comment=started
        )

        # ---- provision
        await db.update_run(run_id, status="forking", started_at=db.utcnow())
        vm_name = f"{RUN_PREFIX}{run_id[:8]}"
        await headroom(boxd, log)
        source = await source_for(boxd, repo, log)
        await db.update_run(run_id, golden=source)
        log.write(f"[factory] provisioning {vm_name} from {source}")
        machine = await _provision(boxd, source, vm_name)
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        log.write(f"[factory] {machine.name} ready ({machine.id})")

        # ---- run the agent
        await db.update_run(run_id, status="running")
        env = dispatch_env(
            repo=repo,
            branch=branch,
            base=base,
            prompt=prompt,
            run_id=run_id,
            number=number,
            vm_name=machine.name,
            attempt=attempt,
        )
        exit_code, usage, manifest = await asyncio.wait_for(
            _stream(boxd, machine.id, env, log, run_id), timeout=settings.run_timeout
        )
        log.write(f"[factory] agent exited {exit_code}")
        await db.update_run(
            run_id,
            exit_code=exit_code,
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            cost_usd=usage.get("cost_usd"),
        )

        # ---- collect
        pr_url = await github.find_pr(repo, branch)
        if pr_url:
            log.write(f"[factory] pull request: {pr_url}")
        else:
            log.write("[factory] no pull request found for this branch")
        await _salvage_transcript(boxd, machine.id, run_id, log, manifest)

        ok = exit_code == 0 and pr_url is not None
        review_next = False
        if ok:
            criteria = parse_criteria(issue.get("body") or "")
            review_next = settings.review_enabled and bool(criteria)
            merged = False
            if review_next:
                # Hand the PR to a reviewing agent instead of merging on the builder's word.
                # An issue with no machine-readable criteria has nothing to review against, so
                # it falls through to the old path rather than being reviewed against a guess.
                log.write(f"[factory] {len(criteria)} acceptance criteria; queueing review")
            elif settings.auto_merge:
                # Merge now so the next issue in the sequence branches from a main that
                # already contains this issue's work.
                if not criteria:
                    log.write("[factory] issue carries no acceptance criteria; skipping review")
                merged = (await _merge(repo, pr_url, base, log)).merged
            await db.update_run(
                run_id, status="succeeded", pr_url=pr_url, error=None, finished_at=db.utcnow()
            )
            outcome = f"Factory run finished. Pull request: {pr_url}"
            if merged:
                outcome += " (auto-merged)"
            elif review_next:
                outcome += " — under review"
            await _mirror_issue(
                repo,
                number,
                None if review_next else github.LABEL_DONE,
                [] if review_next else [github.LABEL_RUNNING],
                log,
                comment=outcome,
            )
        else:
            reason = f"exit {exit_code}, {'no ' if not pr_url else ''}pull request"
            await _fail_run(run_id, repo, issue, attempt, reason, log, pr_url=pr_url)

        # ---- reap
        keep = not ok and settings.keep_failed
        if not keep:
            # Brief drain so any buffered telemetry flushes before the VM disappears.
            await asyncio.sleep(3)
        await reap(boxd, machine, log, keep=keep)
        reaped = True

        # ---- hand off to review, once this run's VM is gone and its slot is free
        if ok and review_next:
            try:
                await create_review(repo, number, pr_url, branch, cycle=review_cycle)
            except Exception as exc:  # noqa: BLE001 - an unreviewed PR beats a lost one
                log.write(f"[factory] could not queue review: {exc!r}")
    finally:
        # Everything above can raise: `wait_until_ready` times out, the stream drops, the run
        # hits `FACTORY_RUN_TIMEOUT`, a GitHub call fails after the agent finished. Each of
        # those used to leave a machine running until its two-hour self-destruct. The reap in
        # the body is the ordinary path and this is the one that catches the rest; `reaped`
        # keeps it from running twice.
        if not reaped:
            await reap(boxd, machine, log, keep=settings.keep_failed)
        await boxd.close()


async def _execute_review(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    pr_url: str,
    log: RunLog,
    cycle: int,
) -> None:
    """Fork a VM, review the PR against the issue's criteria, then merge it or send it back."""
    number = issue["number"]
    base = await github.default_branch(repo)
    criteria = parse_criteria(issue.get("body") or "")
    boxd = client()
    machine = None
    reaped = False
    try:
        await db.update_run(run_id, status="forking", started_at=db.utcnow())
        vm_name = f"{REVIEW_PREFIX}{run_id[:8]}"
        log.write(f"[factory] review {cycle}/{settings.max_review_cycles} of {pr_url}")
        await headroom(boxd, log)
        source = await source_for(boxd, repo, log)
        await db.update_run(run_id, golden=source)
        log.write(f"[factory] provisioning {vm_name} from {source}")
        machine = await _provision(boxd, source, vm_name)
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        log.write(f"[factory] {machine.name} ready ({machine.id})")

        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        head_sha = await github.pr_head_sha(repo, pr_number)
        base_sha = await github.merge_base_sha(repo, base, head_sha)

        await db.update_run(run_id, status="running")
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            project_notes=await project_notes(repo, base),
            repo=repo,
            number=number,
            title=issue["title"],
            pr_url=pr_url,
            branch=branch,
            base=base,
            base_sha=base_sha,
            criteria=yaml.safe_dump(criteria, sort_keys=False, allow_unicode=True),
            body=issue.get("body") or "(no description given)",
        )
        env = dispatch_env(
            repo=repo,
            branch=branch,
            base=base,
            prompt=prompt,
            run_id=run_id,
            number=number,
            vm_name=machine.name,
            kind="review",
        )

        exit_code, usage, manifest = await asyncio.wait_for(
            _stream(boxd, machine.id, env, log, run_id, script=REVIEW_SCRIPT),
            timeout=settings.run_timeout,
        )
        log.write(f"[factory] reviewer exited {exit_code}")
        verdict = await _read_verdict(boxd, machine.id, log)
        await _salvage_transcript(boxd, machine.id, run_id, log, manifest)

        approved, why, findings = decide(verdict, criteria)
        await db.update_run(
            run_id,
            exit_code=exit_code,
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            cost_usd=usage.get("cost_usd"),
            verdict=json.dumps(verdict) if verdict else None,
            status="succeeded",
            error=None if approved else why,
            finished_at=db.utcnow(),
        )
        log.write(f"[factory] verdict: {'approve' if approved else 'request changes'} — {why}")
        for finding in findings:
            log.write(f"[factory]   finding: {finding}")

        await asyncio.sleep(3)
        await reap(boxd, machine, log)
        reaped = True

        # ---- act on the verdict
        #
        # An approved pull request has three possible endings, and they are not the same
        # thing. Merged is finished. Red CI is a real defect in code a reviewer signed off,
        # which is exactly what a fix run is for. Anything else — checks still pending,
        # GitHub unreachable, the merge itself refused — means we do not know what is wrong,
        # so it stops for a human. Collapsing all three into `agent:done` is how a broken
        # pull request ends up sitting open under an issue that claims to be finished.
        if approved:
            merge_attempt = MergeAttempt(False, None, "auto-merge is off")
            if settings.auto_merge:
                merge_attempt = await _merge(repo, pr_url, base, log)
            outcome = merge_outcome(
                settings.auto_merge, merge_attempt.merged, merge_attempt.ci_failure
            )

            if outcome == "done":
                comment = f"Review passed — {why}. {pr_url}"
                if merge_attempt.merged:
                    comment += " (merged)"
                await _mirror_issue(
                    repo, number, github.LABEL_DONE, [github.LABEL_RUNNING], log, comment=comment
                )
                return

            if outcome == "human":
                await db.update_run(run_id, error=f"not merged: {merge_attempt.why}")
                await _mirror_issue(
                    repo,
                    number,
                    github.LABEL_BLOCKED,
                    [github.LABEL_RUNNING, github.LABEL_QUEUED],
                    log,
                    comment=(
                        f"Review passed — {why} — but the pull request could not be merged: "
                        f"{merge_attempt.why}. Needs a human. {pr_url}"
                    ),
                )
                return

            # Fetch the failing job's log now, while we still know which commit CI judged, and
            # hand it to the fix run. Without it the next agent is told only that a check named
            # `gates` failed, which is equally true of a broken test and of a registry timeout.
            await db.update_run(run_id, error=merge_attempt.ci_failure)
            ci_log = "(head commit unknown, could not fetch logs)"
            if merge_attempt.head_sha:
                ci_log = await github.failing_check_logs(repo, merge_attempt.head_sha)
            log.write(f"[factory] fetched {len(ci_log)} chars of failing check log")
            await _fix_cycle(
                repo,
                number,
                cycle,
                log,
                reason=f"CI failed after an approved review: {merge_attempt.ci_failure}",
                detail=(
                    f"CI failed on the pull request after the review approved it.\n"
                    f"{merge_attempt.ci_failure}\n\n"
                    "The reviewer confirmed every acceptance criterion against real command "
                    "output, so the change itself is sound. What failed is something CI runs "
                    "that this VM does not.\n\n"
                    "The failing job's log is below — read it before you change anything, and "
                    "decide first which kind of failure it is.\n\n"
                    "If it is an infrastructure fault rather than a defect in this change — an "
                    "image pull or network timeout, a runner that died, a rate limit, a service "
                    "that was briefly unavailable — then the code is fine and editing it would "
                    "make things worse. Re-run the job instead and wait for the result:\n"
                    f"  gh run list --branch {branch} --limit 1\n"
                    "  gh run rerun <run-id> --failed\n\n"
                    "If it is a real defect, fix its cause. Do not delete, skip or weaken a "
                    "test to make it pass, and do not edit CI configuration — a gate that "
                    "fails is doing its job.\n\n"
                    f"--- failing check log ---\n{ci_log}"
                ),
                going_back=(
                    f"Review passed, but CI is red ({merge_attempt.ci_failure}). "
                    f"Fixing. {pr_url}"
                ),
                giving_up=(
                    f"Review passed, but CI is still red after {cycle} cycles "
                    f"({merge_attempt.ci_failure}).\n\n"
                    f"Stopping — needs a human. {pr_url}"
                ),
            )
            return

        detail = "\n".join(f"- {f}" for f in findings) or "- (no specific findings recorded)"
        await _fix_cycle(
            repo,
            number,
            cycle,
            log,
            reason=f"review requested changes: {why}",
            detail=f"Reviewer findings:\n{detail}",
            going_back=f"Review {cycle} requested changes ({why}):\n{detail}\n\nFixing.",
            giving_up=(
                f"Review still requesting changes after {cycle} cycles ({why}):\n{detail}"
                f"\n\nStopping — the issue may be wrong rather than the code. {pr_url}"
            ),
        )
    finally:
        # The review path leaks a VM the same way the build path does, and for the same
        # reasons — see the note there.
        if not reaped:
            await reap(boxd, machine, log, keep=settings.keep_failed)
        await boxd.close()


async def _read_verdict(boxd: AsyncBoxd, machine_id: str, log: RunLog) -> dict | None:
    """Read the verdict file out of the VM. Any failure returns None, which `decide` treats
    as "not approved" — a reviewer that produced nothing readable has approved nothing."""
    try:
        result = await boxd.machines.exec(machine_id, f"cat {VERDICT_PATH}", timeout=30)
        raw = getattr(result, "stdout", None) or ""
        if inspect.isawaitable(raw):  # pragma: no cover - SDK shape drift
            raw = await raw
        text = raw.strip()
        if not text:
            log.write("[factory] reviewer wrote no verdict file")
            return None
        # The agent sometimes wraps JSON in a ``` fence despite being asked not to.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n|\n```$", "", text.strip())
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.write(f"[factory] verdict file is not valid JSON: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] could not read verdict: {exc!r}")
        return None


async def _stream(
    boxd: AsyncBoxd,
    machine_id: str,
    env: dict,
    log: RunLog,
    run_id: str,
    script: str = VM_SCRIPT,
) -> tuple[int, dict, dict]:
    """Run the agent, formatting its event stream into the log as it arrives.

    Returns the exit code, the usage captured from the final `result` event
    (input/output tokens and cost), which is what the Runs UI shows as the run's cost,
    and the manifest the golden announced before handing off — `{}` when it announced
    none, which is every golden captured before the wrapper existed.

    The same events also go to the telemetry recorder, which normalizes them into
    per-call rows and writes them as the run proceeds. That is a second consumer of a
    stream we were already parsing, not a second stream: the facts telemetry wants are
    the ones scrolling past here, and the only thing that used to happen to them was
    being formatted into a log line and forgotten.
    """
    buffer = ""
    usage: dict = {}
    manifest: dict = {}
    recorder = Recorder(run_id)
    async with boxd.machines.stream_exec(
        machine_id, command=export_prelude(env) + script, env=env, close_stdin=True
    ) as stream:
        async for chunk in stream.iter_chunks():
            text = chunk.data.decode("utf-8", errors="replace")
            if chunk.is_stderr:
                for line in text.splitlines():
                    if line.strip():
                        log.write(f"[stderr] {line}")
                continue
            buffer += text
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                line = line.strip()
                if not line:
                    continue
                if line.startswith(MANIFEST_PREFIX):
                    # The golden speaking about itself, not the agent speaking. It is
                    # handled before the JSON branch and consumed here: the telemetry
                    # recorder normalizes agent events and would count this as a dropped
                    # one, and logging it raw would put a line of machine handshake in
                    # the middle of a log a human reads.
                    manifest = parse_manifest(line)
                    named = str(manifest.get("agent") or "").strip()
                    # Before a single event is read, which is the only moment an
                    # adapter can still be chosen.
                    events = recorder.use(manifest.get("events"))
                    if manifest:
                        log.write(
                            f"[factory] manifest: agent {named or 'unnamed'}, "
                            f"events {events}, transcript {transcript_glob(manifest)}"
                        )
                    else:
                        log.write("[factory] agent manifest unreadable; using defaults")
                    await db.update_run(
                        run_id,
                        manifest=json.dumps(manifest),
                        # What actually ran, overwriting the default the row was seeded
                        # with. This is the registry: the image says which agent it launches,
                        # and nothing else in the system claims to know.
                        **({"agent": named} if named else {}),
                    )
                    continue
                if line.startswith("{"):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        log.write(line)
                        continue
                    # Asked of every event, because which one carries the run's
                    # figures is the adapter's business. A null adapter answers `{}`
                    # to all of them, and the run records no cost rather than a wrong
                    # one — `_salvage_usage` fills the ledger from rows where there
                    # are any.
                    usage = recorder.summary(event) or usage
                    await recorder.feed(event)
                    for formatted in stream_lines(event, line):
                        log.write(formatted)
                    continue
                log.write(line)
        code = stream.exit_code
        if inspect.isawaitable(code):
            code = await code
    await recorder.close()
    if recorder.dropped:
        log.write(f"[factory] telemetry dropped {recorder.dropped} events")
    return int(code or 0), usage, manifest


async def _salvage_transcript(
    boxd: AsyncBoxd, machine_id: str, run_id: str, log: RunLog, manifest: dict | None = None
) -> None:
    """Copy the agent's session transcript out before the VM is destroyed.

    The live stream is for watching; this file is the complete, replayable record. Best
    effort by design — a missing transcript must never fail an otherwise good run.

    Where to look comes from the manifest, because the control plane no longer knows what
    is running inside the VM. Without that, this reaches for `~/.claude` on every machine
    and quietly salvages nothing from any agent that is not Claude Code.
    """
    script = (
        f'f=$(ls -t {transcript_glob(manifest)} 2>/dev/null | head -1); '
        '[ -n "$f" ] && [ "$(wc -c < "$f")" -lt 20000000 ] && cat "$f" || true'
    )
    try:
        result = await boxd.machines.exec(machine_id, script, timeout=120)
        if result.stdout.strip():
            path = settings.log_dir / f"{run_id}.transcript.jsonl"
            path.write_text(result.stdout, encoding="utf-8")
            await db.update_run(run_id, transcript_path=str(path))
            log.write(f"[factory] transcript saved ({len(result.stdout)} bytes)")
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] transcript salvage skipped: {exc!r}")


def track(run_id: str, task: asyncio.Task) -> None:
    """Register a run's task so `cancel()` can reach it, and forget it when it ends.

    Public because provisioning a golden is a run this module does not start — it is an
    agentless one, so it lives in `control/provision.py` — but it is still a task holding a VM,
    and a task the UI cannot cancel is a VM nobody can stop.
    """
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))


async def cancel(run_id: str) -> bool:
    """Cancel an in-flight run and destroy its VM."""
    run = await db.get_run(run_id)
    if not run or run["status"] in db.TERMINAL:
        return False
    task = _tasks.get(run_id)
    if task:
        task.cancel()
    if run.get("vm_id"):
        boxd = client()
        try:
            await boxd.machines.delete(run["vm_id"])
        except Exception:  # noqa: BLE001 - already gone is fine
            pass
        finally:
            await boxd.close()
    await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
    return True


_reconciler: asyncio.Task | None = None


async def _reconcile_loop() -> None:
    _log.info("reconciling the fleet every %ss", settings.reconcile_interval)
    while True:
        await asyncio.sleep(settings.reconcile_interval)
        try:
            found = await reconcile()
        except Exception:  # noqa: BLE001 - a boxd outage is a sweep that did nothing
            _log.exception("reconcile failed")
            continue
        if found["destroyed"] or found["stranded"] or found["stuck"]:
            _log.info(
                "reconciled: destroyed %s, stranded %s, stuck %s",
                found["destroyed"], found["stranded"], found["stuck"],
            )


def start_reconciler() -> None:
    """Run the sweep on a timer.

    `control/README.md` §4 has described "a periodic reconciler" since the layer was designed.
    Until now the function existed and nothing scheduled it: the only way a leaked VM was ever
    reclaimed was somebody noticing and pressing a button in the fleet view. Sleeps first, so a
    control plane in a restart loop cannot turn into a sweep loop.
    """
    global _reconciler
    if _reconciler is not None or settings.reconcile_interval <= 0:
        return
    _reconciler = asyncio.create_task(_reconcile_loop())


async def stop_reconciler() -> None:
    global _reconciler
    if _reconciler is None:
        return
    _reconciler.cancel()
    try:
        await _reconciler
    except asyncio.CancelledError:
        pass
    _reconciler = None


async def reconcile() -> dict:
    """Compare the boxd fleet against the runs table and resolve the difference.

    Fleet state belongs in the database, not in anybody's head. Without this, a crashed
    dispatch silently leaks machines against the quota.

    Safe to call at any time and from anywhere: it reads both sides fresh, and a run whose task
    is still in flight is never touched (`_tasks`). `headroom` calls it before giving up on a
    full fleet, and `_reconcile_loop` calls it on a timer.
    """
    boxd = client()
    try:
        machines = await boxd.machines.list()
        active = await db.active_runs()
        active_vms = {r["vm_name"] for r in active if r.get("vm_name")}

        orphans = [
            m for m in machines if is_run_vm(m.name) and m.name not in active_vms
        ]
        # Per machine, because one that refuses to die used to abort the whole sweep — and the
        # sweep is what reclaims the quota, so a single stuck VM could keep every other orphan
        # alive behind it. Reported rather than raised: the next sweep tries again.
        destroyed, stuck = [], []
        for machine in orphans:
            try:
                await boxd.machines.delete(machine.id)
                destroyed.append(machine.name)
            except Exception as exc:  # noqa: BLE001 - the message is the finding
                _log.warning("could not destroy orphan %s: %r", machine.name, exc)
                stuck.append(machine.name)

        # Runs we think are live whose VM no longer exists.
        names = {m.name for m in machines}
        stranded = []
        for run in active:
            if run.get("vm_name") and run["vm_name"] not in names and run["id"] not in _tasks:
                await db.update_run(
                    run["id"],
                    status="failed",
                    error="VM disappeared while run was active",
                    finished_at=db.utcnow(),
                )
                stranded.append(run["id"])
        return {"destroyed": destroyed, "stuck": stuck, "stranded": stranded}
    finally:
        await boxd.close()
