#!/usr/bin/env bash
# Build Verification Test — proves the BUILD, not the session's environment.
#
# A CLEAN virtualenv, an install from pyproject.toml only, a real boot on a
# free port, then every path in contracts/openapi.yaml exercised over HTTP.
# The service boots in OFFLINE_MODE: the BVT must pass on a machine with no
# route to data.seattle.gov, which is also the demo's declared fallback.
#
# ORDERING NOTE — the local-path install below is load-bearing.
#
# The substrate is installed from a LOCAL PATH before this repo's own package
# is installed. By then `throughline>=0.2` is already satisfied, so pip never
# asks a package index for the name. Do not reorder these, and do not replace
# the local install with an index lookup: `throughline` is not published to the
# fleet's Nexus hosted repo, and a completely unrelated public project of that
# name IS on PyPI, so an index lookup resolves — to the wrong package, exit 0,
# and a service that dies at import with no `throughline.datasets_api`.
# See nemo-nvidia-demo-system/docs/PACKAGE-INDEX.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== clean source copy =="
# Install from a pristine copy, never the working tree: a stale build/ or
# *.egg-info would let the BVT "prove" an older build. Copied with python3
# rather than git, because the CI image has no git binary.
SRC="$WORK/src"
python3 - "$ROOT" "$SRC" <<'COPY'
import shutil
import sys

root, dest = sys.argv[1], sys.argv[2]
EXCLUDED = {
    ".git", ".venv", "venv", "build", "dist", "data", "htmlcov",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
}


def ignore(directory, names):
    return [n for n in names if n in EXCLUDED or n.endswith(".egg-info")]


shutil.copytree(root, dest, ignore=ignore)
print(f"copied source tree to {dest}")
COPY

echo "== clean venv =="
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --upgrade pip

# throughline is a real dependency (siren imports its dataset registry loader)
# and it is not on PyPI, so the clean venv gets it from the sibling source
# before siren is installed. pip leaves an already-satisfied requirement alone,
# so installing siren afterwards does not go looking for it on an index.
SUBSTRATE="${THROUGHLINE_SRC_DIR:-/tmp/throughline}"
if [ ! -f "$SUBSTRATE/throughline/datasets_api.py" ]; then
  echo "== fetching throughline into $SUBSTRATE =="
  bash "$ROOT/scripts/fetch-throughline.sh" "$SUBSTRATE"
fi
"$WORK/venv/bin/pip" install --quiet --no-cache-dir "$SUBSTRATE"

"$WORK/venv/bin/pip" install --quiet --no-cache-dir "$SRC"
"$WORK/venv/bin/pip" install --quiet PyYAML

echo "== boot =="
# data/ is deliberately excluded from the copy above: booting from the
# PACKAGED seed snapshot is what proves a clean install can serve the demo.
export SIREN_DATA_DIR="$WORK/data"
export OFFLINE_MODE=1
export SIREN_SUBSTRATE=mock
export SIREN_HOST=127.0.0.1
# A free port, not 8603: the first version of this script smoked whatever
# siren already happened to be running on the contract port.
SIREN_PORT="${SIREN_BVT_PORT:-$("$WORK/venv/bin/python" - <<'PORT'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PORT
)}"
export SIREN_PORT
echo "port: $SIREN_PORT"
(cd "$SRC" && "$WORK/venv/bin/python" -m siren >"$WORK/server.log" 2>&1) &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "== smoke =="
for _ in $(seq 1 30); do
  if "$WORK/venv/bin/python" -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:${SIREN_PORT}/healthz', timeout=1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server exited during boot"; cat "$WORK/server.log"; exit 1
  fi
  sleep 1
done

export SIREN_URL="http://127.0.0.1:${SIREN_PORT}"
if ! (cd "$SRC" && "$WORK/venv/bin/python" "$SRC/scripts/smoke.py"); then
  echo "--- server log ---"; cat "$WORK/server.log"; exit 1
fi

echo "== packaged seed snapshot served =="
"$WORK/venv/bin/python" - <<PY
import json, sys, urllib.request
body = json.load(urllib.request.urlopen("http://127.0.0.1:${SIREN_PORT}/pulse", timeout=10))
assert body["status"]["source"] == "snapshot", body["status"]
assert body["incidents"], "a clean install served no incidents"
print(f"pulse: {len(body['incidents'])} incidents, as of {body['status']['as_of']}")
PY

echo "== installed CLI: the dataset registry on its third surface =="
# The console script comes from the wheel, not the working tree, and it talks
# to the booted service — so this proves the shipped entry point, not a dev
# shell alias. `datasets validate` runs against the FILE and must exit 0.
export SIREN_URL
(cd "$SRC" && SIREN_DATASETS="$SRC/config/datasets.yaml" \
  "$WORK/venv/bin/siren" datasets validate)
(cd "$SRC" && "$WORK/venv/bin/siren" --url "$SIREN_URL" datasets list >/dev/null)
(cd "$SRC" && "$WORK/venv/bin/siren" --url "$SIREN_URL" \
  datasets show --id siren.seattle-fire-911 >/dev/null)
echo "CLI OK: datasets list, show and validate"

echo "BVT OK"
