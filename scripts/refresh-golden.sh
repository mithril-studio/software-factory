#!/usr/bin/env bash
# Bring a warm golden back in line with its repo: reset the checkout to the base branch, run
# the project's own setup, and re-save the snapshot under the same name.
#
#   scripts/refresh-golden.sh claude mithril-studio/software-factory
#   scripts/refresh-golden.sh claude acme/api --setup 'cd app && npm clean-install'
#
# A snapshot cannot be edited in place, so refreshing one is restore, update, re-save, destroy.
# Re-saving under the same name is not a mistake: boxd captures a new version and the previous
# one stays forkable, so a run dispatched mid-refresh keeps working.
#
# What it will not do is guess how to install a project. The command is an argument, or it is
# the one the repo names in the `## Setup` section of its own `.factory.md`. If neither says,
# this stops — a control plane with opinions about projects is how one project's install
# command became every project's, and that bug cost more than a missing install ever did.
#
# Refreshing is optional. A warm golden is a speed-up; every run installs for itself, and a
# repo with no warm snapshot resolves to the bare `golden-<agent>` image and is merely slower.
set -euo pipefail

repo=""
agent=""
base=""
setup=""
dir=""
keep=0

usage() {
  cat >&2 <<'USAGE'
usage: refresh-golden.sh <agent> <owner/repo> [options]

  --setup CMD   how this project installs (default: the `## Setup` command in its .factory.md)
  --base REF    the branch to reset to (default: the repo's own default branch)
  --dir PATH    the checkout inside the machine (default: $HOME/work/<repo name>)
  --keep        leave the machine running after saving the snapshot
USAGE
  exit 2
}

[ $# -ge 2 ] || usage
agent=$1
repo=$2
shift 2
while [ $# -gt 0 ]; do
  case $1 in
    --setup) setup=${2:?--setup needs a command}; shift 2 ;;
    --base)  base=${2:?--base needs a branch}; shift 2 ;;
    --dir)   dir=${2:?--dir needs a path}; shift 2 ;;
    --keep)  keep=1; shift ;;
    -h|--help) usage ;;
    *)       echo "unknown option: $1" >&2; usage ;;
  esac
done

case $repo in
  */*) ;;
  *) echo "refusing: '$repo' is not owner/repo" >&2; exit 1 ;;
esac

# The same slug `control/agents.py` computes, and it has to stay the same: lowercase, every run
# of non-alphanumerics collapsed to one hyphen, no hyphen at either end. A slug that disagrees
# by one character names a snapshot no dispatch will ever resolve onto.
slug=$(printf '%s' "$repo" | tr 'A-Z' 'a-z' | sed -e 's/[^a-z0-9][^a-z0-9]*/-/g' -e 's/^-*//' -e 's/-*$//')
snapshot="golden-$agent--$slug"
vm="refresh-$agent-$slug-$$"
name=${repo##*/}
# Tilde, not $HOME: `boxd machine cp` expands `~` on the remote side and does not expand a
# variable, and this path is handed to both `cp` and `exec`.
[ -n "$dir" ] || dir="~/work/$name"

say() { echo "==> $vm: $*"; }

# Detached, then polled. A foreground `boxd machine exec` running for minutes has its session
# torn down at 60s, which for an install means a half-deleted dependency directory captured
# into a snapshot — a warm golden worse than the stale one you started with.
exec_long() {
  step=$1
  script=$2
  say "$step"
  boxd machine exec "$vm" -- "
    nohup sh -c '{ $script ; } ; echo \"FACTORY-REFRESH-EXIT=\$?\"' \
      > /tmp/factory-refresh.log 2>&1 < /dev/null &
    sleep 1
  " > /dev/null
  while :; do
    out=$(boxd machine exec "$vm" -- 'grep -h FACTORY-REFRESH-EXIT= /tmp/factory-refresh.log || true' \
            2>/dev/null | tr -dc '0-9A-Z=-')
    case $out in
      *FACTORY-REFRESH-EXIT=0*) say "$step ok"; return 0 ;;
      *FACTORY-REFRESH-EXIT=*)
        say "$step FAILED — boxd machine exec $vm -- 'tail -60 /tmp/factory-refresh.log'" >&2
        exit 1 ;;
    esac
    sleep 15
  done
}

