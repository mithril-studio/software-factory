"""Parsing FACTORY_REPOS into repo→machine targets.

A run forks a golden and works in the one checkout that golden holds, so pairing a repo with
the wrong golden does not fail loudly — it builds the wrong project's code against the right
project's issue. Every case below is a way that pairing could silently go wrong.

Run it directly, no framework needed:

    .venv/bin/python -m control.repo_targets_test
"""
import os
import sys

os.environ["FACTORY_GOLDEN"] = "fallback-golden"
os.environ["FACTORY_REPO_DIR"] = "/home/boxd/repo"

from control.config import RepoTarget, Settings, _targets  # noqa: E402

fails = []
def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n       got={got!r}")
    if not ok:
        fails.append(name)


def parse(raw):
    os.environ["FACTORY_REPOS"] = raw
    return _targets()


# ---------- the shape every existing deployment already has
check("a bare repo inherits the global golden and dir",
      parse("acme/one"),
      (RepoTarget("acme/one", "fallback-golden", "/home/boxd/repo"),))

check("empty means no targets at all", parse(""), ())
check("whitespace and stray commas are not targets", parse(" , ,  "), ())

# ---------- per-repo machines, the reason this exists
check("a repo may name its own golden",
      parse("acme/one=golden-one"),
      (RepoTarget("acme/one", "golden-one", "/home/boxd/repo"),))

check("a repo may name its own golden and checkout",
      parse("acme/one=golden-one:/srv/one"),
      (RepoTarget("acme/one", "golden-one", "/srv/one"),))

check("two repos each keep their own machine",
      parse("acme/one=golden-one, acme/two=golden-two:/srv/two"),
      (RepoTarget("acme/one", "golden-one", "/home/boxd/repo"),
       RepoTarget("acme/two", "golden-two", "/srv/two")))

check("mixed bare and paired entries coexist",
      parse("acme/one, acme/two=golden-two"),
      (RepoTarget("acme/one", "fallback-golden", "/home/boxd/repo"),
       RepoTarget("acme/two", "golden-two", "/home/boxd/repo")))

check("an empty golden falls back rather than becoming blank",
      parse("acme/one=:/srv/one"),
      (RepoTarget("acme/one", "fallback-golden", "/srv/one"),))

# ---------- lookup, which is what the runner actually calls
os.environ["FACTORY_REPOS"] = "acme/one=golden-one:/srv/one, acme/two=golden-two"
s = Settings(targets=_targets(), repos=tuple(t.repo for t in _targets()),
             golden="fallback-golden", repo_dir="/home/boxd/repo")

check("repos stays a plain list of names for the poller and the UI",
      s.repos, ("acme/one", "acme/two"))
check("a watched repo resolves to its own machine",
      (s.target("acme/one").golden, s.target("acme/one").repo_dir),
      ("golden-one", "/srv/one"))
check("a watched repo without a dir keeps the global one",
      s.target("acme/two").repo_dir, "/home/boxd/repo")
check("an unwatched repo (manual dispatch) falls back to the global golden",
      (s.target("acme/three").golden, s.target("acme/three").repo_dir),
      ("fallback-golden", "/home/boxd/repo"))

# ---------- the gap that must be named rather than silently skipped
homeless = Settings(targets=(RepoTarget("acme/four", "", "/home/boxd/repo"),),
                    repos=("acme/four",), golden="", repo_dir="/home/boxd/repo",
                    boxd_api_key="x", github_token="x")
check("a target with no golden anywhere is reported as missing",
      any("acme/four" in m for m in homeless.missing()), True)

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
