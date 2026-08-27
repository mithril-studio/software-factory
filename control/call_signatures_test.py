"""Calls into the runner's shared helpers have to match the helpers.

`reap(boxd, machine, log, failed=False)` shipped to main and merged green. `reap` has no
`failed` parameter — it takes `keep` — so the call raised `TypeError`, and it sat in a
`finally`, which meant every learning run would fail during cleanup *and* leave its VM
running until the reconciler swept it. Nothing caught it: `ruff --select F,E9` checks that
names exist, not that calls fit them, and the paths that would have raised need a real boxd
machine to reach, so no test executes them.

That is a whole class, not one slip. `control/plan.py`, `control/provision.py` and the
learning path all call into `control/runner.py` for dispatch, and each new caller is another
chance to guess a keyword. So this reads the call sites out of the AST and binds them against
the real signatures — no imports executed, no VM needed, and it fails on the argument names
rather than on a crash two layers into a run nobody can reproduce locally.

Checks names and arity, not types. A wrong-typed argument still gets through; a
misremembered keyword does not.

Run it directly, no framework needed:

    .venv/bin/python -m control.call_signatures_test
"""
import ast
import inspect
import pathlib
import sys

from control import plan, provision, runner

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


# The runner helpers other modules dispatch through. Every one of these is called from at
# least two files, which is exactly the condition under which a signature and a caller drift
# apart without either author noticing.
WATCHED = (
    "reap", "dispatch_env", "_provision", "_stream", "_read_json_file",
    "_salvage_transcript", "_salvage_usage", "project_notes", "source_for",
    "headroom", "track", "advance_improvement", "create_learn",
)

MODULES = {
    "control/runner.py": runner,
    "control/plan.py": plan,
    "control/provision.py": provision,
}

ROOT = pathlib.Path(__file__).resolve().parent.parent


def called_name(node: ast.Call) -> str | None:
    """The bare function name a Call refers to, whether `reap(...)` or `runner.reap(...)`."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


sites = 0
for relpath in MODULES:
    tree = ast.parse((ROOT / relpath).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = called_name(node)
        if name not in WATCHED:
            continue
        target = getattr(runner, name, None)
        if target is None or not callable(target):
            continue

        # Positional args are passed through as placeholders; only the shape is under test,
        # so the values are irrelevant and `...` stands in for each of them. A `*args` splat
        # makes the count unknowable statically, so those sites are skipped rather than
        # guessed at — better to check nothing than to fail on something correct.
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue

        positional = [...] * len(node.args)
        keywords = {kw.arg: ... for kw in node.keywords}
        where = f"{relpath}:{node.lineno} {name}(...)"
        try:
            inspect.signature(target).bind(*positional, **keywords)
            sites += 1
        except TypeError as exc:
            check(f"{where} matches its definition", str(exc), "no error")

# The guard only means something if it is actually looking at the calls. Zero sites would
# pass silently and prove nothing — which is how a check like this rots.
check("the watched call sites were found and bound", sites >= 12, True)

# The exact call that shipped broken, stated as the thing it is: `reap` takes `keep`, and a
# caller that says `failed` is naming a parameter that has never existed.
params = set(inspect.signature(runner.reap).parameters)
check("reap takes keep", "keep" in params, True)
check("and has no failed parameter", "failed" in params, False)

# Every RunLog opened must be closed, or the process leaks a descriptor per run. Two of these
# went unclosed at once, one of them on the build dispatch path.
for relpath in ("control/runner.py", "control/plan.py", "control/provision.py"):
    source = (ROOT / relpath).read_text()
    opened = source.count("RunLog(")
    closed = source.count(".close()") - source.count("_fh.close()") - source.count("recorder.close()")
    check(f"{relpath} closes every run log it opens", closed >= opened, True)


print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
