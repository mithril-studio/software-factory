# Deterministic verification

The factory selects required verifiers before the builder starts. Models report evidence;
`control/verification.py` decides whether the factory verifies, retries, fixes, escalates,
or becomes eligible to merge. No model can waive a required check.

## Enable on a pilot

Add one `## Verification` section to the repo's **base branch** `.factory.md`:

```yaml
version: 1
max_retries: 1
```

Put that YAML in a fenced YAML block immediately below the heading. This enables mandatory
code review, even when the legacy `FACTORY_REVIEW_ENABLED` switch is off. An absent section
keeps legacy behavior. A present but malformed section fails the build dispatch; unknown
criteria modes, duplicate IDs, or missing acceptance criteria also refuse dispatch.
Reading the base profile now fails on GitHub errors other than 404: an unavailable contract
must not be mistaken for an absent one. A manually started review loads the contract too.

The issue must contain the existing fenced YAML `## Acceptance criteria` block. The profile,
issue, criteria, retry limit and fix-cycle limit are copied into `runs.verification_plan`
before the builder starts and hashed into a plan version. Fixes and crash retries inherit
that exact snapshot. Changes to an issue or profile require a new manually started run to
establish a new contract; they cannot weaken one already in progress.

`test`, `probe`, and `structure` criteria are required. `inspect` criteria are advisory.
A separate required review approval preserves the reviewer's ability to report a blocking
finding outside the explicit criteria. A refusal without evidence does not count as a
confirmed product failure.

## Add browser verification

See [the foundation-e-learning pilot](examples/foundation-verification.md). Its `e2e` object
names setup, reset, start, cleanup, a local readiness URL, and 1–5 flows. Each flow references
the exact issue criterion IDs it exercises. Task text and expected behavior come from the
frozen plan, never the PR's copy. Configure flows for the issue being piloted; this first
version does not infer which journeys apply to arbitrary issues.

Start with `required: false`. The browser still runs and its results and recordings are
retained, but its findings and infrastructure errors cannot block. Promote to `true` only
after testing clean builds and planted defects repeatedly. This is an additional verifier;
it never replaces code review or CI.

Before enabling E2E, install a reviewed copy of `e2e-agent-harness` in the golden VM using
`scripts/install-e2e-harness.sh SOURCE_DIR`. The source directory is the standalone harness
project, copied into the VM beforehand. The installer does not fetch an unpinned remote.
It installs the locked npm dependencies and Chromium at `/opt/factory/e2e-agent-harness`,
records source hashes in `factory-version.json`, and refuses to overwrite an existing copy.
The golden must also have Node 22+, Python 3, a working Claude CLI login, and the app's test
database/runtime dependencies. Capture the snapshot after installation. None of this is
installed on the control-plane server.

Every verification attempt gets a fresh review VM and checks out the exact PR SHA. Review
runs first; the browser worker then rejects a changed checkout, runs setup and reset before
each flow, starts the app, waits for readiness, invokes the harness, and cleans up. Commands
run in separate shells: include any environment setup in each command that needs it. Use
only a disposable local test database. The worker refuses an already-occupied app port.

Limits: setup/reset 10 minutes each, readiness 90 seconds, browser driver 5 minutes and $1,
whole harness invocation 10 minutes, cleanup 60 seconds. The E2E stage is capped at the
smaller of 30 minutes and `FACTORY_RUN_TIMEOUT`. The harness's judge is time-bounded by that
invocation; there is currently no whole-stage dollar cap. These limits are fixed in this
first version. Review itself retains the factory's existing run timeout.

## Decision rules

- Missing or stale required results: run verification. If an attempt finishes without
  producing them, retry within the frozen retry budget, then escalate.
- Required `fail` with evidence: request a fix until the frozen cycle limit, then escalate.
- Required `error` or `inconclusive`: fresh verification attempt, then escalate at the limit.
- Duplicate answers, malformed results, or pass/fail disagreement across attempts on the
  same commit: escalate. A later pass never silently erases a failure.
- All required checks pass: eligible to merge. Existing CI and auto-merge settings still apply.

Results are matched by check ID, verifier, plan version, commit, and attempt. Stale evidence
is not reused. E2E pass/fail needs referenced screenshots that were actually collected.
The normalized envelope retains the native verdicts and recordings separately.

The merge request is pinned to the verified SHA even if CI checking is disabled. A new push
or merge conflict stops for fresh verification; this path does not silently reconcile and
merge a commit that was never reviewed. CI failures can use the existing bounded fix loop.

## Evidence, recovery, and access

`runs.verification` stores normalized results, the latest decision with reasons, and artifact
locations. The decision is committed before scheduling a fix or attempting a merge. Fix
dispatch uses a deterministic child ID and an atomic insert: replay cannot create a second
fix. Referenced screenshots and reports are copied into `/tmp/factory-evidence/` in the fix VM.

Recordings live under `FACTORY_LOG_DIR/<run-id>-verification/<attempt>/` and are collected
before the VM is reaped. `receipt.json` describes the outcome. Archive collection is limited
to 64 MB per attempt and rejects traversal and non-regular files; failure to collect required
evidence cannot produce a pass. Fix evidence transfer is capped at 16 MB.

Use the authenticated API:

```
GET /api/runs/<run-id>
GET /api/runs/<run-id>/verification/receipt.json
GET /api/runs/<run-id>/verification/1/lesson/steps/01.jpg
```

Artifacts download as attachments, so captured application HTML cannot execute on the factory
origin. Retention follows the log directory; no automatic recording expiry is introduced.

After a controller restart, an enabled nonterminal run with no live task is marked failed
with a durable `controller_interrupted` escalation. The reconciler reclaims its VM through
the existing orphan sweep. It does **not** guess whether an interrupted merge or dispatch
finished, or replay uncertain external actions. Inspect the PR and saved receipt before
starting another run. This deliberately favors an explicit interruption over duplicate work.

## Validation and rollout boundary

`scripts/verify.sh` includes policy, persistence, and integration tests. The integration test
supplies a simulated browser finding (0 points instead of 10), exercises real decision and
dispatch storage, proves duplicate dispatch is suppressed, then supplies a fresh passing
commit. It also covers retries, stale heads, restart escalation and evidence transfer.

These tests do not claim model reliability or run a live browser/VM. Deploying the factory,
installing the harness in a golden, enabling the pilot, and observing real planted defects
are rollout steps. No repo is opted in by this change. Design results can use the same
envelope later; there is no design executor in this slice.
