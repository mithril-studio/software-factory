"""The skill loop is mandatory only because it is observable, and gated before a review is spent.

`discussion.md` says it plainly: a written rule is not a constraint. Telling a build to run
slob-detection, simplification and design-check changes nothing on its own — the agent reads
an instruction it may or may not act on. What makes the loop mandatory is that each skill
prints a receipt the control plane reads, and that a build without all of them is sent back
before a review VM is booted to rediscover the same thing.

The parser has the memory receipt's hazard: the prompt spells the receipt line out, so an
echo of the prompt must parse as nothing rather than as a skill that never ran.

Run it directly, no framework needed:

    .venv/bin/python -m control.skill_receipt_test
"""
import asyncio
import inspect
import json
import sys
import tempfile
from pathlib import Path

from telemetry import config as telemetry_config
from telemetry import store as telemetry_store

from control import runner
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def line(**obj):
    return f"{runner.SKILL_RECEIPT_MARKER} {json.dumps(obj)}"


REQUIRED = ("slob-detection", "simplification", "design-check")

# ---------- parsing

good = line(skill="simplification", verdict="changed", summary="folded two helpers")
check("a well-formed receipt parses",
      runner.parse_skill_receipt(good),
      {"skill": "simplification", "verdict": "changed", "summary": "folded two helpers"})
check("several in one block all parse",
      [r["skill"] for r in runner.parse_skill_receipts(
          "chatter\n" + good + "\n" + line(skill="design-check", verdict="clean", summary="") + "\n")],
      ["simplification", "design-check"])
check("an unknown verdict is not a receipt",
      runner.parse_skill_receipt(line(skill="simplification", verdict="great", summary="x")), None)
check("the prompt's own example line is not a receipt",
      runner.parse_skill_receipt(
          line(skill="<name>", verdict="clean|changed|findings", summary="<one line>")), None)
check("a marker glued to its payload is not a receipt",
      runner.parse_skill_receipt(good.replace(" {", "{", 1)), None)
check("broken JSON is not a receipt",
      runner.parse_skill_receipt(f"{runner.SKILL_RECEIPT_MARKER} {{nope"), None)
check("a skill name that is not a slug is not a receipt",
      runner.parse_skill_receipt(line(skill="Rm -rf", verdict="clean", summary="")), None)
check("a long summary is bounded",
      len(runner.parse_skill_receipt(line(skill="x", verdict="clean", summary="y" * 5000))["summary"]),
      500)
check("nothing in ordinary output", runner.parse_skill_receipts("FACTORY_MEMORY {}\nhello"), [])

# ---------- the gap

check("all three seen, no gap", runner.skill_loop_gap({s: {} for s in REQUIRED}, REQUIRED), [])
check("missing ones are named in the order they are required",
      runner.skill_loop_gap({"simplification": {}}, REQUIRED), ["slob-detection", "design-check"])
check("an extra skill does not stand in for a required one",
      runner.skill_loop_gap({"other": {}}, ("design-check",)), ["design-check"])

# ---------- the prompts

object.__setattr__(settings, "skill_loop", False)
off = runner.build_prompt("a/b", {"number": 1, "title": "t", "body": "b"}, "br", "main")
check("with the loop off the build prompt does not mention it", "skill loop" in off, False)
check("and the pull request is still step 7", "7. Push the final commit" in off, True)
object.__setattr__(settings, "skill_loop", True)
object.__setattr__(settings, "skill_loop_required", REQUIRED)
on = runner.build_prompt("a/b", {"number": 1, "title": "t", "body": "b"}, "br", "main")
check("with it on, the loop comes before the pull request",
      0 < on.find("Run the skill loop") < on.find("8. Push the final commit"), True)
check("naming every required skill", all(f"`{s}`" in on for s in REQUIRED), True)
check("and the receipt marker", runner.SKILL_RECEIPT_MARKER in on, True)
check("an echo of the build prompt files no receipt", runner.parse_skill_receipts(on), [])
check("the reviewer is told to check the PR body's section", "## Skill loop" in runner.skill_loop_review(), True)
check("the review prompt has the slot for it",
      "{skill_loop_check}" in runner.REVIEW_PROMPT_TEMPLATE, True)

# ---------- the stream harvests, and telemetry keeps them

tmp = tempfile.TemporaryDirectory()
telemetry_config.db_path = Path(tmp.name) / "factory.db"
asyncio.run(telemetry_store.init())
log = runner.RunLog(Path(tmp.name) / "run.log")
seen: dict = {}
event = {"type": "user", "message": {"content": [
    {"type": "tool_result", "content": good + "\n" + line(skill="slob-detection", verdict="clean", summary="")}]}}
asyncio.run(runner._harvest_skills("run-1", runner._receipt_candidates(event), seen, log))
asyncio.run(runner._harvest_skills("run-1", (good,), seen, log))  # the same receipt again
check("receipts in a tool result are harvested", sorted(seen), ["simplification", "slob-detection"])
rows = asyncio.run(telemetry_store.skill_receipts_for_run("run-1"))
check("and persisted once each", sorted(r["skill"] for r in rows), ["simplification", "slob-detection"])
log.close()

# ---------- the gate sits before the review, and spends no VM

src = inspect.getsource(runner._execute)
check("the build path reads the receipts the stream collected",
      "_stream(boxd, machine.id, env, log, run_id, skills=skills)" in src, True)
check("the gap is computed only with the loop on",
      "skill_loop_gap(skills, settings.skill_loop_required)\n                if settings.skill_loop" in src,
      True)
check("a gap means no review", "review_next = settings.review_enabled and not unreviewable and not skill_gap" in src,
      True)
check("and a fix cycle with the reason the plan names", "reason=f\"skill-loop not run:" in src, True)
check("the fix cycle is scheduled before the run is marked finished",
      src.find("reason=f\"skill-loop not run:") < src.find('status="succeeded", pr_url=pr_url'), True)
stream_sig = inspect.signature(runner._stream)
check("other callers of _stream need not pass anything new", stream_sig.parameters["skills"].default, None)

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
