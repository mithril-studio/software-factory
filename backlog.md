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

**Not yet started:** items 2 (warm golden), 8 (budget ceiling), 9 (memory), 10 (telemetry),
and CodeRabbit under item 6.

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

### 2. Warm the golden

`factory-golden` is cold. Its repo is pinned at `e21a7b6 Initialize repository` while main is
~15 PRs ahead; `node_modules` is empty; there is no Playwright cache; no db container; and it
runs Node v24 against a repo that declares `engines: node 22.x` (hence the `EBADENGINE`
warnings in every run). Every fork re-pays all of it, and burns discovery turns working out
that it has to.

Bake into the golden:

- **Node 22 as the default** (`nvm install 22 && nvm alias default 22`) — promoted from a
  tidiness item to the most urgent one on this list. See below.
- `npm ci` — `node_modules` present in the snapshot (disk is 88G free, this is cheap)
- `docker compose up -d db_test` running, migrated and seeded (so `app_user` exists —
  it is created by migration `0000_create-app-role.sql`, not by docker)
- one `npm run build` so `.next/cache` is warm (65 builds across 10 runs at ~32s each)
- a correct, read-only `.env` (see item 3)
- the repo checked out at current `origin/main`

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

There is no cost cap today, only a 60-minute wall clock. The runner already streams
`total_cost_usd` from the result event — abort the run above a threshold (~$12) and above a
turn count. Failed runs currently record `cost_usd = NULL` and `error = "crashed: "`, so
real spend is invisible in our own ledger; fix that at the same time.

---

## later

### 9. Memory / compressed repo representation

Joost's item, and the biggest structural lever on cost. Cache reads are **81% of spend**
($94 of $146): 1,590 API calls each carrying 150k–263k tokens. The driver is rediscovery —
669 read/inspect bash calls across 10 runs, re-learning the same codebase every time.

The hook already exists (every run opens with `cat .mem/index.jsonl`, and the `memory` skill
is the only one installed on the golden). What's missing is density: a repo map — file tree,
module purposes, where things live, established conventions — so exploration collapses into
one read.

### 10. Telemetry layer

Spec'd in `telemetry/`, not built. Would close the subagent blind spot (item 5) and give
per-run cost attribution that doesn't depend on parsing transcripts after the fact.

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
