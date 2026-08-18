"""Reading a golden's probe output, and deciding whether a repo may be dispatched to.

Both halves fail quietly if they are wrong: a probe key that stops being parsed reads as
"absent" rather than as an error, and a verdict that counts the wrong checks says READY about
a machine that cannot build anything.

Run it directly, no framework needed:

    .venv/bin/python -m control.preflight_test
"""
import sys

from control.preflight import Check, _parse, report

fails: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n       got={got!r}")
    if not ok:
        fails.append(name)


# ---------- the probe's output
PROBE = """repo_dir=ok
origin=mithril-studio/legal-ai-app
branch=main
head=fe128a9
dirty=0
fetch=ok
behind=0
skills=boxd memory
"""
p = _parse(PROBE)
check("keys and values are split on the first =", p["origin"], "mithril-studio/legal-ai-app")
check("a value with spaces survives", p["skills"], "boxd memory")
check("a key the probe never printed is absent, not empty", p.get("gh"), None)
check("a line with no = is ignored", _parse("noise\nk=v\n"), {"k": "v"})
check("an empty value stays empty rather than becoming a key", _parse("behind=\n")["behind"], "")

# ---------- the verdict
ok_ = Check("a", True, "")
warn = Check("b", False, "", fatal=False)
bad = Check("c", False, "")
check("all clear -> ready", report("r", [ok_]), True)
check("a warning alone -> still ready", report("r", [ok_, warn]), True)
check("one blocking check -> not ready", report("r", [ok_, warn, bad]), False)
check("nothing checked -> ready is vacuous but honest", report("r", []), True)

# ---------- how each is shown
check("a blocking failure reads FAIL", bad.mark, "FAIL")
check("a non-blocking one reads warn", warn.mark, "warn")

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
