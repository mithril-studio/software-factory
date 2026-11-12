#!/usr/bin/env bash
# Deploy the control plane onto its boxd machine.
#
#   scripts/deploy.sh                       # fast-forward to origin/main and restart
#   scripts/deploy.sh --ref some-branch     # deploy a branch, tag or sha instead
#   FACTORY_VM=other-name scripts/deploy.sh
#
# Run it from a laptop or planner box with the boxd CLI; it does its work over
# `boxd machine exec`. The machine holds a plain checkout under ~/software-factory running
# uvicorn under systemd, built by scripts/provision-boxd.sh.
#
# Before it changes anything it saves a checkpoint, `pre-deploy-<sha>`, and keeps the last
# three. The first boxd machine lost its root filesystem with nothing to go back to; a
# checkpoint restores in place in seconds (`boxd machine checkpoint restore VM NAME`).
#
# What it deliberately does not do is touch `.env`. Secrets live on the machine and nowhere
# else; scripts/factory-secrets.sh is the one way they change.
set -euo pipefail

vm=${FACTORY_VM:-mithril-factory}
ref=""

while [ $# -gt 0 ]; do
  case $1 in
    --ref) ref=${2:?--ref needs a branch, tag or sha}; shift 2 ;;
    --vm)  vm=${2:?--vm needs a machine name}; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0" >&2; exit 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v boxd >/dev/null || { echo "refusing: boxd is not installed" >&2; exit 1; }
say() { printf '\n==> %s\n' "$1"; }

status=$(boxd machine get "$vm" --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')
case $status in
  running|standby) ;;
  hibernated) boxd machine wake "$vm" >/dev/null ;;
  stopped) boxd machine start "$vm" >/dev/null ;;
  *) echo "refusing: $vm is ${status:-missing}" >&2; exit 1 ;;
esac

before=$(boxd machine exec "$vm" -- 'cd ~/software-factory && git status --porcelain --untracked-files=no | head -1 | grep -q . && echo DIRTY || git rev-parse --short HEAD')
[ "$before" != "DIRTY" ] || { echo "refusing: the checkout on $vm has local changes" >&2; exit 1; }

say "checkpoint pre-deploy-$before on $vm"
boxd machine checkpoint save "$vm" "pre-deploy-$before" >/dev/null || echo "(checkpoint skipped)"
boxd machine checkpoint list "$vm" --json 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    sys.exit()
rows = rows if isinstance(rows, list) else rows.get("checkpoints", [])
old = sorted((r for r in rows if str(r.get("name", "")).startswith("pre-deploy-")),
             key=lambda r: str(r.get("created_at", "")))[:-3]
print("\n".join(str(r["name"]) for r in old))' | while read -r name; do
  [ -n "$name" ] && boxd machine checkpoint remove "$vm" "$name" >/dev/null 2>&1 || true
done

say "deploying to $vm${ref:+ at $ref}"
boxd machine exec "$vm" --timeout 1200 -e REF="$ref" -- 'set -eu
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/software-factory"

before=$(git rev-parse HEAD)
git fetch origin --prune --quiet
if [ -n "${REF:-}" ]; then
  git checkout -q -B deploy "origin/$REF" 2>/dev/null || git checkout -q -B deploy "$REF"
else
  git checkout -q main 2>/dev/null || git checkout -q -B main origin/main
  git merge --ff-only --quiet origin/main
fi
after=$(git rev-parse HEAD)
echo "at $(git rev-parse --short HEAD) $(git log -1 --format=%s)"

# Reinstall only when something that pins a dependency moved.
if [ "$before" = "$after" ] || git diff --quiet "$before" "$after" -- pyproject.toml uv.lock; then
  echo "python deps unchanged"
else
  uv pip install --quiet --python .venv/bin/python -e .
fi
if [ "$before" = "$after" ] || git diff --quiet "$before" "$after" -- web/package.json web/package-lock.json; then
  echo "node deps unchanged"
else
  npm --prefix web ci --no-audit --no-fund >/tmp/npm-ci.log 2>&1 || { tail -20 /tmp/npm-ci.log; exit 1; }
fi
npm --prefix web run build >/tmp/npm-build.log 2>&1 || { tail -20 /tmp/npm-build.log; exit 1; }

# Credentials set with `boxd env set` are injected into exec sessions like this one and into no
# file, so systemd never sees them. Copy the ones the control plane reads into .env, so that
# rotating a token with `boxd env set` reaches the service on the next deploy. A variable boxd
# does not inject leaves its .env line alone.
python3 - <<\PY
import os, pathlib
keys = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN")
new = {k: os.environ[k] for k in keys if os.environ.get(k, "").strip()}
env = pathlib.Path(".env")
lines = env.read_text().splitlines()
seen, out = set(), []
for line in lines:
    key = line.partition("=")[0]
    if key in new:
        out.append(f"{key}={new[key]}"); seen.add(key)
    else:
        out.append(line)
out += [f"{k}={v}" for k, v in new.items() if k not in seen]
if new:
    env.write_text("\n".join(out) + "\n")
print("from boxd env:", ", ".join(sorted(new)) or "nothing")
PY
chmod 600 .env

# Schema changes apply themselves on boot — db.init() and telemetry.store.init() are both
# idempotent — so a restart is the whole migration step. `sync` because boxd reboots are cold.
sync
# An open live-log stream holds uvicorn open until the run ends, and systemd then kills the
# process before the control plane can destroy its VMs. Units built before this flag existed
# are patched in place; a no-op once it is there.
unit=/etc/systemd/system/factory.service
if [ -f "$unit" ] && ! grep -q -- "--timeout-graceful-shutdown" "$unit"; then
  sudo sed -i "s|--port 8765$|--port 8765 --timeout-graceful-shutdown 10|" "$unit"
  sudo systemctl daemon-reload
fi
sudo systemctl restart factory
for i in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8765/healthz && break; sleep 1; done
systemctl is-active factory
curl -fsS -o /dev/null -w "healthz: %{http_code}\n" http://127.0.0.1:8765/healthz'

say "deployed — https://$vm.boxd.sh (roll back: boxd machine checkpoint restore $vm pre-deploy-$before)"
