# Decomposition methodology

How to turn a brief into a **well-ordered, linear** backlog. The factory runs issues
lowest-number-first, one at a time, and halts the repo on a permanent failure — so ordering and
right-sizing are correctness, not style.

## Mental model

You are writing a **sequence of pull requests**, not a spec. Each issue is exactly one PR that an
autonomous agent opens in a single VM run, working against the **merged result of every prior
issue**. The agent starts from a fresh checkout of the default branch with all earlier issues
already merged. Design each issue for that reality.

## Right-sizing one issue

An issue is correctly sized when it:

- Touches a **coherent slice** — one module / endpoint / component / migration and its tests.
  Usually on the order of a few files.
- Has **acceptance criteria the VM can verify itself** — a command that runs, a test that passes,
  an endpoint that responds. No criterion needing a human's eyes or a service the VM can't reach.
  A useful sizing test: if you cannot name the test file that would prove an issue is done, the
  issue is too vague or too large. Naming it is also what the `verify` field and the
  `## Where this goes` map ask for, so the effort is not extra — and if the file map for an
  issue runs past a handful of entries, that is the same signal by a different route.
- Describes the work **without "and also"** chaining unrelated changes. "And also" is the signal
  to split into two issues.
- Is **not churn** — if the PR would be smaller than its own boilerplate (e.g. "add one field"),
  merge it into its neighbor.

Bias **smaller**, and push **risk late**: an over-scoped early issue is the most expensive
mistake because its failure halts the whole repo.

## Sequencing (scaffolding first)

1. **Issue #1 is the thinnest possible walking skeleton** — project scaffolding, a build/test
   harness, a green baseline, the thing every later issue builds on. It is the highest-blast-
   radius issue, so make it the *most mechanical and least ambiguous* of the whole backlog.
2. Order so each issue depends only on **already-merged** earlier ones. The default spine:
   **data model → persistence → business logic → API → UI → polish.** Reorder to fit the brief.
3. **Cross-cutting concerns** (auth, config, error handling, logging) come *after* the skeleton
   but *before* the features that assume them.
4. **Integration / end-to-end wiring** near the end. **Cosmetic / polish** last of all — they are
   the safe tail if a run halts partway.

## The dependency pass (do this before finalizing order)

For each drafted issue, ask: *"What must already exist and be merged for an agent to complete
this from a fresh checkout?"* Every prerequisite you name must be an **earlier** issue. If it
isn't, either:

- it's a **missing issue** — add it earlier in the sequence, or
- it's an **out-of-scope assumption** — state it explicitly in that issue's `Boundaries`
  (`Never:`) or `Objective`.

This pass is what converts a flat feature list into a safe linear order.

## Granularity band

- **~5–15 issues** for a typical V0 project brief.
- **< 4** usually means issues are over-scoped for one VM run — split them.
- **> 20** usually means over-decomposition into churn, *or* the brief is really several projects.
  Say so, and suggest splitting into separate compose runs / repos.

State your chosen granularity and the reasoning in the review step so the human can push back.

## Smells to catch

- An issue whose acceptance criteria a VM can't check → rewrite the criteria or cut scope.
- An issue whose criteria are **all `inspect`** → nothing can block a bad PR for it. Find at
  least one fact about behaviour that a test can pin down.
- A criterion phrased as a task ("add validation") rather than a fact ("a request with no `url`
  field is rejected with 400") → rewrite it. A task is done when the agent says so; a fact is
  true or it isn't.
- An issue that assumes work not yet merged → reorder or add the missing predecessor.
- Issue #1 doing anything beyond "stand up a green skeleton" → thin it down.
- A criterion needing a real external service/secret → move it out of scope or to a documented
  manual step; it will fail forever inside the VM and halt the repo.
- A `## Where this goes` map you wrote without reading the repo → delete it. Silence costs the
  building agent some grepping; a wrong map costs a bad PR and a wrong scope-creep report.
- A `Boundaries → Always:` line that would be true of every issue in every repo ("commit as you
  go", "stay in scope") → cut it. It already lives in the builder's prompt, and repeating it
  dilutes the lines that are specific to this step.
