#!/usr/bin/env bash
# Ask the control plane, from outside it, whether the factory could dispatch right now.
#
# It runs in GitHub Actions and nowhere else on purpose. A monitor that lives on the control
# plane dies with it — which is precisely the moment it was supposed to speak up: on
# 2026-08-19 the boxd VM's root filesystem corrupted, the control plane served nothing for
# hours, and the only thing that noticed was a human asking. GitHub is outside both boxd and
# the Hetzner box the control plane is moving to, so this keeps working across that move;
# only FACTORY_URL changes.
#
# Reads FACTORY_URL, FACTORY_EMAIL, FACTORY_PASSWORD from the environment. Writes a markdown
# report to stdout. Exits 0 when the factory could dispatch, 1 when it could not.
#
#   FACTORY_URL=https://... FACTORY_EMAIL=... FACTORY_PASSWORD=... scripts/factory-health.sh
#
# Deliberately not `set -e`: every probe below is a thing we want to *report* on, so a failing
# curl has to fall through to the report rather than kill the script and leave it empty.
set -uo pipefail

: "${FACTORY_URL:?FACTORY_URL is required}"
: "${FACTORY_EMAIL:?FACTORY_EMAIL is required}"
: "${FACTORY_PASSWORD:?FACTORY_PASSWORD is required}"

URL="${FACTORY_URL%/}"
JAR=$(mktemp)
trap 'rm -f "$JAR"' EXIT
FAILED=0

fail() { FAILED=1; }

# --- 1. Is the control plane there at all, and does the session gate still work? -----------
# Login rather than /healthz: /healthz answers before the app knows whether its own config is
# usable, and a control plane nobody can log into is down for every practical purpose.
code=$(curl -sS -m 20 -c "$JAR" -o /dev/null -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"email":%s,"password":%s}' \
        "$(printf '%s' "$FACTORY_EMAIL" | jq -Rs .)" \
        "$(printf '%s' "$FACTORY_PASSWORD" | jq -Rs .)")" \
  "$URL/api/login" 2>/dev/null)

if [ "$code" != "200" ]; then
  echo "## Control plane unreachable"
  echo
  echo "\`POST $URL/api/login\` returned \`${code:-no response}\`."
  echo
  echo "Nothing below could be checked. The control plane is down, unreachable, or its login"
  echo "no longer matches \`FACTORY_AUTH_EMAIL\`/\`FACTORY_AUTH_PASSWORD\`."
  exit 1
fi

echo "## Factory health"
echo
echo "Control plane at \`$URL\` is up and accepting logins."
echo

# --- 2. What is it watching, and does it think anything is wrong globally? -----------------
config=$(curl -sS -m 20 -b "$JAR" "$URL/api/config" 2>/dev/null)
if ! repos=$(printf '%s' "$config" | jq -er '.repos[]?' 2>/dev/null); then
  repos=""
fi

problems=$(printf '%s' "$config" | jq -r '.problems[]?' 2>/dev/null)
if [ -n "$problems" ]; then
  echo "### Control plane problems"
  echo
  printf '%s\n' "$problems" | sed 's/^/- /'
  echo
fi

# `// "unknown"` would be wrong here: jq's alternative operator fires on `false` as well as
# on null, and `false` is the answer this field gives most often.
poll=$(printf '%s' "$config" | jq -r 'if has("poll_enabled") then .poll_enabled else "unknown" end' 2>/dev/null)
echo "Polling: \`$poll\`. Watched repos: \`$(printf '%s' "${repos:-none}" | tr '\n' ' ')\`"
echo

if [ -z "$repos" ]; then
  echo "No repos are being watched, so no issue can ever be picked up."
  exit 1
fi

# --- 3. Per repo: can a run actually be dispatched? ----------------------------------------
# `/api/preflight` already spans all three layers this monitor cares about — the control
# plane answering, the boxd fleet being listable, and a golden existing for the repo to boot
# — so it is asked rather than reimplemented here.
#
# Only checks marked `fatal` fail the run. The non-fatal ones are real advice but not an
# outage: a repo with no `.factory.md` still builds, and a repo with no *warm*
# `golden-<owner-repo>` snapshot still builds from the `golden-copy` base image — the warm
# tier is a speed-up, never a requirement. Paging on those teaches everyone to ignore the
# page.
while IFS= read -r repo; do
  [ -n "$repo" ] || continue
  echo "### \`$repo\`"
  echo
  pf=$(curl -sS -m 30 -b "$JAR" --get --data-urlencode "repo=$repo" "$URL/api/preflight" 2>/dev/null)

  if ! printf '%s' "$pf" | jq -e '.checks' >/dev/null 2>&1; then
    echo "- preflight returned no checks; the control plane answered but could not run them"
    echo
    fail
    continue
  fi

  printf '%s' "$pf" | jq -r '
    .checks[]
    | (if .ok then "- ✅ " elif .fatal then "- ❌ **" else "- ⚠️ " end)
      + .name
      + (if .ok or (.fatal | not) then "" else "**" end)
      + " — " + (.detail // "")'
  echo

  if printf '%s' "$pf" | jq -e '[.checks[] | select(.fatal and (.ok | not))] | length > 0' >/dev/null 2>&1; then
    fail
  fi
done <<< "$repos"

if [ "$FAILED" -ne 0 ]; then
  echo "One or more fatal checks failed: the factory cannot dispatch a run right now."
  exit 1
fi

echo "All fatal checks pass. The factory can dispatch."
