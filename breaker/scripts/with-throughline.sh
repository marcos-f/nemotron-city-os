#!/usr/bin/env bash
# Run a command with a REAL throughline standing behind it.
#
# int+ asks for the dispatch to be held by the real gate, so the integration
# tests need a real substrate — including in CI, where none is running. This
# script produces one: scripts/fetch-throughline.sh obtains an installable
# throughline (sibling checkout, ./dist wheel, or the wheel throughline's own
# `package` job publishes), and this script installs it into a throwaway venv
# and boots it on a free port.
#
# Either way the substrate under test is throughline's real code on a real port,
# not a stand-in. Usage: scripts/with-throughline.sh <command...>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"

cleanup() {
  [ -n "${SUBSTRATE_PID:-}" ] && kill "$SUBSTRATE_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

SRC="$(bash "$ROOT/scripts/fetch-throughline.sh" "$WORK")"

echo "== install + boot throughline =="
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --upgrade pip
"$WORK/venv/bin/pip" install --quiet --no-cache-dir "$SRC"

# The wheel carries the package, not the repo's config file; fall back to the
# substrate's own default when there is no checkout to point at.
if [ -f "$SRC/config/effects.yaml" ]; then
  CONFIG="$SRC/config/effects.yaml"
else
  CONFIG=""
fi

PORT="$("$WORK/venv/bin/python" - <<'PORT'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PORT
)"
export THROUGHLINE_PORT="$PORT"
export THROUGHLINE_HOST="127.0.0.1"
export THROUGHLINE_DATA_DIR="$WORK/data"
[ -n "$CONFIG" ] && export THROUGHLINE_CONFIG="$CONFIG"
export THROUGHLINE_URL="http://127.0.0.1:$PORT"

"$WORK/venv/bin/python" -m throughline >"$WORK/throughline.log" 2>&1 &
SUBSTRATE_PID=$!

for _ in $(seq 1 60); do
  if python3 - <<PY
import sys, urllib.request
try:
    with urllib.request.urlopen("$THROUGHLINE_URL/healthz", timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
  then break; fi
  sleep 0.5
done

if ! kill -0 "$SUBSTRATE_PID" 2>/dev/null; then
  echo "throughline exited during boot"; cat "$WORK/throughline.log"; exit 1
fi
echo "throughline up at $THROUGHLINE_URL"

"$@"
STATUS=$?
echo "== throughline ledger after the run =="
python3 - <<PY
import json, urllib.request
with urllib.request.urlopen("$THROUGHLINE_URL/ledger/verify", timeout=10) as r:
    print(json.load(r))
PY
exit $STATUS