cleanup_hint() {
  echo "the machine is still up: boxd machine connect $vm" >&2
  echo "remove it with: boxd machine remove $vm --confirm" >&2
  echo "the snapshot was not re-saved, so $snapshot is unchanged" >&2
}

# A single quote would break out of the `sh -c '…'` the detached step is wrapped in, and what
# it would break into is a shell running as the agent. Refused rather than escaped.
case $setup in
  *\'*) echo "refusing: --setup may not contain a single quote" >&2; exit 1 ;;
esac

if ! boxd snapshots list --json | grep -q "\"$snapshot\""; then
  echo "refusing: no snapshot named $snapshot" >&2
  echo "a repo with no warm golden is not broken — its runs use golden-$agent." >&2
  exit 1
fi

say "creating from $snapshot"
boxd machine new "$vm" --from-snapshot "$snapshot" --auto-suspend-timeout=0 > /dev/null

# The refusal this script exists to make. Uncommitted changes in a golden's checkout are either
# somebody's work in progress or a run's leftovers, and `git reset --hard` is the wrong answer
# to both — the right one is a human looking at what is there.
say "checking the working tree"
dirty=$(boxd machine exec "$vm" -- "cd $dir && git status --porcelain | wc -l | tr -d ' '" 2>/dev/null || echo "?")
case $dirty in
  0) ;;
  # The literal `?` this prints when the exec itself failed — quoted, because as a glob it
  # would also match a single-digit count and read "5 modified files" as "no checkout".
  "?") echo "refusing: no usable checkout at $dir in $snapshot" >&2; cleanup_hint; exit 1 ;;
  *)
    echo "refusing: $dirty uncommitted change(s) in $dir" >&2
    boxd machine exec "$vm" -- "cd $dir && git status --short" >&2 || true
    cleanup_hint
    exit 1 ;;
esac

say "fetching"
boxd machine exec "$vm" -- "cd $dir && git fetch --prune origin" > /dev/null
if [ -z "$base" ]; then
  base=$(boxd machine exec "$vm" -- \
    "cd $dir && git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null" | sed -e 's#^origin/##' -e 's/[^A-Za-z0-9._/-]//g')
fi
[ -n "$base" ] || { echo "refusing: could not work out the base branch — pass --base" >&2; cleanup_hint; exit 1; }

say "resetting to origin/$base"
boxd machine exec "$vm" -- "
  set -e
  cd $dir
  git checkout '$base'
  git reset --hard 'origin/$base'
  git log --oneline -1
"

# Read from the repo rather than decided here. `## Setup` is the section README asks every
# watched repo to carry, and preflight warns when one is missing, precisely so this step has
# something true to read instead of something plausible to guess.
if [ -z "$setup" ]; then
  say "reading the setup command from .factory.md"
  profile=$(boxd machine cp "$vm:$dir/.factory.md" - 2>/dev/null || true)
  setup=$(printf '%s\n' "$profile" | awk '
      /^#+[ \t]*[Ss][Ee][Tt][ -]?[Uu][Pp]/ { in_setup = 1; next }
      in_setup && /^#/ { exit }
      in_setup { print }
    ' | sed -n 's/.*`\([^`]*\)`.*/\1/p' | head -1)
fi
case $setup in
  *\'*) echo "refusing: the setup command in .factory.md contains a single quote" >&2; cleanup_hint; exit 1 ;;
esac
if [ -z "$setup" ]; then
  echo "refusing: $repo names no setup command" >&2
  echo "add a '## Setup' section to its .factory.md, or pass --setup" >&2
  cleanup_hint
  exit 1
fi

exec_long "installing: $setup" "cd $dir && $setup"

say "re-saving $snapshot"
boxd snapshots save "$vm" "$snapshot"

if [ "$keep" = "1" ]; then
  say "kept — remove it with: boxd machine remove $vm --confirm"
else
  say "destroying the machine"
  boxd machine remove "$vm" --confirm > /dev/null
fi

echo "==> $snapshot re-saved at $base. Runs for $repo resolve onto it ahead of golden-$agent."
