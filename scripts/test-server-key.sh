#!/usr/bin/env bash
# Mint a boxd API key for the test server and splice it into the box's .env.
#
#   scripts/test-server-key.sh
#
# The test box does not need a key to *read* the fleet — inside a boxd VM the SDK mints a
# token from the machine's own identity. But `config.Settings.missing()` checks for
# BOXD_API_KEY literally and gates `POST /api/runs`, so dispatching a run from the UI needs
# one. This is that step, and it is separate from provisioning because minting a credential
# is a deliberate act, not a side effect of rebuilding a box.
#
# The key never touches this machine's disk: it goes from `boxd auth keys create` straight
# down the pipe into the VM, and is shredded there once it is in `.env`.
set -euo pipefail

vm=${FACTORY_TEST_VM:-software-factory-test-server}

while [ $# -gt 0 ]; do
  case $1 in
    --vm) vm=${2:?--vm needs a machine name}; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0" >&2; exit 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v boxd >/dev/null || { echo "refusing: boxd is not installed" >&2; exit 1; }

printf '\n==> minting a key for %s\n' "$vm"
boxd auth keys create "$vm" | boxd machine cp - "$vm:/tmp/boxd-key"

boxd machine exec "$vm" -- 'set -eu
cd "$HOME/software-factory"
[ -s /tmp/boxd-key ] || { echo "refusing: /tmp/boxd-key is missing or empty" >&2; exit 1; }
KEY=$(tr -d "\r\n" < /tmp/boxd-key)
case "$KEY" in bxd_*) ;; *) echo "refusing: that does not look like a boxd key" >&2; exit 1 ;; esac
KEY="$KEY" python3 - <<\PY
import os, pathlib
key = os.environ["KEY"]
p = pathlib.Path(".env")
lines = p.read_text().splitlines()
out = [("BOXD_API_KEY=" + key) if l.startswith("BOXD_API_KEY=") else l for l in lines]
if not any(l.startswith("BOXD_API_KEY=") for l in out):
    out.insert(0, "BOXD_API_KEY=" + key)
p.write_text("\n".join(out) + "\n")
PY
chmod 600 .env
shred -u /tmp/boxd-key 2>/dev/null || rm -f /tmp/boxd-key
sudo systemctl restart factory
sleep 4
systemctl is-active factory
curl -fsS -o /dev/null -w "healthz: %{http_code}\n" http://127.0.0.1:8765/healthz'

printf '\n==> done — the UI can dispatch runs now\n'
