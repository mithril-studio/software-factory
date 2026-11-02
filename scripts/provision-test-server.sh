#!/usr/bin/env bash
# Build a repo's stable test server: the persistent machine its releases are deployed to.
#
#   scripts/provision-test-server.sh mithril-studio/foundation-e-learning \
#     --start 'npm start' --port 3000 --health /api/health \
#     --install 'npm ci' --build 'npm run build' --migrate 'npm run db:migrate'
#
# Run once per repo, from a laptop with the boxd CLI and an authenticated `gh`. It is a script
# and not a run kind because it happens once, needs a human twice (the app's secrets, and a
# look at the result), and builds something that must outlive every run: `test-<owner-repo>`,
# a machine the reconciler never sweeps because its name carries no run prefix.
#
# What it leaves behind:
#   - the machine, never suspended, never hibernated, no self-destruct
#   - a checkout of the release branch at --path, fetched with a read-only deploy key
#   - a systemd unit named after the machine, reading the app's .env from the checkout
#   - a checkpoint named `initial`, the state to restore if a deploy ever wrecks the box
#   - the `## Deploy` section to paste into the repo's .factory.md
#
# What it never does: put the app's .env in git, or reuse a machine that already exists. A
# test server that went wrong is removed and rebuilt, not patched in place by a script that
# cannot know what was done to it by hand.
set -euo pipefail

BASE_SNAPSHOT=${FACTORY_BASE_SNAPSHOT:-factory-base}
branch=${FACTORY_RELEASE_BRANCH:-test}
path=/home/boxd/app
start_cmd=""
port=3000
health=/
install_cmd="npm ci"
build_cmd="npm run build"
migrate_cmd=""

usage() {
  cat >&2 <<'USAGE'
usage: provision-test-server.sh owner/repo --start CMD [options]

  --start CMD     how the app is started, in the foreground (required)
  --port N        the port it listens on (default 3000)
  --health PATH   a path that answers 2xx when the app is up (default /)
  --install CMD   install command (default: npm ci)
  --build CMD     build command (default: npm run build)
  --migrate CMD   migration command (default: none)
  --path DIR      where the checkout lives on the machine (default /home/boxd/app)
  --branch NAME   the release branch to check out (default: test, or FACTORY_RELEASE_BRANCH)
USAGE
  exit 2
}

