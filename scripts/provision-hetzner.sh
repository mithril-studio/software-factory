#!/usr/bin/env bash
# Build the control plane's Hetzner box from nothing, reproducibly.
#
#   scripts/provision-hetzner.sh                     # create `software-factory`
#   scripts/provision-hetzner.sh --name factory-2 --host factory.example.com
#
# The lesson this exists for is that rebuilding is fast and recovering is impossible: the
# previous control plane was a pet configured over ssh, and when its disk went, so did every
# decision anyone had typed into it. So the box is described here, and the only thing that
# does not come out of this script is `.env` — see `--env-from`.
#
# It stops short of installing the app. Run `scripts/deploy.sh` afterwards; that is the same
# command that deploys every subsequent change, so the first deploy is not a special one.
set -euo pipefail

name=${FACTORY_SERVER_NAME:-software-factory}
type=${FACTORY_SERVER_TYPE:-cx23}
location=${FACTORY_SERVER_LOCATION:-fsn1}
image=${FACTORY_SERVER_IMAGE:-ubuntu-24.04}
sshkey=${FACTORY_SSH_KEY_NAME:-joost-hetzner}
pubkey=${FACTORY_SSH_PUBKEY:-$HOME/.ssh/hetzner.pub}
host=${FACTORY_PUBLIC_HOST:-factory.mithril-studio.com}
env_from=""

while [ $# -gt 0 ]; do
  case $1 in
    --name)     name=${2:?}; shift 2 ;;
    --type)     type=${2:?}; shift 2 ;;
    --location) location=${2:?}; shift 2 ;;
    --host)     host=${2:?}; shift 2 ;;
    --env-from) env_from=${2:?}; shift 2 ;;
    -h|--help)  sed -n '2,12p' "$0" >&2; exit 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v hcloud >/dev/null || { echo "refusing: hcloud is not installed" >&2; exit 1; }
[ -f "$pubkey" ] || { echo "refusing: no public key at $pubkey" >&2; exit 1; }

say() { printf '\n==> %s\n' "$1"; }

# Both are idempotent: an existing key or firewall of the same name is left alone, so this
# script can be re-run against a half-built project without unpicking it first.
hcloud ssh-key describe "$sshkey" >/dev/null 2>&1 \
  || hcloud ssh-key create --name "$sshkey" --public-key-from-file "$pubkey"

if ! hcloud firewall describe factory-cp >/dev/null 2>&1; then
  say "creating firewall factory-cp"
  hcloud firewall create --name factory-cp
  for port in 22 80 443; do
    hcloud firewall add-rule factory-cp --direction in --protocol tcp --port "$port" \
      --source-ips 0.0.0.0/0 --source-ips ::/0 >/dev/null
  done
  hcloud firewall add-rule factory-cp --direction in --protocol icmp \
    --source-ips 0.0.0.0/0 --source-ips ::/0 >/dev/null
fi

cloud_init=$(mktemp)
trap 'rm -f "$cloud_init"' EXIT
cat > "$cloud_init" <<CLOUDINIT
#cloud-config
package_update: true
package_upgrade: true
users:
  - name: factory
    groups: [sudo]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    ssh_authorized_keys:
      - $(cat "$pubkey")
packages: [git, curl, ca-certificates, build-essential, python3, python3-venv,
           debian-keyring, debian-archive-keyring, apt-transport-https, unattended-upgrades]
write_files:
  - path: /etc/systemd/system/factory.service
    permissions: '0644'
    content: |
      [Unit]
      Description=Software Factory control plane
      After=network-online.target
      Wants=network-online.target

      [Service]
      Type=simple
      User=factory
      Group=factory
      WorkingDirectory=/home/factory/software-factory
      ExecStart=/home/factory/software-factory/.venv/bin/uvicorn control.app:app --host 127.0.0.1 --port 8765
      Restart=always
      RestartSec=5
      StandardOutput=append:/home/factory/software-factory/var/uvicorn.log
      StandardError=append:/home/factory/software-factory/var/uvicorn.log

      [Install]
      WantedBy=multi-user.target
  - path: /etc/apt/apt.conf.d/20auto-upgrades
    permissions: '0644'
    content: |
      APT::Periodic::Update-Package-Lists "1";
      APT::Periodic::Unattended-Upgrade "1";
runcmd:
  - curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  - apt-get install -y nodejs
  - curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  - curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
  - apt-get update
  - DEBIAN_FRONTEND=noninteractive apt-get install -y caddy
  # Written after the package, never before: a pre-placed Caddyfile makes dpkg stop at a
  # conffile prompt with no terminal to answer it, and caddy stays half-configured with no
  # 'caddy' user, which fails as an unrelated-looking systemd 217/USER.
  - |
    cat > /etc/caddy/Caddyfile <<'CADDY'
    $host {
    	encode zstd gzip
    	reverse_proxy 127.0.0.1:8765 {
    		flush_interval -1
    	}
    }
    CADDY
  - systemctl enable --now caddy
  - systemctl restart caddy
  - su - factory -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  - su - factory -c 'git clone https://github.com/mithril-studio/software-factory.git ~/software-factory'
  - systemctl enable unattended-upgrades
CLOUDINIT

say "creating $name ($type, $location)"
hcloud server create \
  --name "$name" --type "$type" --image "$image" --location "$location" \
  --ssh-key "$sshkey" --firewall factory-cp \
  --user-data-from-file "$cloud_init" \
  --enable-backup --label role=factory-control-plane

ip=$(hcloud server ip "$name")
say "waiting for cloud-init (a few minutes: it upgrades packages first)"
until ssh -i "${pubkey%.pub}" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
      -o BatchMode=yes "factory@$ip" 'cloud-init status --wait >/dev/null 2>&1 || true; cloud-init status' 2>/dev/null | grep -q done; do
  sleep 15
done

# `.env` is the one thing not described here, because describing a secret is publishing it.
# It is pushed from wherever it already lives, and if that is nowhere it is rebuilt by hand
# from `boxd env` plus a fresh FACTORY_AUTH_PASSWORD.
if [ -n "$env_from" ]; then
  say "installing .env from $env_from"
  ssh -i "${pubkey%.pub}" -o BatchMode=yes "factory@$ip" \
    'umask 077; cat > ~/software-factory/.env' < "$env_from"
else
  say "no --env-from given: put ~/software-factory/.env on the box before deploying"
fi

# Flip the WAN listener on only once TLS is real. Caddy retries the ACME challenge for a
# month, so pointing DNS here at any later point completes it without touching the box.
cat <<EOF

==> $name is up at $ip

Next:
  1. point $host at $ip (A record), and Caddy issues its certificate on the next retry
  2. FACTORY_HOST=factory@$ip scripts/deploy.sh
EOF
