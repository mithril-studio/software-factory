#!/usr/bin/env bash
# Build the production control plane on a boxd machine, from nothing, reproducibly.
#
#   scripts/provision-boxd.sh                     # creates `mithril-factory`
#   scripts/provision-boxd.sh --vm other-name --email you@example.com
#
# Run once, from a laptop or planner box with the boxd CLI and an authenticated `gh` that may
# add deploy keys to this repo. Re-running it on an existing machine is safe: every step checks
# before it writes, and `.env` is never overwritten.
#
# The control plane lived on boxd until 2026-08-19, when that machine lost its root filesystem
# and took the run history with it; it then ran on Hetzner until that box stopped answering on
# 2026-11-02. It is back on boxd, with the two things the first boxd machine lacked: daily disk
# backups (`backups.period`), and a checkpoint before every deploy (`scripts/deploy.sh`).
#
# What it leaves behind:
#   - the machine: never suspends, never hibernates, no self-destruct, daily backups
#   - ~/software-factory: a checkout of main, fetched with a read-only deploy key
#   - .venv and web/dist built
#   - .env: a freshly minted BOXD_API_KEY, a generated login, and *no* GitHub or agent
#     credential — those are yours to hand over with scripts/factory-secrets.sh
#   - factory.service under systemd, published on port 8765, and checkpoint `provisioned`
set -euo pipefail

vm=${FACTORY_VM:-mithril-factory}
repo=${FACTORY_REPO_SLUG:-mithril-studio/software-factory}
email=${FACTORY_AUTH_EMAIL:-joost@mithril-studio.com}
watch=${FACTORY_WATCH:-mithril-studio/foundation-e-learning,mithril-studio/specter-ai}

while [ $# -gt 0 ]; do
  case $1 in
    --vm)    vm=${2:?--vm needs a machine name}; shift 2 ;;
    --email) email=${2:?--email needs an address}; shift 2 ;;
    --watch) watch=${2:?--watch needs owner/repo[,owner/repo]}; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0" >&2; exit 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v boxd >/dev/null || { echo "refusing: boxd is not installed" >&2; exit 1; }
command -v gh >/dev/null || { echo "refusing: gh is not installed" >&2; exit 1; }
# A name the reconciler sweeps would have the control plane destroy itself as an orphan.
case $vm in
  run-*|rev-*|prov-*|plan-*|learn-*|acc-*) echo "refusing: $vm carries a run prefix" >&2; exit 1 ;;
esac

say() { printf '\n==> %s: %s\n' "$vm" "$1"; }
on_vm() { boxd machine exec "$vm" "$@"; }

if boxd machine get "$vm" >/dev/null 2>&1; then
  say "exists, reusing it"
else
  say "creating (never suspends, never hibernates)"
  boxd machine new "$vm" --auto-suspend-timeout=0 --auto-hibernate-timeout=0 >/dev/null
fi
# A suspended control plane's clock stops, which stalls every run it is streaming.
boxd machine config set "$vm" auto-suspend.timeout 0 >/dev/null
boxd machine config set "$vm" auto-hibernate.timeout 0 >/dev/null
boxd machine config set "$vm" backups.period 24h >/dev/null
# The browser check boxd puts in front of a machine by default answers every non-browser with
# a 503 page: the health monitor, `curl`, any API client. The app has its own login.
boxd machine config set "$vm" bot-protection off >/dev/null

say "checking the toolchain"
on_vm -- 'set -e
for t in git python3 node npm curl openssl; do command -v $t >/dev/null || { echo "missing $t" >&2; exit 1; }; done
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
export PATH="$HOME/.local/bin:$PATH"; uv --version'

# Read-only and scoped to this one repo. The control plane's own GitHub token is for the API
# and for runs; the checkout does not need it, so a box with no secrets yet can still deploy.
say "deploy key"
on_vm -- 'set -e
mkdir -p ~/.ssh && chmod 700 ~/.ssh
[ -f ~/.ssh/factory_checkout ] || ssh-keygen -q -t ed25519 -N "" -C "control plane checkout" -f ~/.ssh/factory_checkout
grep -q "^github.com" ~/.ssh/known_hosts 2>/dev/null || ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null
grep -q factory_checkout ~/.ssh/config 2>/dev/null || printf "Host github.com\n  IdentityFile ~/.ssh/factory_checkout\n  IdentitiesOnly yes\n" >> ~/.ssh/config
chmod 600 ~/.ssh/config'
pub=$(on_vm -- 'cat ~/.ssh/factory_checkout.pub')
# Asked of the machine first: a key that already reads the repo is a re-run, and needs no gh
# that may administer the repo (a GitHub App token usually may not).
if on_vm -e REPO="$repo" -- 'git ls-remote "git@github.com:$REPO.git" HEAD >/dev/null 2>&1'; then
  echo "deploy key already reads $repo"
elif ! out=$(echo "$pub" | gh repo deploy-key add - --repo "$repo" --title "$vm control plane (read-only)" 2>&1); then
  case $out in
    *"already in use"*|*"key is already"*) echo "deploy key already registered" ;;
    *) echo "$out" >&2
       echo "add it by hand, then re-run: echo '$pub' | gh repo deploy-key add - --repo $repo" >&2
       exit 1 ;;
  esac
fi