repo=${1:-}
case $repo in
  */*) shift ;;
  *) usage ;;
esac
while [ $# -gt 0 ]; do
  case $1 in
    --start)   start_cmd=${2:?--start needs a command}; shift 2 ;;
    --port)    port=${2:?--port needs a number}; shift 2 ;;
    --health)  health=${2:?--health needs a path}; shift 2 ;;
    --install) install_cmd=${2:?--install needs a command}; shift 2 ;;
    --build)   build_cmd=${2:?--build needs a command}; shift 2 ;;
    --migrate) migrate_cmd=${2:?--migrate needs a command}; shift 2 ;;
    --path)    path=${2:?--path needs a directory}; shift 2 ;;
    --branch)  branch=${2:?--branch needs a name}; shift 2 ;;
    -h|--help) usage ;;
    *)         echo "unknown option: $1" >&2; usage ;;
  esac
done

[ -n "$start_cmd" ] || { echo "refusing: --start is required — how does this app run?" >&2; exit 1; }
case $port in *[!0-9]*|"") echo "refusing: --port is a number" >&2; exit 1 ;; esac
case $health in /*) ;; *) echo "refusing: --health is a path starting with /" >&2; exit 1 ;; esac
# Every command below crosses into a single-quoted `sh -c` on the machine. A quote in one would
# break out of it, so it is refused rather than escaped.
for cmd in "$start_cmd" "$install_cmd" "$build_cmd" "$migrate_cmd" "$path"; do
  case $cmd in *\'*) echo "refusing: commands and paths may not contain a single quote" >&2; exit 1 ;; esac
done
if [ ! -t 0 ]; then
  echo "refusing: this pauses for you twice; run it from a terminal" >&2
  exit 1
fi

slug=$(echo "$repo" | tr '[:upper:]/' '[:lower:]-' | tr -c 'a-z0-9-\n' '-')
vm="test-$slug"
service="$vm"

say() { echo "==> $vm: $*"; }
on_vm() { boxd machine exec "$vm" -- "$@"; }

if boxd machine get "$vm" > /dev/null 2>&1; then
  echo "refusing: $vm already exists. This builds a test server; it does not repair one." >&2
  echo "remove it first if you mean to rebuild: boxd machine remove $vm --confirm" >&2
  exit 1
fi
if ! gh api "repos/$repo/branches/$branch" > /dev/null 2>&1; then
  echo "refusing: $repo has no branch $branch yet." >&2
  echo "the first release creates it; or create it now: gh api repos/$repo/git/refs -f ref=refs/heads/$branch -f sha=\$(gh api repos/$repo/commits/develop -q .sha)" >&2
  exit 1
fi

say "creating from $BASE_SNAPSHOT (never suspends, never hibernates, no self-destruct)"
boxd machine new "$vm" --from-snapshot "$BASE_SNAPSHOT" \
  --auto-suspend-timeout=0 --auto-hibernate-timeout=0 > /dev/null

say "installing jq and fnm"
on_vm '
  set -e
  command -v jq > /dev/null 2>&1 || { sudo apt-get update -qq && sudo apt-get install -y -qq jq; }
  command -v fnm > /dev/null 2>&1 || curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell
'

# Read-only and scoped to this one repo: the test server can fetch the release branch and
# nothing else. The control plane's own token never lands on a machine that outlives a run.
say "creating a read-only deploy key"
on_vm '
  set -e
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  [ -f ~/.ssh/factory_deploy ] || ssh-keygen -q -t ed25519 -N "" -C "'"$vm"'" -f ~/.ssh/factory_deploy
  ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null
  printf "Host github.com\n  IdentityFile ~/.ssh/factory_deploy\n  IdentitiesOnly yes\n" > ~/.ssh/config
  chmod 600 ~/.ssh/config
'
on_vm 'cat ~/.ssh/factory_deploy.pub' | gh repo deploy-key add - --repo "$repo" --title "$vm"

say "cloning $repo on $branch into $path"
on_vm "
  set -e
  git clone --branch '$branch' 'git@github.com:$repo.git' '$path'
  cd '$path'
  export PATH=\"\$HOME/.local/share/fnm:\$PATH\"
  eval \"\$(fnm env)\"
  if [ -f .nvmrc ]; then fnm install && fnm default \$(fnm current); fi
"

# A launcher rather than the command inline in the unit: systemd expands `$VAR` in ExecStart
# whatever the quoting, and the app wants the fnm-managed node on PATH at start, not the PATH
# that happened to exist when the unit was written.
say "writing the launcher and the systemd unit $service"
user=$(on_vm 'id -un' | tr -d '\r\n')
boxd machine cp - "$vm:/tmp/$service-start" <<LAUNCH
#!/bin/bash
# Written by scripts/provision-test-server.sh. Starts $repo in the foreground for systemd.
export PATH="\$HOME/.local/share/fnm:\$PATH"
eval "\$(fnm env)"
cd '$path' || exit 1
exec $start_cmd
LAUNCH
boxd machine cp - "$vm:/tmp/$service.service" <<UNIT
[Unit]
Description=$repo test server ($branch)
After=network-online.target

[Service]
User=$user
WorkingDirectory=$path
EnvironmentFile=-$path/.env
Environment=PORT=$port
ExecStart=/usr/local/bin/$service-start
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
on_vm "
  set -e
  sudo install -m 0755 /tmp/$service-start /usr/local/bin/$service-start
  sudo install -m 0644 /tmp/$service.service /etc/systemd/system/$service.service
  sudo systemctl daemon-reload
  sudo systemctl enable $service > /dev/null
"

cat <<PROMPT

  The app's secrets. They never go in git and never pass through the control plane:

      boxd machine cp ./path/to/test.env $vm:$path/.env

  The unit reads $path/.env as its environment. Use test credentials, not production ones —
  this server is restarted by an automated deploy several times a day.

PROMPT
printf 'press enter once %s/.env is in place (ctrl-c to abandon): ' "$path"
read -r _

say "first install, build and start"
on_vm "
  set -e
  cd '$path'
  export PATH=\"\$HOME/.local/share/fnm:\$PATH\"
  eval \"\$(fnm env)\"
  $install_cmd
  $build_cmd
  ${migrate_cmd:-true}
  sudo systemctl restart $service
  for i in \$(seq 1 90); do
    curl -fsS -o /dev/null --max-time 5 'http://localhost:$port$health' && { echo '  healthy'; exit 0; }
    sleep 1
  done
  echo '  not healthy after 90s:' >&2
  sudo journalctl -u $service -n 40 --no-pager >&2
  exit 1
"

say "publishing port $port"
boxd machine proxy set-port --vm "$vm" --port "$port"
boxd machine proxy list | grep -i "$vm" || true

say "saving checkpoint initial"
boxd machine checkpoint save "$vm" initial

cat <<DEPLOY

==> $vm is serving $repo at $branch. Paste this into the repo's .factory.md, with the URL from
    the proxy list above, and commit it on the integration branch:

## Deploy

- machine: $vm
- path: $path
- install: \`$install_cmd\`
- build: \`$build_cmd\`
$( [ -n "$migrate_cmd" ] && echo "- migrate: \`$migrate_cmd\`" )
- service: $service
- health: http://localhost:$port$health
- url: https://<the proxy URL above>

    Then set FACTORY_DEPLOY=1 on the control plane. If a deploy ever leaves this machine
    broken beyond what its own rollback fixes: boxd machine checkpoint restore $vm initial
DEPLOY
