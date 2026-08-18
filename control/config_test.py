"""What FACTORY_REPOS means, and what the control plane does with it.

The right-hand side of an entry names an *agent*, not a machine. That is the whole point of
this layer: the snapshot an agent boots is derived from its name at dispatch time, so a
deployment adding a fourth agent adds a snapshot and no configuration at all.

Two failure modes are kept apart on purpose, and most of the checks below are really about
that line. A *missing setting* is something nobody filled in, and it blocks starting a run.
A *problem* is a complete configuration with nothing to run on — an agent whose snapshot has
not been built yet — which is a thing to go build, not a broken setting, and which can only
be answered by asking the fleet. And a typo passed to the API is neither: it is rejected,
because quietly running an issue on a different agent than the one asked for is worse than
refusing to start.

Nothing here reads the fleet, a database or a clock. Run it directly, no framework needed:

    .venv/bin/python -m control.config_test
"""
import os
import sys

from control.agents import DEFAULT_AGENT
from control.config import Settings, _watched, unknown_agent

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}"
          + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def watched(raw):
    """`_watched()` reads the environment when it is called, so set it and call it."""
    os.environ["FACTORY_REPOS"] = raw
    try:
        return _watched()
    finally:
        del os.environ["FACTORY_REPOS"]


def settings(raw, default=DEFAULT_AGENT):
    return Settings(watched=watched(raw), agent_default=default)


# The snapshots a fleet might hold. Only the name matters — that is the whole registry.
CLAUDE = "golden-claude"
PI = "golden-pi"
WARM = "golden-claude--acme-api"


# ---------- parsing

check("watched: a bare repo names no agent", watched("acme/api"), (("acme/api", ""),))
check("watched: an entry can name one", watched("acme/api=pi"), (("acme/api", "pi"),))
check("watched: several entries, order preserved",
      watched("acme/api=pi, acme/web , acme/ops=codex"),
      (("acme/api", "pi"), ("acme/web", ""), ("acme/ops", "codex")))
check("watched: whitespace around either half is not part of it",
      watched("  acme/api  =  pi  "), (("acme/api", "pi"),))
check("watched: empty entries and a trailing comma are skipped",
      watched("acme/api,,acme/web,"), (("acme/api", ""), ("acme/web", "")))
check("watched: nothing configured is not an error", watched(""), ())
check("watched: repos are just the names it parsed",
      settings("acme/api=pi,acme/web").repos, ("acme/api", "acme/web"))


# ---------- agent_for  (AC1, AC2)

check("agent_for: an entry owner/repo=pi resolves that repo to the pi agent",
      settings("acme/api=pi").agent_for("acme/api"), "pi")
check("agent_for: each repo keeps its own agent",
      settings("acme/api=pi,acme/ops=codex").agent_for("acme/ops"), "codex")

check("default agent: a repo listed with no agent of its own falls back to claude",
      settings("acme/api").agent_for("acme/api"), DEFAULT_AGENT)
check("default agent: claude is the fallback when the deployment names none",
      settings("").agent_default, "claude")
check("default agent: a deployment can change it for everything unconfigured",
      settings("acme/api", default="pi").agent_for("acme/api"), "pi")
check("default agent: a repo nobody listed still gets one",
      settings("acme/api=pi").agent_for("other/repo"), DEFAULT_AGENT)
check("default agent: an explicit agent still wins over it",
      settings("acme/api=pi", default="codex").agent_for("acme/api"), "pi")


# ---------- missing: static gaps only, so it can gate a run without an await

full = Settings(boxd_api_key="k", github_token="t", watched=watched("acme/api=pi"))
check("missing: nothing static is absent", full.missing(), [])
check("missing: the two settings nobody can guess",
      Settings(boxd_api_key="", github_token="", watched=()).missing(),
      ["BOXD_API_KEY", "GITHUB_TOKEN (or `gh auth login`)"])


# ---------- problems  (AC3)

check("problems: a repo naming an agent that has no snapshot is reported",
      settings("acme/api=pi").problems([CLAUDE]),
      ["agent 'pi' is configured but has no golden-pi snapshot"])
check("problems: and it is not reported as a missing setting",
      Settings(boxd_api_key="k", github_token="t", watched=watched("acme/api=pi")).missing(), [])
check("problems: an agent that does have one is fine",
      settings("acme/api=pi").problems([CLAUDE, PI]), [])
check("problems: a warm snapshot alone does not make the agent runnable elsewhere",
      settings("acme/api=pi").problems([WARM]),
      ["agent 'pi' is configured but has no golden-pi snapshot"])
check("problems: an empty fleet is one problem, not one per repo",
      settings("acme/api=pi,acme/web").problems([]),
      ["no golden snapshots found — build one named golden-<agent>"])
check("problems: no golden-claude leaves an unconfigured repo with nothing to run on",
      settings("acme/api").problems([PI]),
      ["no golden-claude snapshot, so a repo that names no agent has nothing to run on"])
check("problems: every configured agent is reported, each once",
      settings("acme/api=pi,acme/ops=pi,acme/web=codex").problems([CLAUDE]),
      ["agent 'pi' is configured but has no golden-pi snapshot",
       "agent 'codex' is configured but has no golden-codex snapshot"])
check("problems: a fleet that answers everything asked of it is quiet",
      settings("acme/api=pi,acme/web").problems([CLAUDE, PI, WARM]), [])


# ---------- unknown_agent  (AC4)

check("unknown agent: a name no snapshot provides is rejected",
      unknown_agent("pi", [CLAUDE]),
      "unknown agent 'pi': known agents are claude")
check("unknown agent: the rejection lists what could have been meant",
      unknown_agent("clade", [CLAUDE, PI]),
      "unknown agent 'clade': known agents are claude, pi")
check("unknown agent: with no snapshots at all, say that instead",
      unknown_agent("claude", []),
      "unknown agent 'claude': no golden snapshots exist to run it")
check("unknown agent: a known one is accepted", unknown_agent("pi", [CLAUDE, PI]), None)
check("unknown agent: a warm snapshot proves the agent exists",
      unknown_agent("claude", [WARM]), None)
check("unknown agent: surrounding whitespace does not make it unknown",
      unknown_agent("  pi  ", [PI]), None)


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