say "checkout"
on_vm -e REPO="$repo" -- 'set -e
cd "$HOME"
[ -d software-factory/.git ] || git clone --quiet "git@github.com:$REPO.git" software-factory
cd software-factory && git fetch --quiet origin && git log --oneline -1'

say "dependencies and the web build"
on_vm --timeout 1200 -- 'set -e
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/software-factory"
[ -d .venv ] || uv venv --quiet --python 3.12 .venv
uv pip install --quiet --python .venv/bin/python -e .
npm --prefix web ci --no-audit --no-fund >/tmp/npm-ci.log 2>&1 || { tail -20 /tmp/npm-ci.log; exit 1; }
npm --prefix web run build >/tmp/npm-build.log 2>&1 || { tail -20 /tmp/npm-build.log; exit 1; }
echo built'

# Built on the box, never copied: no secret passes through a laptop. The boxd key is minted
# here because a control plane without one cannot dispatch, and it goes straight down a pipe.
say "writing .env"
if on_vm -- 'test -f $HOME/software-factory/.env'; then
  echo ".env already exists, leaving it alone"
else
  on_vm -e VM="$vm" -e EMAIL="$email" -e WATCH="$watch" -- 'set -eu
cd "$HOME/software-factory"
umask 077
{
  echo "# Production control plane. Built by scripts/provision-boxd.sh; secrets by scripts/factory-secrets.sh."
  echo "BOXD_API_KEY="
  echo "GITHUB_TOKEN="
  echo "CLAUDE_CODE_OAUTH_TOKEN="
  echo "ANTHROPIC_API_KEY="
  echo
  echo "FACTORY_REPOS=$WATCH"
  echo "FACTORY_BASE_URL=https://$VM.boxd.sh"
  echo "FACTORY_AUTH_EMAIL=$EMAIL"
  echo "FACTORY_AUTH_PASSWORD=$(openssl rand -base64 18 | tr -d "/+=" | cut -c1-20)"
  echo "FACTORY_SECRET_KEY=$(openssl rand -hex 32)"
} > .env
chmod 600 .env
echo written'
fi

# Minted only while the line is empty, so a re-run never mints a second key. `keys create`
# needs an interactive `boxd login` — an API-key session may not mint keys — so on a machine
# that has only a key this is the step that stops, with the command to run where you do.
if on_vm -- 'grep -q "^BOXD_API_KEY=." $HOME/software-factory/.env'; then
  echo "BOXD_API_KEY already set"
elif on_vm -- 'test -s /tmp/boxd-key'; then
  echo "a key minted elsewhere is waiting on the machine"
else
  # Held in a variable, never in a file, and copied only once it exists — a failed mint must
  # not overwrite a key somebody already placed.
  if ! key=$(boxd auth keys create "$vm-control-plane" 2>/dev/null) || [ -z "$key" ]; then
    echo "refusing: could not mint a key here. From a shell with \`boxd login\`, run:" >&2
    echo "  boxd auth keys create $vm-control-plane | boxd machine cp - $vm:/tmp/boxd-key" >&2
    echo "then re-run this script." >&2
    exit 1
  fi
  printf '%s\n' "$key" | boxd machine cp - "$vm:/tmp/boxd-key" >/dev/null
  unset key
fi
if on_vm -- 'test -s /tmp/boxd-key'; then
  on_vm -- 'set -eu
cd "$HOME/software-factory"
KEY=$(grep -o "bxd_[A-Za-z0-9_-]*" /tmp/boxd-key | head -1)
shred -u /tmp/boxd-key 2>/dev/null || rm -f /tmp/boxd-key
[ -n "$KEY" ] || { echo "refusing: no boxd key arrived" >&2; exit 1; }
sed -i "s|^BOXD_API_KEY=.*|BOXD_API_KEY=$KEY|" .env
echo "boxd key installed"'
fi

# systemd, so a reboot brings it back. `sync` after anything written, because boxd reboots
# are cold and an unflushed file can vanish in one.
say "factory.service"
on_vm -e VM="$vm" -- 'set -eu
mkdir -p "$HOME/software-factory/var"
sudo tee /etc/systemd/system/factory.service >/dev/null <<UNIT
[Unit]
Description=Software Factory control plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$HOME/software-factory
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=BOXD_VM_NAME=$VM
ExecStart=$HOME/software-factory/.venv/bin/uvicorn control.app:app --host 0.0.0.0 --port 8765
Restart=always
RestartSec=5
StandardOutput=append:$HOME/software-factory/var/uvicorn.log
StandardError=append:$HOME/software-factory/var/uvicorn.log

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --quiet factory
sudo systemctl restart factory
sync
for i in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8765/healthz && break; sleep 1; done
systemctl is-active factory
curl -fsS http://127.0.0.1:8765/healthz; echo'

say "publishing port 8765"
boxd machine proxy set-port --vm "$vm" --port 8765 >/dev/null

say "checkpoint provisioned"
boxd machine checkpoint save "$vm" provisioned >/dev/null || echo "checkpoint skipped"

cat <<NEXT

==> $vm provisioned: https://$vm.boxd.sh
    Login: $email, password in ~/software-factory/.env on the machine:
      boxd machine exec $vm -- 'grep FACTORY_AUTH_PASSWORD ~/software-factory/.env'
    Next: scripts/factory-secrets.sh, which hands over the GitHub and agent credentials.
NEXT
