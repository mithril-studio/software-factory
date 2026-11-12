---
name: factory-tests
description: Write a test in this repo's own style — a plain module with a __main__ block, no pytest, discovered by scripts/verify.sh because it exists. Use when adding or changing a control/*_test.py or telemetry/*_test.py, when a change needs a regression test, or when you need to seed a throwaway SQLite database for control or telemetry.
---

# Tests in the Software Factory

There is no pytest, no fixtures, no test runner, and no registry. A test is a module that
does its work at import time and exits non-zero if anything failed. `scripts/verify.sh`
globs `control/*_test.py` and `telemetry/*_test.py` and runs each with `python -m`, so a new
file is picked up **by existing**. Nothing registers it anywhere.

Do not introduce a test framework. Follow the file beside the one you are changing.

## The shape

```python
"""One paragraph on the defect this file exists because of.

Not what the code does — what went wrong, or what would go wrong unnoticed. A test whose
docstring only restates its assertions tells the next reader nothing they could not get
from the assertions.

Run it directly, no framework needed:

    .venv/bin/python -m control.thing_test
"""
import sys

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


# ---------- what is being established

check("the property, stated as a sentence", thing(), expected)

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
```

`check` names a **property**, not a function. "a warm-up in flight still does not claim the
repo" survives a refactor; "test_has_active_run_provision" does not.

## Async

Everything in `control/` and `telemetry/` is async. There is no event-loop fixture:

```python
def run(coro):
    return asyncio.run(coro)

run(db.init())
rows = run(db.list_runs())
```

## A throwaway database

`control` reads its path from a frozen dataclass, so assignment does not work:

```python
import tempfile
from pathlib import Path
from control.config import settings

tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "log_dir", Path(tmp.name) / "logs")
settings.log_dir.mkdir(parents=True, exist_ok=True)
run(db.init())
```

`telemetry` reads a plain module attribute per call, so it is simpler — and note that this
must be set *before* `store.init()`:

```python
from telemetry import config, store
config.db_path = Path(tmp.name) / "factory.db"
run(store.init())
```

Both layers share one database file. A test that needs both points them at the same path.

## Seeding `runs` from a telemetry test

`runs` belongs to `control`, and `telemetry` never imports it — that dependency runs one way
(`docs/architecture.md` §3.2). A telemetry test that needs the join creates the table by hand
rather than reaching across the boundary:

```python
async with aiosqlite.connect(config.db_path) as conn:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        " id TEXT PRIMARY KEY, repo TEXT, kind TEXT, status TEXT, created_at TEXT)"
    )
```

Only the columns the query under test reads. See `telemetry/digest_test.py`.

## Costs need a model the price table knows

Derived cost is a join against `SEED_PRICES`, so an `LlmCall` with an unlisted model prices
at **zero** and a spend assertion silently passes on nothing. Use a seeded name —
`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`. Dated and context suffixes are
stripped by `canonical_model`, so `claude-opus-5[1m]` is fine; an invented name is not.

## Asserting on source

Some properties are about *where* code lives — an ordering that must hold, a call that must
not appear in a particular function. `inspect.getsource` is the tool, and these are the
tests most likely to rot into false confidence, so assert on something that would have to be
deliberately reintroduced:

```python
guard_src = inspect.getsource(runner._guarded)
check("a budget failure does not buy another attempt",
      "retryable=not isinstance(exc, BudgetExceeded)" in guard_src, True)
```

For call-shape rather than presence, prefer `control/call_signatures_test.py`'s approach:
parse the AST and `inspect.signature(...).bind()` the call sites. That catches a
misremembered keyword, which `ruff --select F,E9` does not — it checks that names exist, not
that calls fit them.

## Before opening the pull request

Run `scripts/verify.sh`. It is exactly what CI runs, it is fast, and it includes the
repository-memory validator — so run it again after writing anything into `.mem/`. Do not
reconstruct the gate list by hand; that list drifted once already and runs kept failing on a
check nobody had been told about.
