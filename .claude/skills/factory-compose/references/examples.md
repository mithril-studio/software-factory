# Worked examples

## Example A — brief → ordered backlog (new repo)

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

## Objective
A URL-shortener API in FastAPI with SQLite. This first step stands up an empty but running app
with a test harness, so every later step has a green baseline to build on and a layout to
follow. Nothing here is user-facing; its whole value is that it cannot fail for six later steps.

## Task
- Create a FastAPI app with a `GET /healthz` returning `{"ok": true}`.
- Add `pyproject.toml` with fastapi + uvicorn + pytest; a `.venv` install that works.
- Add one test that asserts `/healthz` returns 200.

## Where this goes
Establishes the layout; later steps follow it.
- `app/__init__.py` — new: the FastAPI app object
- `app/main.py` — new: the `/healthz` route
- `pyproject.toml` — new: dependencies and the pytest config
- `tests/test_healthz.py` — new: proves AC1

If the repo disagrees with a path here, follow the repo and say so in the pull request.

## Acceptance criteria
```yaml
- id: AC1
  mode: test
  statement: "GET /healthz returns 200 with body {\"ok\": true}."
  verify: "tests/test_healthz.py"
- id: AC2
  mode: probe
  statement: "The app starts under uvicorn without error."
  verify: "uvicorn app.main:app --port 8099 & sleep 3; curl -fsS localhost:8099/healthz"
```

## Boundaries
- **Always:** keep the app importable as `app.main:app` — every later step and the probe above
  depend on that name.
- **Stop and flag:** if pytest cannot be made green on a clean checkout, open a draft PR rather
  than skipping or xfailing the test. Six issues queue behind this one.
- **Never:** add shortening, redirect, database or persistence logic — later steps own all of
  that. No ORM, no migrations, no `links` table yet.

## Sequence
Step 1 of 7 in the "url-shortener" backlog. Depends on: PENDING
Do not depend on any work not listed above; later steps handle the rest.
```

### Issue #4 body (note the file map lining up with the criteria)

```markdown
<!-- factory-compose: url-shortener step 4/7 -->

## Objective
Builds on the short-code creation and storage from the previous step. Codes can be created but
not yet resolved, so the product does nothing end-to-end; this step closes that loop by adding
the redirect endpoint.

## Task
- Add `GET /{code}` that looks up the code and returns a 302 redirect to the original URL.
- Return 404 with a JSON error when the code doesn't exist.

## Where this goes
Follows the layout established in step 1.
- `app/routes/redirect.py` — new: the `GET /{code}` handler
- `app/main.py` — extend: register the new router
- `tests/test_redirect.py` — new: proves AC1, AC2

If the repo disagrees with a path here, follow the repo and say so in the pull request.

## Acceptance criteria
```yaml
- id: AC1
  mode: test
  statement: "A code created via POST /shorten then fetched via GET /{code} returns 302 with the original URL in Location."
  verify: "tests/test_redirect.py::test_known_code_redirects"
- id: AC2
  mode: test
  statement: "GET /{unknown} returns 404 with a JSON body."
  verify: "tests/test_redirect.py::test_unknown_code_404"
```

## Boundaries
- **Always:** resolve codes through the storage layer added in the previous step, not with a
  fresh SQL query in the handler.
- **Stop and flag:** if the stored schema has no way to look up by code, open a draft PR saying
  so — do not migrate the schema here.
- **Never:** hit counting (next step) or input validation (its own step).

## Sequence
Step 4 of 7 in the "url-shortener" backlog. Depends on: PENDING
Do not depend on any work not listed above; later steps handle the rest.
```

## Example B — an existing repo (grounded file map)

**Brief:** "Rate-limit sign-in on the e-learning app."
**Target:** `mithril-studio/foundation-e-learning`

The difference from Example A is entirely in `## Where this goes`: the paths are **observed, not
decided**. Read the tree before drafting — where tests actually live, how modules are named,
what `package.json` scripts exist for a `probe` to call. Then stamp what you looked at:

```markdown
## Where this goes
Grounded in the repo at main@a3f91c2.
- `src/auth/throttle.ts` — new: the attempt counter and its window
- `src/auth/repository.ts` — extend: read/write `login_attempts`
- `drizzle/schema.ts` — extend: the `login_attempts` table
- `tests/integration/auth-throttle.test.ts` — new: proves AC1, AC2

If the repo disagrees with a path here, follow the repo and say so in the pull request.
```

If you did not read the tree, **omit the section**. An invented path is worse than none: it
sends the builder off to create a parallel structure the repo does not use, and it makes the
reviewer's scope-creep map wrong in both directions.

Note this issue touches `drizzle/` and `src/auth/` — both on the standing list of changes that
stay human regardless of how green the gates are. Say so in the review step.

## Example C — when to refuse

**Brief:** "Build me a SaaS."
Too vague to yield acceptance-testable issues. Do **not** draft. Ask: what does it do, for whom,
what stack, and what's the smallest thing that counts as "working"? Only compose once the answers
make issue-level acceptance criteria writable.

## Example D — when it's really several projects

**Brief:** "A mobile app, a billing backend, an admin dashboard, and a marketing site."
That's four deliverables. Decomposing into one 30-issue backlog in one repo would be wrong.
Say so and propose one compose run per repo/deliverable, sequenced by which unblocks the others.
