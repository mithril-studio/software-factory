#!/usr/bin/env bash
# Bring a golden back in line with its repo: fast-forward the checkout, then reinstall
# whatever that project installs.
#
# The control plane deliberately does not do this itself. It sweeps goldens for drift and
# reports it (`control/goldens.py`), but reinstalling means knowing whether this project says
# `npm ci`, `uv sync` or something else — and a control plane that guesses that is a control
# plane with opinions about projects. So the install command is an argument here.
#
#   scripts/sync-golden.sh factory-golden /home/boxd/repo main 'npm ci && npm run build'
#   scripts/sync-golden.sh legal-ai-golden /home/boxd/repo main 'cd app && npm ci'
#
# Refuses to touch a golden with uncommitted changes: those are somebody's work-in-progress or
# a run's leftovers, and either way `git reset --hard` is the wrong answer.
set -euo pipefail

machine=${1:?usage: sync-golden.sh <machine> <repo-dir> <base-branch> [install command]}
repo_dir=${2:?}
base=${3:?}
install=${4:-}

echo "==> $machine: $repo_dir on $base"

boxd machine exec "$machine" "
set -e
cd '$repo_dir'
dirty=\$(git status --porcelain | wc -l | tr -d ' ')
if [ \"\$dirty\" != '0' ]; then
  echo 'refusing: working tree has '\$dirty' modified file(s)' >&2
  git status --short >&2
  exit 1
fi
git fetch --prune origin
git checkout '$base'
git reset --hard 'origin/$base'
git log --oneline -1
"

if [ -n "$install" ]; then
  echo "==> $machine: $install"
  # Detached, then polled. A foreground `boxd machine exec` that runs for minutes gets its
  # session torn down ("the shell did not respond within 60s"), which leaves a half-deleted
  # `node_modules` behind — a golden worse off than the stale one you started with. The
  # sentinel line is how we learn the exit status of something we are no longer attached to.
  boxd machine exec "$machine" "
    cd '$repo_dir'
    nohup sh -c '{ $install ; } ; echo \"FACTORY-SYNC-EXIT=\$?\"' > /tmp/factory-sync.log 2>&1 < /dev/null &
    sleep 1
  " > /dev/null

  while :; do
    out=$(boxd machine exec "$machine" 'grep -h FACTORY-SYNC-EXIT= /tmp/factory-sync.log || true' 2>/dev/null | tr -dc '0-9A-Z=-')
    case "$out" in
      *FACTORY-SYNC-EXIT=0*) echo "==> $machine: install ok"; break ;;
      *FACTORY-SYNC-EXIT=*)  echo "==> $machine: install FAILED — boxd machine exec $machine 'tail -40 /tmp/factory-sync.log'" >&2; exit 1 ;;
    esac
    sleep 15
  done
fi

echo "==> $machine: synced"
