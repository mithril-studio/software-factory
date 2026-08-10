# Worked examples

## Example A — brief → ordered backlog

**Brief:** "A URL-shortener API in FastAPI with SQLite. Create short codes for URLs, redirect on
lookup, basic hit counting."
**Target:** `joost/shortener`  **Slug:** `url-shortener`

**Drafted backlog (shown in chat for review):**

| # | Title | Depends on | Scope |
|---|-------|-----------|-------|
| 1 | Scaffold the FastAPI app and test harness | — | app skeleton, `/healthz`, pytest green |
| 2 | Add the SQLite schema and connection layer | 1 | `links` table, connect helper, migration |
| 3 | Implement short-code generation and storage | 2 | `POST /shorten` → code, persisted |
| 4 | Implement redirect lookup | 3 | `GET /{code}` → 302, 404 when missing |
| 5 | Add hit counting on redirect | 4 | increment + expose count |
| 6 | Add input validation and error handling | 3 | reject bad URLs, consistent errors |
| 7 | README with run + usage instructions | 5 | docs, safe tail |

Rationale: 7 issues, PR-sized each. #1 is a pure green-skeleton (lowest risk first). Persistence
before logic; validation after the endpoint it guards exists; docs last as the safe tail.

### Issue #1 body (note how thin and mechanical it is)

```markdown
<!-- factory-compose: url-shortener step 1/7 -->

## Context
A URL-shortener API in FastAPI with SQLite. This first step stands up an empty but running app
with a test harness, so every later step has a green baseline to build on.

## Task
- Create a FastAPI app with a `GET /healthz` returning `{"ok": true}`.
- Add `pyproject.toml` with fastapi + uvicorn + pytest; a `.venv` install that works.
- Add one test that asserts `/healthz` returns 200.

## Acceptance criteria
- [ ] `uvicorn app:app` starts and `GET /healthz` returns 200 `{"ok": true}`.
- [ ] `pytest` passes with at least the healthz test.
- [ ] Existing tests still pass and the build is green.

## Out of scope
- Any shortening, redirect, database, or persistence logic — later steps own all of that.

## Sequence
Step 1 of 7 in the "url-shortener" backlog. Depends on: PENDING
Do not depend on any work not listed above; later steps handle the rest.
```

### Issue #4 body (note the explicit Out of scope and VM-verifiable criteria)

```markdown
<!-- factory-compose: url-shortener step 4/7 -->

## Context
Builds on the short-code creation and storage from the previous step. This step makes stored
codes resolvable by adding the redirect endpoint.

## Task
- Add `GET /{code}` that looks up the code and returns a 302 redirect to the original URL.
- Return 404 with a JSON error when the code doesn't exist.

## Acceptance criteria
- [ ] A code created via `POST /shorten` then `GET /{code}` returns 302 with the correct Location.
- [ ] `GET /{unknown}` returns 404 with a JSON body.
- [ ] A test covers both the hit and miss paths.
- [ ] Existing tests still pass and the build is green.

## Out of scope
- Hit counting (next step) and input validation (its own step).

## Sequence
Step 4 of 7 in the "url-shortener" backlog. Depends on: PENDING
Do not depend on any work not listed above; later steps handle the rest.
```

## Example B — when to refuse

**Brief:** "Build me a SaaS."
Too vague to yield acceptance-testable issues. Do **not** draft. Ask: what does it do, for whom,
what stack, and what's the smallest thing that counts as "working"? Only compose once the answers
make issue-level acceptance criteria writable.

## Example C — when it's really several projects

**Brief:** "A mobile app, a billing backend, an admin dashboard, and a marketing site."
That's four deliverables. Decomposing into one 30-issue backlog in one repo would be wrong.
Say so and propose one compose run per repo/deliverable, sequenced by which unblocks the others.
