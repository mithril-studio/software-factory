#!/usr/bin/env bash
# Wire the planner's GitHub credential, clone every repo read-only, and prove the boundary.
#
#   boxd machine cp scripts/planner-setup.sh planner:/home/boxd/planner-setup.sh
#   boxd machine exec planner 'bash ~/planner-setup.sh <the-fine-grained-token>'
#
# It runs on the planner, not here. Kept in the repo because the planner is disposable:
# the first one was rebuilt after its root filesystem corrupted, and the only copy of
# this script was in a temp directory at the time.
#
# The token is the enforcement, not the prompt: with Contents=Read-only and Issues=Read+write,
# the planner physically cannot push code, and does not need to be told not to.
set -euo pipefail
OWNER=mithril-studio
TOKEN="${1:?usage: planner-setup.sh <token>}"

echo "==> authenticating gh"
printf '%s' "$TOKEN" | gh auth login --with-token
gh auth setup-git                       # git uses gh for credentials; no token in any .git/config
echo "    logged in as: $(gh api user --jq .login)"

echo "==> cloning every $OWNER repo into ~/repos (read-only by credential)"
# Plain `git clone`, not `gh repo clone`: the latter resolves the default branch over GraphQL,
# which a fine-grained PAT cannot reach ("Resource not accessible by personal access token"),
# and it fails per-repo without failing the loop. git goes over REST via the gh credential
# helper, so no token is ever written into a .git/config.
mkdir -p ~/repos && cd ~/repos
ok=0; bad=0; failed=""
for r in $(gh repo list "$OWNER" --limit 200 --json nameWithOwner --jq '.[].nameWithOwner'); do
  name="${r##*/}"
  if [ -d "$name/.git" ]; then
    if git -C "$name" fetch --prune --quiet origin 2>/dev/null; then
      echo "    fetched  $name"; ok=$((ok+1))
    else
      echo "    FAILED   $name (fetch)"; bad=$((bad+1)); failed="$failed $name"
    fi
  elif git clone --quiet "https://github.com/$r.git" "$name" 2>/dev/null; then
    echo "    cloned   $name"; ok=$((ok+1))
  else
    rm -rf "$name"
    echo "    FAILED   $name"; bad=$((bad+1)); failed="$failed $name"
  fi
done
echo "    $ok ok, $bad failed"
if [ "$bad" -gt 0 ]; then
  echo
  echo "    Cannot read:$failed"
  echo "    The token has Metadata and Issues but not Contents. Edit it and set"
  echo "    Contents = Read-only; the token value does not change, so nothing else needs"
  echo "    updating — just re-run this script."
  exit 1
fi

echo "==> verifying the boundary"
probe=$(ls -d ~/repos/*/ 2>/dev/null | head -1)
probe="${probe%/}"
if git -C "$probe" push --dry-run origin HEAD >/dev/null 2>&1; then
  echo "    FAIL: this token can push code to $(basename "$probe")."
  echo "          That is not a planner credential — it is a builder credential."
  echo "          Re-issue it with Contents = Read-only."
  exit 1
fi
echo "    ok: push is refused (Contents is read-only)"
if gh api "repos/$OWNER/software-factory/issues" --method GET >/dev/null 2>&1; then
  echo "    ok: issues are readable"
else
  echo "    FAIL: cannot read issues — the token is missing the Issues permission."; exit 1
fi
echo
echo "==> done. Issue-write is proven the first time factory-compose creates one;"
echo "    create_backlog.sh checks write access itself and fails early if it is missing."
