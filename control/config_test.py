"""Parsing FACTORY_REPOS, which now carries a golden per repo.

The failure this guards against is quiet and expensive: a repo paired with the wrong machine
forks a VM holding somebody else's code, and the agent works for half an hour in a checkout
that has nothing to do with its issue.

Run it directly, no framework needed:

    .venv/bin/python -m control.config_test
"""
import os
import sys

from control.config import Settings, _watched

fails: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n       got={got!r}")
    if not ok:
        fails.append(name)


def watched(repos: str, golden: str = "") -> tuple:
    os.environ["FACTORY_REPOS"] = repos
    os.environ["FACTORY_GOLDEN"] = golden
    return _watched()


# ---------- parsing
check("empty -> nothing watched", watched(""), ())
check("bare repo falls back to FACTORY_GOLDEN",
      watched("a/one", golden="g"), (("a/one", "g"),))
check("explicit golden wins over the fallback",
      watched("a/one=one-golden", golden="g"), (("a/one", "one-golden"),))
check("two repos, two goldens",
      watched("a/one=one-golden, a/two=two-golden"),
      (("a/one", "one-golden"), ("a/two", "two-golden")))
check("mixed: only the bare entry falls back",
      watched("a/one=one-golden,a/two", golden="g"),
      (("a/one", "one-golden"), ("a/two", "g")))
check("whitespace and empty entries are ignored",
      watched(" a/one = one-golden , , a/two "), (("a/one", "one-golden"), ("a/two", "")))
check("order is preserved — it is the order the poller works them in",
      [r for r, _ in watched("a/three,a/one,a/two", golden="g")],
      ["a/three", "a/one", "a/two"])

# ---------- lookup
s = Settings(golden="fallback", watched=(("a/one", "one-golden"), ("a/two", "")))
check("golden_for finds the pair", s.golden_for("a/one"), "one-golden")
check("a watched repo with no golden of its own gets none, not the wrong one",
      s.golden_for("a/two"), "")
check("an unwatched repo (a hand-started run) uses FACTORY_GOLDEN",
      s.golden_for("a/other"), "fallback")
check("repos is just the names", s.repos, ("a/one", "a/two"))
check("goldens dedupes and keeps FACTORY_GOLDEN first",
      Settings(golden="g", watched=(("a/one", "g"), ("a/two", "two-golden"))).goldens,
      ("g", "two-golden"))

# ---------- what the UI is told is wrong
check("a watched repo with no golden is a missing setting",
      any("a/two" in gap for gap in s.missing()), True)
check("a fully paired config has no golden gap",
      [g for g in Settings(golden="g", watched=(("a/one", "one-golden"),)).missing()
       if "golden" in g],
      [])

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
