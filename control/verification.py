"""Frozen verification contracts and deterministic policy. No I/O or model calls."""

import hashlib
import json
import re
from urllib.parse import urlparse

import yaml

CONFIG_PATH = ".factory.md"
OUTCOMES = {"pass", "fail", "inconclusive", "error"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def freeze(profile: str, issue: dict, max_cycles: int) -> dict:
    """Absent section opts out; a present but invalid contract refuses dispatch."""
    sections = re.findall(r"^## Verification\s*\n(.*?)(?=^## |\Z)", profile, re.M | re.S)
    if not sections:
        return {"enabled": False}
    if len(sections) != 1:
        raise ValueError("exactly one Verification section is allowed")
    block = re.fullmatch(r"\s*```ya?ml\n(.*?)\n```\s*", sections[0], re.S)
    if not block:
        raise ValueError("Verification must contain one YAML block")
    config = yaml.safe_load(block[1])
    if not isinstance(config, dict) or set(config) - {"version", "max_retries", "e2e"}:
        raise ValueError("unknown verification configuration")
    if type(config.get("version")) is not int or config["version"] != 1:
        raise ValueError("verification version must be 1")
    retries = config.get("max_retries", 1)
    if type(retries) is not int or not 0 <= retries <= 2:
        raise ValueError("max_retries must be 0, 1 or 2")
    blocks = re.findall(r"^##\s*Acceptance criteria\s*\n+```ya?ml\n(.*?)^```",
                        issue.get("body") or "", re.M | re.S | re.I)
    criteria = yaml.safe_load(blocks[0]) if len(blocks) == 1 else None
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("enabled verification requires acceptance criteria")
    ids = set()
    for c in criteria:
        if (not isinstance(c, dict) or not isinstance(c.get("id"), str)
                or not re.fullmatch(r"[\w-]+", c["id"])
                or c["id"] in ids or not isinstance(c.get("mode"), str)
                or c["mode"] not in {"test", "probe", "structure", "inspect"}
                or not isinstance(c.get("statement"), str) or not c["statement"].strip()):
            raise ValueError("invalid or duplicate acceptance criterion")
        ids.add(c["id"])
    checks = [{"id": "review:approval", "verifier": "review", "required": True}]
    checks += [{"id": "review:" + c["id"], "verifier": "review",
                "required": c["mode"] != "inspect"} for c in criteria]
    e2e = config.get("e2e")
    if e2e is not None:
        fields = {"required", "setup", "reset", "start", "cleanup", "ready_url", "flows"}
        if not isinstance(e2e, dict) or set(e2e) != fields:
            raise ValueError("e2e requires exactly: " + ", ".join(sorted(fields)))
        if type(e2e["required"]) is not bool:
            raise ValueError("e2e.required must be a boolean")
        for key in fields - {"required", "flows"}:
            if not isinstance(e2e[key], str) or not e2e[key].strip():
                raise ValueError("e2e." + key + " must be nonempty text")
        url = urlparse(e2e["ready_url"])
        if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("e2e ready_url must target the VM's local HTTP app")
        if not isinstance(e2e["flows"], list) or not 1 <= len(e2e["flows"]) <= 5:
            raise ValueError("e2e needs 1–5 flows")
        flows = set()
        for flow in e2e["flows"]:
            if (not isinstance(flow, dict) or set(flow) != {"id", "start", "task", "criteria"}
                    or not isinstance(flow["id"], str) or not re.fullmatch(r"[a-z0-9-]+", flow["id"])
                    or flow["id"] in flows):
                raise ValueError("invalid or duplicate E2E flow")
            flows.add(flow["id"])
            if (not isinstance(flow["start"], str) or not flow["start"].startswith("/")
                    or flow["start"].startswith("//") or "\\" in flow["start"]
                    or not isinstance(flow["task"], str) or not flow["task"].strip()
                    or re.search(r"^#", flow["task"], re.M)):
                raise ValueError("invalid E2E task/start")
            selected = flow["criteria"]
            if (not isinstance(selected, list) or not selected
                    or any(not isinstance(cid, str) or cid not in ids for cid in selected)
                    or len(selected) != len(set(selected))):
                raise ValueError("flow must reference unique acceptance-criterion IDs")
            checks += [{"id": f"e2e:{flow['id']}:{cid}", "verifier": "e2e",
                        "required": e2e["required"]} for cid in selected]
    plan = {"enabled": True, "schema": 1, "criteria": criteria, "checks": checks,
            "max_retries": retries, "max_cycles": max_cycles, "e2e": e2e,
            "issue": issue, "profile": profile}
    return {**plan, "version": digest(plan)}


def result(plan, sha, attempt, check, outcome, evidence=(), reason=""):
    return {"check_id": check["id"], "verifier": check["verifier"], "commit": sha,
            "plan_version": plan["version"], "attempt": attempt, "outcome": outcome,
            "evidence": list(evidence), "reason": reason}


def decide(plan, sha, results, *, attempt=1, cycle=1, completed=False):
    """Only current evidence counts; conflicting attempts cannot silently become green."""
    problems = {"missing": [], "invalid": [], "conflict": [], "fail": [], "unknown": []}
    for check in plan["checks"]:
        if not check["required"]:
            continue
        cid = check["id"]
        rows = [r for r in results if isinstance(r, dict) and r.get("check_id") == cid
                and r.get("commit") == sha and r.get("plan_version") == plan["version"]]
        current = [r for r in rows if r.get("attempt") == attempt]
        if not current:
            problems["missing"].append(cid)
            continue
        if (len(current) != 1 or any(r.get("verifier") != check["verifier"]
                or not isinstance(r.get("outcome"), str) or r["outcome"] not in OUTCOMES
                or type(r.get("attempt")) is not int or not 1 <= r["attempt"] <= attempt
                or not isinstance(r.get("evidence"), list)
                or any(not isinstance(e, str) or not e.strip() for e in r["evidence"])
                or (r["outcome"] in {"pass", "fail"} and not r["evidence"])
                for r in rows)):
            problems["invalid"].append(cid)
        elif {"pass", "fail"} <= {r["outcome"] for r in rows}:
            problems["conflict"].append(cid)
        elif current[0]["outcome"] == "fail":
            problems["fail"].append(cid)
        elif current[0]["outcome"] != "pass":
            problems["unknown"].append(cid)
    if problems["invalid"] or problems["conflict"]:
        action, code = "escalate", "invalid_or_conflicting_results"
    elif problems["fail"]:
        action = "request_fixes" if cycle < plan["max_cycles"] else "escalate"
        code = "product_failure" if action == "request_fixes" else "fix_limit"
    elif problems["missing"] and not completed:
        action, code = "run_verification", "missing_or_stale_results"
    elif problems["unknown"] or problems["missing"]:
        action = "retry_verification" if attempt <= plan["max_retries"] else "escalate"
        code = "verification_unresolved" if action == "retry_verification" else "retry_limit"
    else:
        action, code = "eligible_to_merge", "required_checks_passed"
    return {"action": action, "reason": code, "checks": problems,
            "commit": sha, "plan_version": plan["version"], "attempt": attempt, "cycle": cycle}


def review_results(plan, sha, attempt, verdict, exit_code=0):
    """Preserve duplicate answers so policy rejects them, instead of last-answer-wins."""
    out = []
    for check in (c for c in plan["checks"] if c["verifier"] == "review"):
        if exit_code or not isinstance(verdict, dict):
            out.append(result(plan, sha, attempt, check, "error", reason="review did not finish"))
            continue
        if check["id"] == "review:approval":
            outcome = {"approve": "pass", "request_changes": "fail"}.get(str(verdict.get("verdict")), "error")
            findings = verdict.get("findings", [])
            if outcome == "fail" and not findings:
                outcome = "inconclusive"
            out.append(result(plan, sha, attempt, check, outcome, ["review.json"],
                              json.dumps(findings)))
            continue
        answers = verdict.get("criteria")
        if not isinstance(answers, list):
            answers = []
        matches = [a for a in answers if isinstance(a, dict)
                   and "review:" + str(a.get("id")) == check["id"]]
        for answer in matches:
            outcome = {"met": "pass", "not_met": "fail", "cannot_verify": "inconclusive"}.get(
                str(answer.get("status")), "error")
            evidence = answer.get("evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                outcome, evidence = "inconclusive", "no evidence supplied"
            out.append(result(plan, sha, attempt, check, outcome, ["review.json"], evidence))
    return out


def e2e_results(plan, sha, attempt, flow, verdict, step_ids):
    out = []
    answers = verdict.get("checks", []) if isinstance(verdict, dict) else []
    for n, cid in enumerate(flow["criteria"], 1):
        check = next(c for c in plan["checks"] if c["id"] == f"e2e:{flow['id']}:{cid}")
        matches = [a for a in answers if isinstance(a, dict) and a.get("id") == f"c{n}"] if isinstance(answers, list) else []
        if not matches:
            out.append(result(plan, sha, attempt, check, "error", reason="missing browser verdict"))
        for answer in matches:
            steps = answer.get("steps", [])
            outcome = {"pass": "pass", "fail": "fail", "not_enough_evidence": "inconclusive"}.get(
                str(answer.get("verdict")), "error")
            if (not isinstance(steps, list) or not steps
                    or any(type(s) is not int or s not in step_ids for s in steps)):
                outcome, steps = "inconclusive", []
            out.append(result(plan, sha, attempt, check, outcome,
                              [f"{flow['id']}/steps/{s:02}.jpg" for s in steps],
                              str(answer.get("reason", ""))))
    return out
