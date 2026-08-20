# Backlog

Working backlog for the Software Factory. Ordered by expected return, not by effort.
Everything here came out of the run analysis on 2026-08-12 (last 10 runs of
`mithril-studio/foundation-e-learning`: $146.78, 6h31m, 3 of 10 failed).

Status key: **now** = next thing to build · **next** = queued · **later** = agreed direction,
not yet scheduled. ✅ marks what has shipped.

**Shipped 2026-08-12:** items 1, 3 (the file), 4, 5, 6 (the gate), 7 and 11 — the prompt
rewrite, the golden's read-only `.env`, command-timeout env vars, `--disallowed-tools`,
resume-on-retry, `checks_green` gating auto-merge, and Sentry removal (PR #44). Also raised
`FACTORY_RUN_TIMEOUT` to 90 minutes and made a timeout say so instead of recording
`crashed: ` with an empty message.

**Also shipped:** item 0 (CI repaired + change guards), item 2 (golden warm, on Node 22, with a
toolchain assertion in `VM_SCRIPT`), and executable acceptance criteria in `factory-compose`.

**Not yet started:** 8 (budget ceiling), 8b (dependency safety), 9 (memory), 12 (credential
rotation), CodeRabbit under 6 — and the three pipeline pieces below.

**Shipped 2026-08-17:** item 10, the telemetry layer — per-call rows, derived cost, and the
ledger fix that item 8 depends on.

**The pipeline, agreed 2026-08-13** (see `discussion.md` for the design):
CI floor ✅ → executable acceptance criteria ✅ → smoke suite → reviewer agent → preview.
CodeRabbit was removed from the account: rate-limited on this plan, so it reviewed some PRs
and not others while reporting green either way.

---

## now

### 0. Repair CI and add change guards — ✅ [FE #45](https://github.com/mithril-studio/foundation-e-learning/pull/45)

Three unrelated breakages, all pre-existing: the lock file (see item 2's Node note),
`secret-scan` 403ing because gitleaks-action's PR mode needs `pull-requests: read`, and
`dependency-review` requiring GitHub Advanced Security the repo does not have (removed —
`npm run audit` already covers the lock file more broadly).

Added a `guards` job — five deterministic checks on the *shape* of a change, each with an
explicit escape hatch, each tested against its own negative case before shipping:

| Guard | Escape hatch |
|---|---|
| No silently deleted tests | `Removes-tests: <reason>` in the PR body |
| No unapproved dependencies | `Adds-dependency: <name> — <why>` |
| No schema/migration drift | none — should never fail legitimately |
| Suppressions carry a `-- <why>` | the justification itself |
| Coverage floor (33% statements) | raise it deliberately in its own PR |

The secret scan is scoped to the commits a change introduces rather than full history: a
full-history scan reports 8 findings, all fixture passwords in `tests/integration`, so as a
blocking gate it would have been red on arrival for something no author can fix.

**`main`'s CI is green for the first time since issue #19.**

### 1. Stop the background-wait deadlock

Three of the last ten runs died the same way: the agent backgrounds a long Playwright suite,
then waits for a task notification that never arrives under `claude -p`. It exits 0 having
never committed. Cost: ~$22 recorded (~$35 real — the crashed run logs no cost at all) and
~2 hours per 10 runs.

✅ **Done**, all three layers:

- `BASH_DEFAULT_TIMEOUT_MS=600000` / `BASH_MAX_TIMEOUT_MS=1800000` in the dispatch env
  (`FACTORY_BASH_TIMEOUT` / `FACTORY_BASH_MAX_TIMEOUT`), so a long command is never
  auto-backgrounded at 120s. Removes the trigger. Ten minutes is ~4× the longest legitimate
  command observed (a 153s cold build) now that e2e is out of scope, so nothing real gets
  backgrounded, and a genuinely stuck command is still bounded by `FACTORY_RUN_TIMEOUT`.
- `--disallowed-tools Agent Task ScheduleWakeup` on the `claude -p` line. Removes the tool
  that produced the hang (and the nested agents from item 5).
- The invariant is in `PROMPT_TEMPLATE`'s environment notes: run long commands in the
  foreground with an explicit `timeout`; background tasks and wake-ups do not deliver here.

The mechanical layers matter more than the written one. `.mem/domains/_universal.jsonl`
(`mem_o201`) records that the agent already had a memory telling it to commit after every
milestone, and still didn't — a rule the agent has read is not the same as a constraint it
cannot violate.

Not a skill — see `discussion.md` §"Where rules live".

### 2. Warm the golden — ✅ done 2026-08-13

`factory-golden` was cold: repo pinned at `e21a7b6 Initialize repository` while main was ~15
PRs ahead, no `node_modules`, no db container, and Node v24 against a repo pinning 22. Every
fork re-paid all of it, and burned discovery turns working out that it had to.

**A fork inherits running processes, not just disk.** That was the open question, and it is
worth writing down because it decides how much can be baked in. Verified by forking the warm
golden and inspecting the fork before touching anything:

```
commit:       37e58c1 (current main)     node:   v22.23.2
node_modules: 617 packages               .next:  232M, cache warm
docker:       repo-db_test-1  Up (healthy)
psql:         schema present, 2 tenants / 6 users seeded
npm run test:integration → 19 files, 126 tests passed in 34s, zero setup
```

The database keeps its data in tmpfs (RAM), so surviving the fork was not a given — a
cold-booting fork would have come up with an empty database and made the prompt's "already
migrated" line a lie. It survives, so the prompt can state it.

What was baked in:

- ✅ **Node 22 as the default** — done 2026-08-13. Note `nvm alias default` is *not*
  sufficient on its own: agent runs execute under `/bin/sh`, which never loads nvm. What
  actually resolves `node` is a set of root-owned symlinks in `/usr/local/bin`
  (`node`, `npm`, `npx`, `corepack`) that pointed at v24. Both had to be repointed.
- ✅ `npm ci` — 617 packages present, verified usable by running `npm run typecheck`
- ✅ `db_test` running, migrated and seeded (`app_user` comes from migration
  `0000_create-app-role.sql`, not from docker, so migrating is what creates the role)
- ✅ one `npm run build` — `.next/cache` warm (65 builds across 10 runs at ~32s each)
- ✅ a correct, read-only `.env` (item 3)
- ✅ the repo checked out at current `origin/main`
- ✅ a version assertion in `VM_SCRIPT`, so a golden that drifts off `.nvmrc` fails
  immediately with a clear message instead of producing a broken lock file

**This is machine state, not repo state.** It does not need re-checking per fork — forks
inherit it — but all of it must be redone if the golden is ever rebuilt from a base image,
and it goes stale as `main` advances. A re-sync-after-merge job is the remaining gap
(`git pull && npm ci && npm run build && npm run db:migrate`, then re-snapshot); without it
this decays back to where it started, just more slowly.

**Deliberately not installed:** Playwright browsers. E2E belongs to a future tester agent and
to CI, not to the main factory run — see item 7.

#### The Node version is a correctness bug, not a speed one

The golden runs **Node 24 / npm 11**. The target repo's `.nvmrc` says **22**, and CI installs
from that file, so CI runs **npm 10**. The two npm majors resolve optional peer dependencies
differently: npm 11 omits the nested `@swc/helpers` entry that npm 10 requires.

So every agent that touched a dependency wrote a lock file that `npm ci` could not install,
and CI died at its first step — on every PR and on main — from issue #19 onward. Fifteen PRs
merged with no verification but the agent's own word for it. Nobody noticed because
auto-merge did not wait for CI (item 6), so the red never blocked anything.

Confirmed the hard way while fixing it: the corrected lock file was regenerated under Node
22, then silently re-broken by a single local `npm i` under npm 11 before it was pushed.

This makes matching the golden's toolchain to `.nvmrc` a prerequisite for the whole
pipeline. Every gate downstream — guards, reviewer agent, preview — assumes `npm ci` works.
A tempting stopgap is to pin `engine-strict` or check the lock file in CI, but the real fix
is that the machine agents work on should run what CI runs.

**Fixed 2026-08-13**, verified in the environment agents actually get (a fresh
non-interactive `boxd machine exec`, not a login shell):

```
node: v22.23.2    npm: 10.9.8      # matches .nvmrc and CI exactly
npm ci                             → added 898 packages
npm install --package-lock-only    → lock file unchanged, no drift
```

That second test is the one that matters: an agent running a plain `npm install` no longer
rewrites the lock file out from under CI, which is the mechanism that broke `main` fourteen
times in a row. This does not need re-checking per run — but it does need re-checking
whenever the golden is rebuilt, since it is a property of the machine, not of the repo.

Expected: ~10 min/run of tooling, plus a meaningful cut in exploration turns.

**Operational cost this creates:** a warm golden goes stale. Needs a re-sync job after every
merge to main (`git pull && npm ci && npm run build`, re-snapshot). Without it this item
decays back to where we are today.

### 3. `.env` from the control plane, not from the repo — ✅ the file, rest open

Joost's call, and correct. Note the actual situation: there is no secret `.env` today —
`.env.example` is three non-secret keys pointing at the local docker postgres, on the wrong
port. What the agent does (`cp .env.example .env && sed -i 's/5432/5433/'`, 7 times in 10
runs) is one-off setup work it should never have to do.

✅ **Done** — this file is now on `factory-golden` at `/home/boxd/repo/.env`, root-owned and
`chmod 444`. One honest limitation: the agent runs with sudo in the VM, so read-only is a
strong default rather than a wall. The prompt line ("`.env` is correct and read-only — do not
create, copy or modify it, and do not print its contents") carries the rest.

Still open: injecting anything genuinely secret as process env through the runner's exec env
dict, rather than through a file at all.

The file, for reference:

```sh
DATABASE_URL=postgres://app_user:app_user_local_dev_password@localhost:5433/foundation_test
DATABASE_MIGRATION_URL=postgres://app_migrator:app_migrator_password@localhost:5433/foundation_test
APP_URL=http://localhost:3000
NODE_ENV=development
LOG_LEVEL=info
```

Three notes on why these values and not `.env.example`'s:

- **Port 5433, not 5432.** `.env.example` points at the `db` service; the integration suite
  and `vitest.integration.config.ts` use `db_test` on 5433. That mismatch is precisely what
  every agent kept repairing by hand with `sed -i 's/5432/5433/'`.
- **`LOG_LEVEL=info` is a token optimisation, not a preference.** `src/lib/logger.ts`
  defaults to `debug` in development, and the transcripts are full of tool results that are
  nothing but dumped SQL debug lines — one of them a 35s read. Every one of those is context
  we pay cache-read on for the rest of the run.
- **No Sentry or email variables.** All optional in `src/env.ts`, and Sentry is being
  removed (item 11).

Then:
- Inject anything real as **process env** through the exec env dict the runner already
  passes to `stream_exec`. Process env beats `.env` for secrets: never on disk, never in the
  salvaged transcript, never committable.
- Prompt line: the environment is already configured; do not create or modify `.env`.

The risk worth naming: the danger was never deletion. It is that the day a real secret lands
in `.env`, a routine `cat .env` puts it into the transcript we salvage to disk and render in
the UI.

### 4. Retry that resumes instead of restarting

`VM_SCRIPT` runs `git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BASE"` on **every**
attempt — so attempt 2 discards everything attempt 1 pushed and starts over from main, at
full price. `RETRY_TEMPLATE` then tells the agent to `--force-with-lease` over its own
earlier work. Issue #18 cost 3 attempts, 135 min, $28.55+ largely because of this.

Joost's design (incremental commits, resume from the last one):

- ✅ Prompt: commit and push after each meaningful step; the branch is what survives, the VM
  is destroyed on exit.
- ✅ `VM_SCRIPT` resumes `origin/factory/issue-N` when it exists **and this is a retry**
  (`FACTORY_ATTEMPT > 1`), falling back to `origin/{base}` otherwise. Scoping it to retries
  means re-queueing an issue whose branch still exists from an earlier merged PR gets a clean
  start rather than a resurrection. Verified against a fixture repo: attempt 1 starts from
  base, attempt 2 resumes the pushed commit.
- ✅ `RETRY_TEMPLATE` now tells the agent it is already on the branch with the previous
  attempt's commits and to read them first — the `--force-with-lease` paragraph is gone,
  since there is no longer anything to force over.
- Classify the failure and only auto-retry the mechanical ones: harness hang / crash /
  timeout / infra. "Agent gave up" goes to a human.
- Feed forward a short diagnosis, not the 4k-char log tail (the tail is itself context cost).
- Cap retries by **spend**, not attempt count.

### 5. Forbid nested agent calls — ✅

Run 937b51a7 spent 37 of its 42 minutes inside a single `Agent` call and cost $24.40 — the
most expensive run of the ten — while its own transcript accounts for $0.92. The work is
invisible to us.

✅ `--disallowed-tools Agent Task ScheduleWakeup` on the `claude -p` invocation.

Revisit once the telemetry layer (item 10) can see inside subagents. Read-only exploration
subagents genuinely save context; it's the *implementation* subagent that hides cost. Tool
flags can't tell them apart, so both are banned for now.

---

## next

### 6. PR check before auto-merge — ✅ the gate, CodeRabbit still queued

✅ **Done:** `github.checks_green()` polls the check-runs API until every check on the PR's
head sha completes, and `runner.py` only merges when they all pass. Controlled by
`FACTORY_MERGE_REQUIRE_CHECKS` (default on) and `FACTORY_MERGE_CHECK_TIMEOUT` (default 900s).
A timeout, an API error, or a commit with no checks at all leaves the PR open rather than
merging — an unverified commit is never treated as a green one.

Still queued: CodeRabbit itself. It posts its verdict as a check run, so it needs no new
plumbing — installing it on the repo is enough for the gate above to start waiting on it.

The evidence that motivated this:

PR #43 was created at `02:25:27` and merged at `02:25:37`. Ten seconds. All three CI checks
then failed. Fifteen PRs are on main with `reviewDecision: -`, including +9,072 lines of
AVG/GDPR erasure code. Because every run branches from main, a red main propagates into
every subsequent run.

The mechanism is the same one CodeRabbit will need: `merge_pr` should poll the checks API and
decline to merge when checks aren't green. ~20 lines, and it's a prerequisite for any
review gate on top.

### 7. Take Playwright out of the factory's scope — ✅

**Decided and shipped:** e2e is a tester agent's job, not the main factory's. Playwright must
not be installed or run inside a factory run.

Playwright entered this repo through the factory itself (issue #18 added `@playwright/test`
+ `@axe-core/playwright`, the `test:e2e` script, and the CI step). Nothing about it is Claude
Code native. The prompt's "run whatever tests the repo already has" then made it permanent:
78 invocations, 37 minutes across 10 runs — the single most expensive instruction in the
prompt.

- ✅ Prompt step 4 now names the checks the agent runs — `npm run lint`, `npm run typecheck`,
  `npm run test`, `npm run test:integration`, `npm run build` — and forbids `test:e2e` and
  installing browsers.
- ✅ Golden does not warm `~/.cache/ms-playwright` (item 2).
- ✅ The repo's e2e suite and its CI step are left alone — CI still runs them on every PR,
  which is where that coverage comes from until the tester agent exists.

### 8. Per-run budget ceiling

There is no cost cap today, only a wall clock. Abort the run above a threshold (~$12) and
above a turn count.

The ledger half of this is ✅ done by item 10: failed runs no longer record `cost_usd = NULL`,
because rows are flushed per turn and `_salvage_usage()` reads them back. That also supplies
the mechanism the ceiling needs — `store.usage_for_run()` returns a live derived cost mid-run,
so the check is a threshold on a number that already exists rather than new plumbing.

---

### 8b. Dependency safety in the target repos

The factory installs dependencies on its own initiative — `pino`, `@sentry/nextjs`, `pdf-lib`,
`audit-ci` and `@playwright/test` all arrived that way — so supply-chain controls matter more
here than in a repo where a human types every `npm install`.

`foundation-e-learning` already has more of this than most repos, and none of it should be
disturbed: Dependabot with grouped production/dev PRs, a committed lock file with `npm ci`,
`audit-ci` in CI, gitleaks, `engines` + `.nvmrc`, and — unusually — **GitHub Actions pinned by
commit SHA rather than tag**, which is the control that stops a compromised action from
executing in the pipeline.

**Do first, it is broken right now:** Dependabot PRs #41 and #42 still fail in seven seconds.
They branched off the old broken `main`, so they carry the pre-fix lock file. `@dependabot
rebase` on each, or wait for its next run.

Then, in order of value:

1. **A cooldown before adopting new versions.** The single strongest control available:
   compromised packages are typically caught within days of publication, so simply not being
   first to install anything skips nearly every such event. Dependabot supports a cooldown;
   Renovate calls it `minimumReleaseAge`. 3–7 days.
2. **`npm audit signatures` in CI** — verifies packages came from who claims to have published
   them. One line in the `gates` job.
3. **`ignore-scripts=true` in `.npmrc`** — install scripts are the main execution vector for a
   malicious package. Third rather than first because some packages genuinely need them, so it
   takes tuning.
4. **An automerge policy for dependency PRs** — dev-dependency patches can merge themselves on
   green CI; production dependencies and majors get a human. Same gating question as item 6,
   and now answerable, since CI means something again.
5. **SBOM generation** (`npm sbom`) — matters more here than in most projects given the AVG
   posture already documented in `docs/security.md`.

Not recommended: switching Dependabot for Renovate. Renovate is more configurable, but
Dependabot is working and configured, and the gap does not pay for the migration.

### 8c. Provision the golden from a script, not by hand — ✅ `scripts/build-golden.sh`

Everything in item 2 was done by hand over `boxd machine exec`. That makes the golden a pet:
nobody can reproduce it, and a rebuild loses all of it silently. It should be a
`scripts/build-golden.sh` that goes from a bare `boxd new` to a warm golden in one run —
reviewable, repeatable, and the same script the re-sync-after-merge job can call.

This is the difference between "the golden is warm" being a fact about a machine somebody
once configured, and a property the system maintains.

### 12. Rotate the control-plane credentials, and make rotating them a documented step

Filed as [#61](https://github.com/mithril-studio/software-factory/issues/61). Deferred by
Joost on 2026-08-19 and again on 2026-08-20 — it is real, it is not urgent, and it is written
down here so it stops being re-discovered every session.

Three parts, in order:

1. **Rotate `FACTORY_AUTH_PASSWORD` and `FACTORY_SECRET_KEY` together.** The session cookie
   key does not derive from the password once it is set explicitly, so changing the password
   alone does not invalidate sessions already issued.
2. **Rate-limit `POST /api/login`.** There is none today, on a publicly reachable control
   plane that holds `CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN` and `BOXD_API_KEY`.
3. **Write the rotation down** next to the deploy steps, so it is a procedure rather than an
   archaeology exercise.

Related and already agreed: Infisical for secrets, on Joost's own backlog. Until that lands,
credentials live in `.env` on the Hetzner box and in `boxd env` for the machines, and
mirroring between the two is by hand.

## later

### 9. Memory / compressed repo representation

Joost's item, and the biggest structural lever on cost. Cache reads are **81% of spend**
($94 of $146): 1,590 API calls each carrying 150k–263k tokens. The driver is rediscovery —
669 read/inspect bash calls across 10 runs, re-learning the same codebase every time.

The hook already exists (every run opens with `cat .mem/index.jsonl`, and the `memory` skill
is the only one installed on the golden). What's missing is density: a repo map — file tree,
module purposes, where things live, established conventions — so exploration collapses into
one read.

### 10. Telemetry layer — ✅ built 2026-08-17

One row per model call and per tool call, flushed per turn, priced by a `model_prices` table
we control, joined to `runs` for outcomes. Built in process against the event stream the
runner already parses rather than over OTLP — see `telemetry/README.md` §2 for why, and §7
for what shipped.

**Validated against all 47 production runs** (37 transcripts replayed, 2026-08-17). Derived
cost reproduces the runtime's own figure to the cent on 15 of 21 comparable runs; token
counts match exactly on every one. Real numbers for the whole history:

| metric | value |
|---|---|
| total spend | $251.63 across 47 runs, 22 issues |
| cost composition | cache read 73.5%, cache write 13.8%, output 12.6%, input 0.0% |
| cost per shipped issue | $11.44 |
| spend on runs that shipped nothing | $22.98 (9.1%) |
| tool wall time | Bash 210 min / 2,222 calls · nested agents 49 min / 6 calls |
| ledger gap this closes | 13 of 47 runs recorded no cost at all |

What it changes, concretely:

- **Cache reads are now a column.** 73.5% of all spend, and the old schema had nothing that
  could show it. Cache writes are split 5m/1h because they price differently.
- **Failed runs record their spend** (§5.3), which closes the half of item 8 that made real
  spend invisible in our own ledger. The other half — actually aborting above a threshold —
  now has a truthful number to enforce against.
- **Cost per shipped issue** and **spend on runs that shipped nothing** are computed rather
  than estimated by hand.
- **The 2026-08-12 analysis is repeatable.** It was excellent and manual; that was the tell.

Still open: the subagent blind spot (item 5) is *not* closed — nested agents are disabled,
the `parent_call_id` column is in place but empty, and seeing inside them is what OTLP is
for. Run `python -m telemetry.backfill` on the `software-factory` VM to load history from
existing transcripts.

### 11. Remove Sentry — ✅ PR #44 open

**Decided:** out. Joost is building his own telemetry layer for log insight, so a third-party
error tracker is not wanted.

✅ Done by hand rather than through the factory (deterministic and free, vs ~$15 and a retry
risk for a mechanical deletion):
[PR #44](https://github.com/mithril-studio/foundation-e-learning/pull/44). Typecheck, lint,
format, 190 unit tests and a production build pass locally. Awaiting review — nothing merges
it automatically, since the poller only acts on issues labelled `agent:queued`.

Added by run 6f2eb679 for issue #20, and currently inert by design — `Sentry.init()` only
fires when `SENTRY_DSN` is set, and no DSN exists anywhere. So today we pay the build-time
cost of `@sentry/nextjs` (visible as a 153s build in the transcripts) for zero observability.

What went: `@sentry/nextjs`, `sentry.server.config.ts`, `src/instrumentation-client.ts`,
`src/lib/sentry-scrub.ts` and its tests, the `withSentryConfig` wrapper in `next.config.ts`,
`onRequestError` in `src/instrumentation.ts`, the Sentry block in `src/env.ts` and
`.env.example`, and the conditional Sentry origin in the CSP (`connect-src` is now
`'self'`-only, with no exception left to reason about). `pino` and the structured logging
from issue #20 stay — that is what the telemetry layer will consume.

`docs/security.md`'s third-party-processors section is now empty by design, which is the
accurate AVG position, and both it and the repo's `CLAUDE.md` now say that adding a processor
is an explicit decision rather than a dependency added in passing.

Related: the agent installs dependencies unilaterally — `pino`, `@sentry/nextjs`, `pdf-lib`,
`audit-ci`, `@playwright/test` — and they reach main unreviewed. Worth a policy once the PR
gate exists.
