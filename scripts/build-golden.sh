#!/usr/bin/env bash
# Build a golden snapshot for one agent, from scratch, reproducibly.
#
# Every golden before this one was configured by hand over `boxd machine exec`, which makes
# it a pet: nobody can rebuild it, and a rebuild loses whatever the last person typed without
# saying so. A golden is a snapshot now, so building one is a script that ends in
# `snapshots save` — and what it installs is reviewable in a diff rather than remembered.
#
#   scripts/build-golden.sh claude
#   scripts/build-golden.sh codex --launch ./codex-agent.sh --manifest ./codex.json
#
# It produces `golden-<agent>`: the bare agent image, no repo. That is the tier every run
# falls back to, and the one a warm `golden-<agent>--<owner-repo>` is made from (see
# scripts/refresh-golden.sh and README "The golden VM").
#
# The one thing it cannot finish is the agent's own login. That is a browser OAuth, so the
# script stops, tells you how to do it, and waits — the credential has to be inside the
# machine before the snapshot captures it. Everything after that point is automatic.
set -euo pipefail

BASE_SNAPSHOT=${FACTORY_BASE_SNAPSHOT:-factory-base}
SKILLS_REPO=${FACTORY_SKILLS_REPO:-https://github.com/mithril-studio/agent-skills}
# Toolchains worth having on disk before any repo asks for one. See `warm_caches`.
NODE_VERSIONS=${FACTORY_WARM_NODE:-"22 20"}
PYTHON_VERSIONS=${FACTORY_WARM_PYTHON:-"3.12 3.11"}

agent=""
launch_file=""
manifest_file=""
cli_install=""
skip_login=0
keep=0

usage() {
  cat >&2 <<'USAGE'
usage: build-golden.sh <agent> [options]

  --cli-install CMD   how to install this agent's CLI (required for an unknown agent)
  --launch FILE       the /usr/local/bin/factory-agent to install (required for an unknown agent)
  --manifest FILE     the /etc/factory/agent.json to install (required for an unknown agent)
  --no-login          do not stop for the interactive login (for a deployment using a durable key)
  --keep              leave the build machine running after saving the snapshot
USAGE
  exit 2
}

[ $# -ge 1 ] || usage
agent=$1
shift
while [ $# -gt 0 ]; do
  case $1 in
    --cli-install) cli_install=${2:?--cli-install needs a command}; shift 2 ;;
    --launch)      launch_file=${2:?--launch needs a file}; shift 2 ;;
    --manifest)    manifest_file=${2:?--manifest needs a file}; shift 2 ;;
    --no-login)    skip_login=1; shift ;;
    --keep)        keep=1; shift ;;
    -h|--help)     usage ;;
    *)             echo "unknown option: $1" >&2; usage ;;
  esac
done

# The name is the registry. `control/agents.py` parses `golden-<agent>` and
# `golden-<agent>--<repo-slug>` back apart on the double hyphen, so an agent name carrying one
# would split into something nobody meant — rejected here rather than discovered at dispatch.
case $agent in
  *--*|-*|*-|"") echo "refusing: '$agent' is not a usable agent name" >&2; exit 1 ;;
esac
case $agent in
  *[!a-z0-9-]*) echo "refusing: agent names are lowercase letters, digits and hyphens" >&2; exit 1 ;;
esac

if [ "$skip_login" = "0" ] && [ ! -t 0 ]; then
  echo "refusing: the agent login needs a terminal to pause for" >&2
  echo "run this from a shell, or pass --no-login if the deployment sets a durable agent key" >&2
  exit 1
fi

snapshot="golden-$agent"
vm="build-$snapshot-$$"

say() { echo "==> $vm: $*"; }

