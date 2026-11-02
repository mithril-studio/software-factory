#!/usr/bin/env bash
# Hand the control plane its GitHub and agent credentials, without either touching a disk here.
#
#   scripts/factory-secrets.sh                  # prompts for both, hidden
#   gh auth token | scripts/factory-secrets.sh --github-from-stdin
#
# The two credentials `scripts/provision-boxd.sh` deliberately leaves out, because they are a
# person's to give rather than a script's to mint:
#
#   GITHUB_TOKEN             a token with `repo` and `workflow` scope on every watched repo
#   CLAUDE_CODE_OAUTH_TOKEN  from `claude setup-token` (or set ANTHROPIC_API_KEY instead)
#
# Each goes from this shell straight into the machine's .env over `boxd machine cp -` and is
# shredded there; nothing is echoed, logged or written locally. An empty answer leaves that
# line as it was, so this is also how one credential is rotated without the other.
set -euo pipefail

vm=${FACTORY_VM:-mithril-factory}
github_from_stdin=0
while [ $# -gt 0 ]; do
  case $1 in
    --vm) vm=${2:?--vm needs a machine name}; shift 2 ;;
    --github-from-stdin) github_from_stdin=1; shift ;;
    -h|--help) sed -n '2,15p' "$0" >&2; exit 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ "$github_from_stdin" = 1 ]; then
  IFS= read -r github || true
else
  printf 'GitHub token (repo + workflow), empty to keep: ' >&2; IFS= read -rs github; echo >&2
fi
if [ -t 0 ] || [ "$github_from_stdin" = 0 ]; then
  printf 'Claude OAuth token (claude setup-token), empty to keep: ' >&2
  IFS= read -rs claude < /dev/tty; echo >&2
else
  claude=""
fi

printf 'GITHUB_TOKEN=%s\nCLAUDE_CODE_OAUTH_TOKEN=%s\n' "$github" "$claude" \
  | boxd machine cp - "$vm:/tmp/factory-secrets"

boxd machine exec "$vm" -- 'set -eu
cd "$HOME/software-factory"
python3 - <<\PY
import pathlib
new = {}
for line in pathlib.Path("/tmp/factory-secrets").read_text().splitlines():
    key, _, value = line.partition("=")
    if value.strip():
        new[key] = value.strip()
env = pathlib.Path(".env")
lines = env.read_text().splitlines()
seen = set()
out = []
for line in lines:
    key = line.partition("=")[0]
    if key in new:
        out.append(f"{key}={new[key]}")
        seen.add(key)
    else:
        out.append(line)
out += [f"{k}={v}" for k, v in new.items() if k not in seen]
env.write_text("\n".join(out) + "\n")
print("updated:", ", ".join(sorted(new)) or "nothing")
PY
chmod 600 .env
shred -u /tmp/factory-secrets 2>/dev/null || rm -f /tmp/factory-secrets
sync
sudo systemctl restart factory
for i in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8765/healthz && break; sleep 1; done
curl -fsS http://127.0.0.1:8765/healthz; echo'
