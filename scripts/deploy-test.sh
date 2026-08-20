#!/usr/bin/env bash
# Deploy a branch to the boxd test server, and optionally re-copy prod's database.
#
#   scripts/deploy-test.sh --ref my-branch      # put a branch on the test box
#   scripts/deploy-test.sh                      # redeploy whatever it is already on
#   scripts/deploy-test.sh --refresh-db         # ...and reload prod's data first
#
# The test box is a boxd VM, not Hetzner, so this talks to it over `boxd machine exec`
# rather than ssh. Everything else is the same shape as `deploy.sh`: a plain checkout under
# ~/software-factory, uvicorn under systemd, deps reinstalled only when something that pins
# them moved.
#
# It never touches `.env`. The test box's `.env` is not a copy of prod's — it is built from
# the credentials boxd injects into the machine, with FACTORY_POLL=0 and FACTORY_AUTO_MERGE=0
# so a staging control plane can never claim an issue or merge a PR out from under prod.
set -euo pipefail

vm=${FACTORY_TEST_VM:-software-factory-test-server}
prod=${FACTORY_HOST:-factory@46.224.40.20}
key=${FACTORY_SSH_KEY:-$HOME/.ssh/hetzner}
ref=""
refresh_db=0

while [ $# -gt 0 ]; do
  case $1 in
    --ref)        ref=${2:?--ref needs a branch, tag or sha}; shift 2 ;;
    --vm)         vm=${2:?--vm needs a machine name}; shift 2 ;;
    --refresh-db) refresh_db=1; shift ;;
    -h|--help)    sed -n '2,8p' "$0" >&2; exit 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v boxd >/dev/null || { echo "refusing: boxd is not installed" >&2; exit 1; }

say() { printf '\n==> %s\n' "$1"; }

if [ "$refresh_db" = 1 ]; then
  # A live SQLite file copied byte-for-byte can land mid-write, so take a real backup on the
  # prod box first. The row data is prod's, but the paths in it are not: log_path and
  # transcript_path are absolute and point at /home/factory, which does not exist here.
  say "copying the production database"
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  ssh -i "$key" -o BatchMode=yes "$prod" 'python3 - <<PY
import sqlite3
src = sqlite3.connect("/home/factory/software-factory/var/factory.db")
dst = sqlite3.connect("/tmp/factory-snapshot.db")
src.backup(dst); dst.close(); src.close()
PY
tar czf /tmp/factory-logs.tgz -C /home/factory/software-factory/var logs'
  scp -q -i "$key" -o BatchMode=yes "$prod:/tmp/factory-snapshot.db" "$tmp/factory.db"
  scp -q -i "$key" -o BatchMode=yes "$prod:/tmp/factory-logs.tgz" "$tmp/logs.tgz"

  boxd machine exec "$vm" -- 'sudo systemctl stop factory'
  boxd machine cp "$tmp/factory.db" "$vm:/home/boxd/software-factory/var/factory.db"
  boxd machine cp "$tmp/logs.tgz" "$vm:/tmp/logs.tgz"
  boxd machine exec "$vm" -- 'set -e
    cd $HOME/software-factory/var
    rm -rf logs && tar xzf /tmp/logs.tgz && rm /tmp/logs.tgz
    python3 - <<PY
import sqlite3
old, new = "/home/factory/software-factory/var/", "/home/boxd/software-factory/var/"
c = sqlite3.connect("factory.db")
for col in ("log_path", "transcript_path"):
    c.execute(f"update runs set {col}=replace({col},?,?) where {col} like ?", (old, new, old + "%"))
c.commit()
PY
    echo "database refreshed"'
fi

say "deploying to $vm${ref:+ at $ref}"
boxd machine exec "$vm" -e REF="$ref" -- 'set -eu
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

if [ "$before" = "$after" ] || git diff --quiet "$before" "$after" -- pyproject.toml uv.lock; then
  echo "python deps unchanged"
else
  uv pip install --python .venv/bin/python -e . 2>&1 | tail -2
fi

if [ "$before" = "$after" ] || git diff --quiet "$before" "$after" -- web/package.json web/package-lock.json; then
  echo "node deps unchanged"
else
  npm --prefix web install --no-audit --no-fund 2>&1 | tail -2
fi

npm --prefix web run build 2>&1 | tail -3

sudo systemctl restart factory
sleep 4
systemctl is-active factory
curl -fsS -o /dev/null -w "healthz: %{http_code}\n" http://127.0.0.1:8765/healthz'

say "deployed — https://$vm.boxd.sh"
