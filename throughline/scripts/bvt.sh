#!/usr/bin/env bash
# Build Verification Test — proves the BUILD, not the session's environment.
#
# A CLEAN virtualenv, an install from pyproject.toml only, a real boot on the
# real port, then every path in contracts/openapi.yaml exercised over HTTP.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== clean source copy =="
# Install from a pristine copy of the repo, never the working directory: a
# stale build/ or *.egg-info left by an earlier run would let the BVT "prove"
# an older build. Same reason for --no-cache-dir below — pip caches the built
# wheel by name+version, and the version does not move per commit.
# Copied with python3 rather than git: the CI image has no git binary.
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
"$WORK/venv/bin/pip" install --quiet --no-cache-dir "$SRC"

echo "== boot =="
export THROUGHLINE_DATA_DIR="$WORK/data"
export THROUGHLINE_CONFIG="$SRC/config/effects.yaml"
# Bind a free port rather than the service's default. The first version of
# this script used 8600, found it already taken by a running throughline, and
# happily smoked THAT service instead of the build under test.
export THROUGHLINE_PORT="${THROUGHLINE_BVT_PORT:-$("$WORK/venv/bin/python" - <<'PORT'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PORT
)}"
echo "port: $THROUGHLINE_PORT"
export THROUGHLINE_HOST="127.0.0.1"
"$WORK/venv/bin/python" -m throughline >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "== smoke =="
sleep 1
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "server exited during boot"; cat "$WORK/server.log"; exit 1
fi
if ! "$WORK/venv/bin/python" "$SRC/scripts/smoke.py"; then
  echo "--- server log ---"; cat "$WORK/server.log"; exit 1
fi
echo "== installed CLI =="
# The console script is a contract surface too: exercise the installed entry
# point, not the source tree.
"$WORK/venv/bin/throughline" --url "http://127.0.0.1:$THROUGHLINE_PORT" ledger verify
"$WORK/venv/bin/throughline" --url "http://127.0.0.1:$THROUGHLINE_PORT" \
  signal ingest --class bvt.cli --source bvt >/dev/null
echo "BVT OK"
