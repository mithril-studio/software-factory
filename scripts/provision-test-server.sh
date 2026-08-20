#!/usr/bin/env bash
# Build the boxd test control plane from nothing, reproducibly.
#
#   scripts/provision-test-server.sh                    # create `software-factory-test-server`
#   scripts/provision-test-server.sh --vm other-name
#
# Same lesson as `provision-hetzner.sh`: a box configured by hand over ssh is a box nobody
# can rebuild. This one is cheaper to lose — it holds a copy of prod's data and none of its
# own — so it is described here and thrown away freely.
#
# It stops short of loading data. Run `scripts/deploy-test.sh --refresh-db` afterwards; that
# is the same command used for every later update, so the first one is not a special case.
set -euo pipefail

vm=${FACTORY_TEST_VM:-software-factory-test-server}
repo=${FACTORY_TEST_REPO:-mithril-studio/software-factory}
email=${FACTORY_TEST_EMAIL:-joost@mithril-studio.com}

while [ $# -gt 0 ]; do
  case $1 in
    --vm)    vm=${2:?--vm needs a machine name}; shift 2 ;;
    --repo)  repo=${2:?--repo needs owner/name}; shift 2 ;;
    --email) email=${2:?--email needs an address}; shift 2 ;;
    -h|--help) sed -n '2,11p' "$0" >&2; exit 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v boxd >/dev/null || { echo "refusing: boxd is not installed" >&2; exit 1; }

say() { printf '\n==> %s\n' "$1"; }

# Auto-suspend off, deliberately. A suspended machine's clock stops, so a control plane that
# suspends between inbound requests stalls a run it dispatched and looks hung. Pause it by
# hand (`boxd machine pause`) when it is not in use.
if boxd machine get "$vm" >/dev/null 2>&1; then
  say "$vm already exists, reusing it"
else
  say "creating $vm"
  boxd machine new "$vm" --auto-suspend-timeout=0 --auto-hibernate-timeout=0
fi

# The clone URL carries no token: the credential helper reads GITHUB_PAT_TOKEN out of the
# environment boxd injects, so nothing is written into .git/config that a fork would inherit.
say "cloning $repo"
boxd machine exec "$vm" -e REPO="$repo" -- 'set -eu
cd "$HOME"
if [ ! -d software-factory/.git ]; then
  git clone --quiet "https://oauth2:${GITHUB_PAT_TOKEN}@github.com/${REPO}.git" software-factory
fi
cd software-factory
git remote set-url origin "https://github.com/${REPO}.git"
git config --local credential.helper "!f() { echo username=oauth2; echo password=\$GITHUB_PAT_TOKEN; }; f"
git fetch origin --quiet
git log --oneline -1'

say "installing dependencies"
boxd machine exec "$vm" -- 'set -eu
cd "$HOME/software-factory"
[ -d .venv ] || uv venv .venv
uv pip install --python .venv/bin/python -e . 2>&1 | tail -2
npm --prefix web install --no-audit --no-fund 2>&1 | tail -2
npm --prefix web run build 2>&1 | tail -2'

# `.env` is built on the box, out of what boxd already injects, so no secret passes through
# a laptop and there is nothing to copy from prod. The staging rails are set here rather
# than left to be remembered: FACTORY_POLL=0 and FACTORY_AUTO_MERGE=0.
say "writing .env"
boxd machine exec "$vm" -e VM="$vm" -e EMAIL="$email" -- 'set -eu
cd "$HOME/software-factory"
if [ -f .env ]; then
  echo ".env already exists, leaving it alone"
  exit 0
fi
: "${GITHUB_PAT_TOKEN:?not injected by boxd}"
: "${CLAUDE_CODE_OAUTH_TOKEN:?not injected by boxd}"
PASS=$(openssl rand -base64 18 | tr -d "/+=" | cut -c1-20)
umask 077
{
  echo "# Test control plane. Built by scripts/provision-test-server.sh — NOT a copy of prod."
  echo "# Secrets come from the values boxd injects into this machine (boxd env)."
  echo
  echo "# Empty on purpose: inside a boxd VM the SDK mints a token from the machine identity."
  echo "# Set it (scripts/deploy-test.sh documents how) only to enable POST /api/runs."
  echo "BOXD_API_KEY="
  echo "GITHUB_TOKEN=$GITHUB_PAT_TOKEN"
  echo "CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN"
  echo "ANTHROPIC_API_KEY="
  echo
  echo "# --- staging rails: this box must never act on production'"'"'s behalf ---"
  echo "FACTORY_POLL=0"
  echo "FACTORY_AUTO_MERGE=0"
  echo "FACTORY_MERGE_REQUIRE_CHECKS=1"
  echo "FACTORY_REPOS="
  echo
  echo "FACTORY_REVIEW=1"
  echo "FACTORY_MAX_REVIEW_CYCLES=2"
  echo "FACTORY_MAX_CONCURRENT=1"
  echo "FACTORY_MAX_ATTEMPTS=1"
  echo "FACTORY_KEEP_FAILED=1"
  echo "FACTORY_RUN_TIMEOUT=5400"
  echo "FACTORY_AUTO_DESTROY=7200"
  echo "FACTORY_AGENT_REFRESH=300"
  echo
  echo "FACTORY_BASE_URL=https://$VM.boxd.sh"
  echo "FACTORY_AUTH_EMAIL=$EMAIL"
  echo "FACTORY_AUTH_PASSWORD=$PASS"
  echo "FACTORY_SECRET_KEY=$(openssl rand -hex 32)"
} > .env
chmod 600 .env
echo "LOGIN: $EMAIL / $PASS"'

# systemd, not nohup, for the same reason Hetzner uses it: a box that forgets its service on
# reboot is a box that is down and nobody knows. BOXD_VM_NAME is here because systemd does
# not inherit boxd's injected environment, and without it the SDK will not try in-VM auth.
say "installing factory.service"
boxd machine exec "$vm" -e VM="$vm" -- 'set -eu
sudo tee /etc/systemd/system/factory.service >/dev/null <<UNIT
[Unit]
Description=Software Factory control plane (test server)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=boxd
Group=boxd
WorkingDirectory=/home/boxd/software-factory
Environment=PATH=/home/boxd/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=BOXD_VM_NAME=$VM
ExecStart=/home/boxd/software-factory/.venv/bin/uvicorn control.app:app --host 0.0.0.0 --port 8765
Restart=always
RestartSec=5
StandardOutput=append:/home/boxd/software-factory/var/uvicorn.log
StandardError=append:/home/boxd/software-factory/var/uvicorn.log

[Install]
WantedBy=multi-user.target
UNIT
mkdir -p "$HOME/software-factory/var"
sudo systemctl daemon-reload
sudo systemctl enable factory
sudo systemctl restart factory
sleep 6
systemctl is-active factory
curl -fsS -o /dev/null -w "healthz: %{http_code}\n" http://127.0.0.1:8765/healthz'

say "publishing port 8765"
boxd machine proxy set-port --vm "$vm" --port 8765

say "provisioned — https://$vm.boxd.sh"
echo "next: scripts/deploy-test.sh --refresh-db"