# Long steps are detached and polled rather than waited on. A foreground `boxd machine exec`
# that runs for minutes has its session torn down ("the shell did not respond within 60s"),
# which here would mean a half-installed toolchain captured into a snapshot as if it were
# finished. The sentinel line is how we learn the exit status of something we let go of.
exec_long() {
  step=$1
  script=$2
  say "$step"
  boxd machine exec "$vm" -- "
    nohup sh -c '{ $script ; } ; echo \"FACTORY-BUILD-EXIT=\$?\"' \
      > /tmp/factory-build.log 2>&1 < /dev/null &
    sleep 1
  " > /dev/null
  while :; do
    out=$(boxd machine exec "$vm" -- 'grep -h FACTORY-BUILD-EXIT= /tmp/factory-build.log || true' \
            2>/dev/null | tr -dc '0-9A-Z=-')
    case $out in
      *FACTORY-BUILD-EXIT=0*) say "$step ok"; return 0 ;;
      *FACTORY-BUILD-EXIT=*)
        say "$step FAILED — boxd machine exec $vm -- 'tail -60 /tmp/factory-build.log'" >&2
        exit 1 ;;
    esac
    sleep 10
  done
}

# What installs each agent's own CLI. One entry per agent the factory has actually run;
# anything else must say so with --cli-install, because a wrong install command produces a
# golden that looks built and fails at the first dispatch.
default_cli_install() {
  case $1 in
    claude) echo 'curl -fsSL https://claude.ai/install.sh | bash' ;;
    *)      echo '' ;;
  esac
}

# The two files a golden owes the control plane — see control/README.md §2.3. `factory-agent`
# is exec'd with no arguments and reads $FACTORY_PROMPT itself, which is what keeps a
# multi-kilobyte prompt from being re-quoted through another shell.
write_launcher_claude() {
  boxd machine cp - "$vm:/tmp/factory-agent" <<'LAUNCHER'
#!/bin/sh
# Installed by scripts/build-golden.sh. Reads its whole contract from the environment.
exec claude -p "$FACTORY_PROMPT" \
  --effort "${FACTORY_AGENT_EFFORT:-medium}" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null
LAUNCHER
  boxd machine cp - "$vm:/tmp/agent.json" <<'MANIFEST'
{"agent": "claude", "transcript": "\"$HOME\"/.claude/projects/*/*.jsonl", "events": "claude-code"}
MANIFEST
}

# Toolchains, not packages. The prompt tells the agent the download caches are warm and that
# it must still install the repo itself, so what belongs here is only what is true of every
# repo: the runtimes a first install spends its first two minutes fetching. Guessing a
# project's dependencies would put us back where step 8 of this backlog started.
warm_caches() {
  exec_long "warming toolchain caches" "
    set -e
    export PATH=\"\$HOME/.local/share/fnm:\$HOME/.local/bin:\$PATH\"
    eval \"\$(fnm env)\" || true
    for v in $NODE_VERSIONS; do fnm install \"\$v\"; done
    fnm default $(set -- $NODE_VERSIONS; echo "$1")
    corepack enable || true
    corepack prepare pnpm@latest yarn@latest --activate || true
    for v in $PYTHON_VERSIONS; do uv python install \"\$v\" || true; done
  "
}

cleanup_hint() {
  echo "the build machine is still up: boxd machine connect $vm" >&2
  echo "remove it with: boxd machine remove $vm --confirm" >&2
}

[ -n "$cli_install" ] || cli_install=$(default_cli_install "$agent")
# A single quote would break out of the `sh -c '…'` the detached step wraps this in, and what
# it would break into is a shell on the machine. Refused rather than escaped.
case $cli_install in
  *\'*) echo "refusing: --cli-install may not contain a single quote" >&2; exit 1 ;;
esac
if [ -z "$cli_install" ]; then
  echo "refusing: no known CLI install for agent '$agent' — pass --cli-install" >&2
  exit 1
fi
if [ "$agent" != "claude" ] && { [ -z "$launch_file" ] || [ -z "$manifest_file" ]; }; then
  echo "refusing: agent '$agent' needs --launch and --manifest" >&2
  exit 1
