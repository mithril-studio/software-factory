#!/usr/bin/env bash
# Deploy the control plane onto its Hetzner box.
#
#   scripts/deploy.sh                       # pull the tracked branch and restart
#   scripts/deploy.sh --ref main            # deploy a specific branch or tag
#   FACTORY_HOST=factory@1.2.3.4 scripts/deploy.sh
#
# Run it from a laptop; it does its work over ssh. The box holds a plain checkout under
# ~/software-factory running uvicorn under systemd — no container, because there is one
# service and a container would only add a layer to debug through.
#
# What it deliberately does not do is touch `.env`. Secrets live on the box and nowhere else:
# there is no copy in this repo, none in CI, and none passing through anyone's shell history.
# Recreating them is `boxd env` plus a fresh FACTORY_AUTH_PASSWORD, not a file to restore.
set -euo pipefail

host=${FACTORY_HOST:-factory@46.224.40.20}
key=${FACTORY_SSH_KEY:-$HOME/.ssh/hetzner}
ref=""

while [ $# -gt 0 ]; do
  case $1 in
    --ref)  ref=${2:?--ref needs a branch, tag or sha}; shift 2 ;;
    --host) host=${2:?--host needs user@address}; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0" >&2; exit 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$1"; }

say "deploying to $host${ref:+ at $ref}"
ssh -i "$key" -o BatchMode=yes "$host" REF="$ref" 'bash -s' <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/software-factory"

before=$(git rev-parse HEAD)
git fetch origin --prune --quiet
if [ -n "${REF:-}" ]; then
  git checkout -B deploy "origin/$REF"
else
  git reset --hard "@{upstream}"
fi
after=$(git rev-parse HEAD)
echo "at $(git rev-parse --short HEAD) $(git log -1 --format=%s)"

# Reinstall only when something that pins a dependency moved. `pip install -e .` on every
# deploy is thirty wasted seconds and one more thing that can fail on a box that was fine.
if [ "$before" = "$after" ] || git diff --quiet "$before" "$after" -- pyproject.toml uv.lock; then
  echo "python deps unchanged"
else
  echo "python deps changed, reinstalling"
  uv pip install --python .venv/bin/python -e . 2>&1 | tail -2
fi

if [ "$before" = "$after" ] || git diff --quiet "$before" "$after" -- web/package.json web/package-lock.json; then
  echo "node deps unchanged"
else
  echo "node deps changed, reinstalling"
  npm --prefix web install --no-audit --no-fund 2>&1 | tail -2
fi

npm --prefix web run build 2>&1 | tail -3

# Schema changes apply themselves on boot — db.init() and telemetry.store.init() are both
# idempotent — so a restart is the whole migration step.
sudo systemctl restart factory
sleep 4
systemctl is-active factory
curl -fsS -o /dev/null -w 'healthz: %{http_code}\n' http://127.0.0.1:8765/healthz
REMOTE

say "deployed"
