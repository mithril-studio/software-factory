# Deterministic post-build verification

The factory still has one primitive: issue → VM → agent → PR into `test`. Humans merge.
An opt-in browser check now runs after the builder exits and before that same VM is reaped.
There is no separate review-agent pipeline, automatic merge, automatic fix dispatch, or
retry state machine. Re-add `agent:queued` to continue a failed PR, as before.

## Contract

Put a `## Verification` heading and one fenced YAML block in the target branch's `.factory.md`.
The block has `version: 1` and an `e2e` object. See [the pilot example](examples/foundation-verification.md).
No section means existing behavior. A present malformed section fails closed. A GitHub error
reading the profile now fails dispatch; only a 404 means absent.

For opted-in runs, the issue must also contain a fenced YAML `## Acceptance criteria` block
with unique `id`, `mode`, and `statement` fields. This is optional for ordinary factory runs;
the existing checkbox issue template continues to work when verification is not enabled.
A flow references the exact criterion IDs it exercises. This first version does not infer
journeys for arbitrary issues. Missing IDs or unknown modes are errors, not skipped checks.

The target-branch profile and issue criteria are frozen into `runs.verification_plan` before
implementation. The factory creates a clean detached worktree at the PR's exact SHA inside
the run VM, executes the installed harness, collects evidence, and records the decision.
The builder's uncommitted files are not tested. If the PR head moves during verification,
the result is stale and the run fails. This is isolation from workspace state, not a security
boundary against malicious code running as the VM user.

## Decisions

- Required checks pass → `ready_for_review`; the run can succeed. This never merges a PR.
- Required check fails with evidence → `request_fixes`; the run fails with that explanation.
- Missing evidence, malformed/duplicate answers, worker errors, or a stale commit → `escalate`.
- Missing results before execution → `run_verification`.
- Advisory (`required: false`) results are recorded but cannot fail the build.

There are no automatic verification retries. A new run from a re-queued issue freezes a new
contract and executes again. The factory's existing atomic claim prevents concurrent runs;
its existing restart recovery re-queues interrupted work. Prior recordings are never reused
as proof for a new run or commit. Merge and re-queue decisions remain human-owned.

## Install and operate

The harness lives in [mithril-studio/e2e-agent-harness](https://github.com/mithril-studio/e2e-agent-harness).
Inside a golden VM, clone that repository, check out an immutable release/tag SHA, and run
its `scripts/install-golden.sh SOURCE_DIR`. The installation belongs outside app checkouts at
`/opt/factory/e2e-agent-harness`. The script installs locked dependencies and Chromium, and
records source hashes; the worker checks those hashes before invoking it. There is one
installer, in the harness repo. The golden also needs Node 22+, Python 3, Claude CLI auth,
and the application's disposable database/runtime prerequisites.

The flow's explicit setup/reset/start/cleanup commands run in separate shells. Include any
environment initialization in each command needing it. Reset before every flow. Only local
HTTP readiness addresses are accepted, and an occupied app port is refused. Start advisory
and measure clean and planted-bug runs before enabling required checks.

Setup and reset have 10 minutes each, readiness 90 seconds, the driver 5 minutes and $1,
the whole harness invocation 10 minutes, and cleanup 60 seconds. The stage is capped at
30 minutes or the factory run timeout, whichever is smaller. There is no whole-stage dollar
cap yet. Recordings include native driver/judge model and cost fields; the factory's existing
builder cost column does not yet aggregate the browser model spend.

## Evidence and debugging

`runs.verification` holds the normalized results and decision. The existing closing report
includes the decision and tells the operator to re-queue on failure. Artifacts are saved at
`FACTORY_LOG_DIR/<run-id>-verification/1/` before VM cleanup, including partial recordings
when transport allows. They inherit log retention. Archives are capped at 64 MB and reject
traversal and non-regular files. Pass/fail needs cited screenshots that were collected.

Authenticated downloads:

```
GET /api/runs/<run-id>
GET /api/runs/<run-id>/verification/receipt.json
GET /api/runs/<run-id>/verification/1/lesson/steps/01.jpg
```

Files download as attachments, preventing captured app HTML from executing on the factory
origin. No new dashboard is added.

`scripts/verify.sh` exercises policy, storage, clean-worktree invocation, stale-head rejection,
advisory failure, and artifact authorization. Real browser pilot results are recorded in the
harness repo. Installing a golden and merging/deploying this factory change are separate
rollout steps; this PR does not enable any application's configuration automatically.