fi

say "creating from $BASE_SNAPSHOT"
boxd machine new "$vm" --from-snapshot "$BASE_SNAPSHOT" --auto-suspend-timeout=0 > /dev/null

exec_long "installing the $agent CLI" "$cli_install"

exec_long "installing jq and fnm" '
  set -e
  command -v jq > /dev/null 2>&1 || { sudo apt-get update -qq && sudo apt-get install -y -qq jq; }
  command -v fnm > /dev/null 2>&1 || curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell
'

exec_long "installing skills" "
  set -e
  rm -rf /tmp/agent-skills
  git clone --depth 1 $SKILLS_REPO /tmp/agent-skills
  /tmp/agent-skills/install.sh
"

warm_caches

say "installing factory-agent and the manifest"
if [ -n "$launch_file" ]; then
  boxd machine cp "$launch_file" "$vm:/tmp/factory-agent"
  boxd machine cp "$manifest_file" "$vm:/tmp/agent.json"
else
  write_launcher_claude
fi
boxd machine exec "$vm" -- '
  set -e
  sudo install -m 0755 /tmp/factory-agent /usr/local/bin/factory-agent
  sudo mkdir -p /etc/factory
  sudo install -m 0644 /tmp/agent.json /etc/factory/agent.json
'

if [ "$skip_login" = "0" ]; then
  cat <<PROMPT

  The one step a script cannot do: the agent's login is a browser OAuth.

      boxd machine connect $vm
      $agent   # follow the login, then exit the shell

  The credential lands on disk inside the machine and the snapshot captures it. Every fork
  inherits it, and it expires — which is what re-running this script is for.

PROMPT
  printf 'press enter when the login is done (ctrl-c to abandon): '
  read -r _ || { echo; cleanup_hint; exit 1; }
fi

# Checked here, while the machine is still alive, because every one of these is cheap now and
# costs a forked VM and a dead run to discover later. A snapshot is only saved if they pass.
say "checking"
boxd machine exec "$vm" -e FACTORY_BUILD_AGENT="$agent" -- '
  fail=0
  note() { echo "  $1"; }
  [ -x /usr/local/bin/factory-agent ] || { note "FAIL factory-agent is not executable"; fail=1; }
  command -v factory-agent > /dev/null 2>&1 || { note "FAIL factory-agent is not on PATH"; fail=1; }
  jq -e . /etc/factory/agent.json > /dev/null 2>&1 || { note "FAIL /etc/factory/agent.json does not parse"; fail=1; }
  for tool in git gh jq fnm node; do
    command -v "$tool" > /dev/null 2>&1 || { note "FAIL $tool is missing"; fail=1; }
  done
  # Where skills and a login live differs per agent, so only the agent whose layout this
  # script knows is checked on it. Everything above is true of any golden.
  if [ "$FACTORY_BUILD_AGENT" = "claude" ]; then
    [ -n "$(ls ~/.claude/skills 2>/dev/null)" ] || { note "FAIL no skills installed"; fail=1; }
    # A warning, not a failure: a run is handed a durable agent key by the control plane, and
    # the login on the golden is only the fallback for a deployment that sets none.
    ls ~/.claude/.credentials.json > /dev/null 2>&1 || note "warn no agent login on disk"
  fi
  gh auth status > /dev/null 2>&1 || note "warn gh is not authenticated in the machine"
  [ "$fail" = "0" ] || { echo "checks failed" >&2; exit 1; }
  echo "  all checks passed"
' || { cleanup_hint; exit 1; }

say "saving $snapshot"
boxd snapshots save "$vm" "$snapshot"

if [ "$keep" = "1" ]; then
  say "kept — remove it with: boxd machine remove $vm --confirm"
else
  say "destroying the build machine"
  boxd machine remove "$vm" --confirm > /dev/null
fi

echo "==> $snapshot saved. Runs resolve onto it for any repo with no warm snapshot of its own."
