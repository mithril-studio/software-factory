# Foundation-e-learning pilot

Use a dedicated pilot issue with this acceptance criterion:

```yaml
- id: AC1
  mode: probe
  statement: After the learner completes the first lesson of Demonstratiecursus, the app reports 10 earned points and the course is shown at 25% progress.
```

Put it under the issue's `## Acceptance criteria` heading. Add the following fenced YAML
under `## Verification` in the target branch (`test` by default) `.factory.md` **for this pilot**:

```yaml
version: 1
e2e:
  required: false
  setup: >-
    npm ci && docker compose up -d --wait db_test &&
    DATABASE_URL=postgres://app_user:app_user_local_dev_password@localhost:5433/foundation_test
    DATABASE_MIGRATION_URL=postgres://app_migrator:app_migrator_password@localhost:5433/foundation_test
    APP_URL=http://127.0.0.1:3100 OPENROUTER_API_KEY=e2e-fake-key npm run build
  reset: /opt/factory/e2e-agent-harness/examples/foundation-e-learning/reset-data.sh "$PWD"
  start: >-
    DATABASE_URL=postgres://app_user:app_user_local_dev_password@localhost:5433/foundation_test
    DATABASE_MIGRATION_URL=postgres://app_migrator:app_migrator_password@localhost:5433/foundation_test
    APP_URL=http://127.0.0.1:3100 OPENROUTER_API_KEY=e2e-fake-key npm run start -- -p 3100
  cleanup: docker compose stop db_test
  ready_url: http://127.0.0.1:3100/inloggen
  flows:
    - id: lesson
      start: /inloggen
      task: >-
        Sign in as test-user@example.com with password Demo-elearning-2026!.
        Use navigation to open the courses page, then visit /cursussen/demonstratiecursus.
        Open the first lesson and finish it using the button at the bottom.
        Return to the courses page using the link at the top of the lesson sidebar.
        The app is in Dutch.
      criteria: [AC1]
```

The passwords above are the app's existing disposable test credentials. The golden needs
Docker available for `db_test`. This example is documentation, not an enabled configuration.
Do not apply it indiscriminately to unrelated issues whose `AC1` describes another behavior.

First run a correct baseline, then a branch that deliberately awards 5 instead of 10 points, then the
fixed commit. Confirm the evidence shows the expected behavior, count false or inconclusive
results across repeated runs, and only then switch E2E to required for a new pilot run.

Use 5 rather than 0: the database requires positive point awards, so 0 would introduce a
database error as well as the intended incorrect reward.

The [isolated v0.1.0 pilot](https://github.com/mithril-studio/e2e-agent-harness/blob/main/docs/pilots/2026-11-12.md)
passed the baseline, caught that reward fault, and passed after reverting it. The real worker
ran in disposable VMs; required/advisory decisions were replayed offline. A queued factory
issue is still the next rollout check.
