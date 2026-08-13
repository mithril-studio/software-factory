# Discussion

Durable context from working sessions — decisions, stances, and direction that aren't
derivable from the code or git history. Read this before proposing changes, so you argue
against what's actually intended rather than against what the repo looks like.

Newest first.

---

## 2026-08-12 — Run analysis session

Analysed the last 10 runs on the `software-factory` boxd VM (target repo:
`mithril-studio/foundation-e-learning`). Findings live in `backlog.md`; what follows is the
thinking around them.

### Auto-merge is deliberate, not an oversight

Joost: *"I'm still finding out how I want to work with auto merges. For now, I'm testing how
and what auto merges work."*

`FACTORY_AUTO_MERGE=1` merges every successful PR into main within seconds, unreviewed,
before CI can report. This is an experiment in progress, not a bug to be fixed behind his
back. Do not turn it off unilaterally, and do not re-litigate it every session.

What he *has* committed to: a PR check (CodeRabbit or similar) as a gate, on the backlog.
The useful contribution is building the mechanism that gate needs — `merge_pr` polling the
checks API — rather than arguing about the policy.

The cost of the current setting, so it's on the record: main is currently red, and because
every run branches from main, a red main propagates into every subsequent run.

### Secrets belong in the control plane, not in the repo

Joost: *"I was thinking of removing the .env from the .env and just injecting them from the
control pane."*

That's the direction. Process env through the runner's exec env dict, not files on disk in
the VM. The reasoning that makes it more than a tidiness fix: transcripts are salvaged to
disk and rendered in the UI, so anything the agent can `cat` is anything we've published to
ourselves.

### Retries should resume, not restart

Joost: *"commiting incremental and when a run fails during merge, the next agent can continue
with latest smaller commit, this way context isn't fully lost."*

Right instinct, and it lands on a real defect: `VM_SCRIPT` re-checkouts from `origin/main` on
every attempt, so a retry actively discards the previous attempt's pushed commits. The branch
should be the durable artifact; the VM is disposable. Design in `backlog.md` §4.

### Where rules live: prompt/flags vs skills

Asked directly, and worth keeping as a general principle:

- **Harness invariants** — things that, if ignored, hang or break the run — go in the
  dispatch env, CLI flags, or the system prompt. They must be in context from turn 0 and
  ideally enforced mechanically. The background-wait rule and the no-nested-agents rule are
  both of this kind.
- **Repo know-how** — conventions, where things live, what was learned — goes in skills and
  `.mem/`. Loaded on demand, by an agent that has decided it needs them.

A skill cannot prevent a hang, because the agent only reads it if it thinks to.

### Memory is understood as the big cost lever

Joost: *"A memory layer could add a lot of tokens saving... providing the agent with a
compressed version of what's in the repos would save a lot of tokens. Will get there."*

Correct, and the numbers back it: cache reads are 81% of spend. Explicitly parked for now —
don't push it ahead of the mechanical fixes, which are cheaper and land sooner.

### Scope decisions: Playwright out, Sentry out

- **Playwright belongs to a future tester agent, not the main factory.** It must not be
  installed or run inside a factory run, and the golden must not warm the browser cache. The
  repo's e2e suite and its CI step stay — CI is where that coverage comes from for now.
- **Sentry gets removed.** Joost is building his own telemetry layer to give insight into the
  logs, so a third-party error tracker is redundant. `pino` and the structured logging stay;
  that's what the telemetry layer will consume.

The general shape here: the factory's job is to make a change and prove it builds and
passes the repo's own fast checks. Verification breadth (e2e, browsers) and observability
are separate concerns, owned by separate agents or by CI — not bolted onto every run.

### A written rule is not a constraint

`.mem/domains/_universal.jsonl` (`mem_o201`) records issue #20's first attempt losing ~150
turns of finished work, and notes that the agent *already had* a memory telling it to commit
after every milestone — and still didn't. Independent confirmation of the background-wait
diagnosis, and the reason the mechanical half of each fix (env vars, tool flags, the
checkout) matters more than the prompt half. Prompt for things you'd like; use flags and
harness behaviour for things that cost money when ignored.

### The review pipeline, and why criteria are executable

Joost, on not reviewing diffs by hand: *"Automated development is the whole premise of the
software-factory."* Agreed shape — layers, cheapest first, and every failure feeds a fix run
rather than a human inbox:

CI floor → diff-shape guards → reviewer agent → preview + smoke → human, rarely.

The design decision worth preserving, because it is what keeps the reviewer honest: **the
agent never chooses how hard to look.** Each acceptance criterion declares a `mode`, and the
reviewer executes that mode. Given the choice it would always pick the cheapest — read the
diff and reason about it — and that is exactly where hallucination lives. Reading code and
asserting "this returns 429" is a confident guess; running it and observing a 429 is a fact.

Joost: *"it should be insanely deterministic to prevent hallucinations."* Hence: evidence is a
`file:line`, a test name, or a command and its output. No evidence means `cannot_verify`, and
`cannot_verify` counts as not met. `inspect` — the mode that needs judgment — can never block
on its own.

And the cheap comparison that catches the failure nobody else can: **a new test must fail
against the old code.** The reviewer checks out base, drops the branch's new tests onto it,
and runs them. If they pass there, either the criterion was already satisfied and the agent
did nothing, or the test asserts nothing. Both are invisible in a diff. It is "watch the test
fail first", mechanised.

What stays human regardless of how green everything is: product judgment, and changes touching
`drizzle/migrations/`, `src/auth/`, or anything with PII. The factory has already merged +9,072
lines of GDPR erasure logic unreviewed; automated gates are for correctness, and that class of
change carries legal exposure, which is a different question.

### Reference numbers from this analysis

Baseline to measure future changes against. Last 10 runs, 7 issues:

| metric | value |
|---|---|
| total spend | $146.78 |
| total wall time | 6h 31m |
| failure rate | 3 / 10 |
| spend on failed runs | ~$35 (only $22 recorded) |
| cost composition | cache read 81%, output 11%, cache write 7% |
| API calls | 1,590 across 10 runs |
| context per call | 150k–263k tokens |
| model latency | ~20 min/run |
| tool time | playwright 37m, builds 35m, nested agents 47m (5 calls) |

Three failure modes seen: background-wait deadlock (2), 60-min timeout while polling (1).
No malicious or lazy agent behaviour — no force-pushes, no `--no-verify`, no skipped tests;
`eslint-disable` uses were all narrowly scoped and justified in comments. The agent's
judgment is sound. The harness around it is what leaks time and money.

---

## Standing context

- **Governing constraint is smallness.** Second attempt at this system; the first failed on
  scope (`../learnings.md`). Prefer the 20-line fix in the runner over the new subsystem.
- **The control plane contains no LLM, by design.** It is a state machine over VMs. If a
  model call appears in `control/`, something has gone wrong architecturally. Intelligence
  lives inside the VM.
- **Fleet state lives in the `runs` table, not in a context window.** That's the reason this
  layer exists at all rather than handing an agent CLI access.
