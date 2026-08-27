"""The deterministic half of the goal loop: when to plan, how to read a planner's verdict,
and where a finished plan leaves the repo's goal.

Everything else in a plan run is an agent exercising judgement; these three functions are
where that judgement is bounded. Every case below is either a way the loop could run away
(planning while off, planning inside the cooldown, stalling forever without parking) or a
way an agent's say-so could finish a project that is not finished — which is the one
direction that must always fail closed.

Run it directly, no framework needed:

    .venv/bin/python -m control.plan_logic_test
"""
import sys

from control.plan import cooldown_elapsed, parse_plan_verdict, plan_outcome, should_plan

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


NOW = "2026-08-27T12:00:00+00:00"


def row(**over):
    base = {"goal": "a working CLI", "goal_state": "active", "last_planned_at": None}
    base.update(over)
    return base


# ---------- should_plan: every gate, one at a time

check("all-clear -> plan", should_plan(row(), NOW, enabled=True, cooldown=900))
check("switched off -> never", should_plan(row(), NOW, enabled=False, cooldown=900), False)
check("unwatched repo (no row) -> never", should_plan(None, NOW, enabled=True, cooldown=900), False)
check("no goal -> nothing to plan toward",
      should_plan(row(goal=None), NOW, enabled=True, cooldown=900), False)
check("whitespace goal is no goal",
      should_plan(row(goal="   "), NOW, enabled=True, cooldown=900), False)
check("goal met -> idle until a human or an edit re-arms it",
      should_plan(row(goal_state="met"), NOW, enabled=True, cooldown=900), False)
check("stalled -> parked for a human",
      should_plan(row(goal_state="stalled"), NOW, enabled=True, cooldown=900), False)
check("state none -> no goal loop",
      should_plan(row(goal_state="none"), NOW, enabled=True, cooldown=900), False)
check("inside the cooldown -> wait",
      should_plan(row(last_planned_at="2026-08-27T11:55:00+00:00"), NOW,
                  enabled=True, cooldown=900), False)
check("cooldown elapsed -> plan again",
      should_plan(row(last_planned_at="2026-08-27T11:40:00+00:00"), NOW,
                  enabled=True, cooldown=900))

# ---------- the cooldown clock itself

check("never planned -> elapsed", cooldown_elapsed(None, NOW, 900))
check("exactly at the boundary counts as elapsed",
      cooldown_elapsed("2026-08-27T11:45:00+00:00", NOW, 900))
check("one second short does not",
      cooldown_elapsed("2026-08-27T11:45:01+00:00", NOW, 900), False)
# Both stamps come from db.utcnow, so a bad one means a hand-edited table. Elapsed rather
# than stuck: the stamp is rewritten on the very next dispatch, so failing open here cannot
# loop, while failing closed would stop the repo forever on a value nobody can see.
check("an unreadable stamp does not freeze the loop",
      cooldown_elapsed("not a timestamp", NOW, 900))

# ---------- parse_plan_verdict: fails closed

check("no verdict file -> nothing decided", parse_plan_verdict(None)[0], False)
check("garbage -> nothing decided", parse_plan_verdict("nonsense")[0], False)  # type: ignore[arg-type]
check("empty dict -> not met, no issues", parse_plan_verdict({})[:2], (False, []))
check("goal_met true survives", parse_plan_verdict({"goal_met": True})[0], True)
check("goal_met as truthy junk stays boolean",
      parse_plan_verdict({"goal_met": "yes"})[0], True)
check("issue numbers coerced to ints",
      parse_plan_verdict({"issues_created": [3, "4", 5.0]})[1], [3, 4, 5])
check("uncoercible issue numbers dropped rather than fatal",
      parse_plan_verdict({"issues_created": ["#7", None, 8]})[1], [8])
check("issues_created absent -> []", parse_plan_verdict({"goal_met": False})[1], [])
check("summary carried through",
      parse_plan_verdict({"summary": "  built it all  "})[2], "built it all")

# ---------- plan_outcome: GitHub outranks the verdict, both directions

check("issues queued -> keep building, stalls reset",
      plan_outcome(False, True, 1, 2), ("active", 0))
check("goal met beside queued issues is a contradiction -> keep building",
      plan_outcome(True, True, 0, 2), ("active", 0))
check("goal met, queue empty -> met, stalls reset",
      plan_outcome(True, False, 1, 2), ("met", 0))
check("fruitless below the cap -> active, counted",
      plan_outcome(False, False, 0, 2), ("active", 1))
check("fruitless at the cap -> stalled, exactly then",
      plan_outcome(False, False, 1, 2), ("stalled", 2))
check("a cap of one stalls on the first fruitless pass",
      plan_outcome(False, False, 0, 1), ("stalled", 1))

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
