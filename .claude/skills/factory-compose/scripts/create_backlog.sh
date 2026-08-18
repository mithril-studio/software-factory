#!/usr/bin/env bash
# Create a factory backlog: ensure labels, create issues in order, back-fill deps, report.
#
# Usage: create_backlog.sh <owner/repo> <slug> <workdir>
#   <workdir> holds one file per issue, named NN.md (01.md, 02.md, ... zero-padded so lexical
#   order == numeric order). Each file:
#       TITLE: <the issue title>
#       <blank line>
#       <full issue body, starting with the <!-- factory-compose: <slug> step n/total --> marker>
#
# Order is the whole contract: issues are created in NN order so their GitHub numbers ascend
# with the sequence, which is the order the factory runs them. Stops on the first failure.
set -euo pipefail

REPO="${1:?usage: create_backlog.sh <owner/repo> <slug> <workdir>}"
SLUG="${2:?missing slug}"
WORKDIR="${3:?missing workdir}"

# Lifecycle labels — names AND colors must match control/github.py exactly, or the UI Plan view
# renders them inconsistently.
ensure_label() {
  gh label create "$1" --repo "$REPO" --color "$2" --description "$3" >/dev/null 2>&1 \
    || gh label edit "$1" --repo "$REPO" --color "$2" --description "$3" >/dev/null 2>&1 \
    || true
}

echo "==> validating $REPO"
gh auth status >/dev/null 2>&1 || { echo "gh not authenticated (run: gh auth login)"; exit 1; }
perm=$(gh repo view "$REPO" --json viewerPermission --jq .viewerPermission 2>/dev/null) \
  || { echo "cannot access repo: $REPO"; exit 1; }
case "$perm" in
  ADMIN|WRITE|MAINTAIN) ;;
  *) echo "no write access to $REPO (permission: $perm)"; exit 1 ;;
esac

echo "==> ensuring agent:* labels"
ensure_label agent:queued  8b93a3 "Factory: waiting to be picked up"
ensure_label agent:running e0af68 "Factory: an agent is working on this"
ensure_label agent:blocked b48ead "Factory: blocked, needs a human"
ensure_label agent:done    4ec9a5 "Factory: finished, pull request opened"
ensure_label agent:failed  f2777a "Factory: run failed"

echo "==> idempotency check for slug '$SLUG'"
# Read bodies directly rather than via `--search`, whose index lags creation and would let a
# quick re-run duplicate the backlog. Match the exact marker prefix incl. "step" so slug "url"
# doesn't collide with "url-shortener".
existing=$(gh issue list --repo "$REPO" --state all --limit 500 --json body \
  --jq "[.[] | select(.body | contains(\"factory-compose: $SLUG step\"))] | length" 2>/dev/null \
  || echo 0)
if [ "${existing:-0}" -gt 0 ]; then
  echo "ABORT: $existing issue(s) already carry the 'factory-compose: $SLUG' marker."
  echo "This backlog was already created. To add only missing steps or update existing bodies,"
  echo "do it deliberately with 'gh issue edit' — do not re-run to avoid duplicates."
  exit 2
fi

shopt -s nullglob
files=("$WORKDIR"/[0-9]*.md)
shopt -u nullglob
[ "${#files[@]}" -gt 0 ] || { echo "no NN.md step files found in $WORKDIR"; exit 1; }

# Validate before anything is created. Every defect in a drafted issue degrades silently
# downstream — a malformed criteria block makes the control plane skip review for that PR
# altogether — so this is the last place it can be caught. A gap is worse than a short backlog,
# and a backlog that disarms the reviewer is worse than both.
echo "==> validating ${#files[@]} drafted issues"
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
python3 "$SCRIPT_DIR/validate_backlog.py" "$SLUG" "$WORKDIR" || {
  echo "ABORT: the drafted backlog did not validate. Nothing was created."
  exit 1
}

echo "==> creating ${#files[@]} issues in $REPO"
numbers=(); urls=(); bodyfiles=(); titles=()
for f in "${files[@]}"; do
  title=$(head -1 "$f" | sed 's/^TITLE: *//')
  [ -n "$title" ] || { echo "missing 'TITLE:' first line in $f"; exit 1; }
  bf=$(mktemp)
  # body = everything after line 1, with leading blank lines stripped
  tail -n +2 "$f" | sed '/./,$!d' > "$bf"
  url=$(gh issue create --repo "$REPO" --title "$title" --body-file "$bf" --label agent:queued)
  num=${url##*/}
  case "$num" in ''|*[!0-9]*) echo "could not parse issue number from: $url"; exit 1 ;; esac
  numbers+=("$num"); urls+=("$url"); bodyfiles+=("$bf"); titles+=("$title")
  echo "    #$num  $title"
done

echo "==> back-filling dependency references"
for i in "${!numbers[@]}"; do
  if [ "$i" -eq 0 ]; then dep="nothing (foundation)"; else dep="#${numbers[$((i-1))]}"; fi
  sed -i.bak "s|Depends on: PENDING|Depends on: ${dep}|" "${bodyfiles[$i]}"
  gh issue edit "${numbers[$i]}" --repo "$REPO" --body-file "${bodyfiles[$i]}" >/dev/null
done

echo
echo "==> done. ${#numbers[@]} issues created in $REPO, all labeled agent:queued."
echo "    runs first: #${numbers[0]}   order: $(printf '#%s ' "${numbers[@]}")"
echo
for i in "${!urls[@]}"; do echo "    ${urls[$i]}"; done
echo
echo "    Reminder: add '$REPO' to FACTORY_REPOS (and FACTORY_POLL=1) for the poller to pick"
echo "    these up. A permanent failure on any issue halts the repo until a human clears it."
