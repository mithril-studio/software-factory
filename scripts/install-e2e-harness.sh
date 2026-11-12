#!/usr/bin/env bash
# Run INSIDE a golden VM, with a reviewed harness source directory already copied there.
# The snapshot pins this installed copy; build agents never install it from their PR branch.
set -euo pipefail
source_dir=${1:?usage: install-e2e-harness.sh SOURCE_DIR [DESTINATION]}
destination=${2:-/opt/factory/e2e-agent-harness}
test -f "$source_dir/e2e.mjs"
test -f "$source_dir/package-lock.json"
if [ -e "$destination" ]; then
  echo "Destination already exists; choose a new version directory." >&2
  exit 1
fi
mkdir -p "$destination"
cp "$source_dir/e2e.mjs" "$source_dir/browser-server.mjs" "$source_dir/package.json" "$source_dir/package-lock.json" "$destination/"
cp -R "$source_dir/lib" "$source_dir/examples" "$destination/"
cd "$destination"
npm ci
npx playwright install --with-deps chromium
python3 - <<'PY'
import hashlib, json, pathlib
root = pathlib.Path('.')
files = [root/'package-lock.json', root/'e2e.mjs', root/'browser-server.mjs', *sorted(root.glob('lib/*.mjs'))]
manifest = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
pathlib.Path('factory-version.json').write_text(json.dumps(manifest, indent=2))
PY
echo "Installed; capture the golden only after checking its Claude login."
