#!/usr/bin/env bash
# Build Verification Test — proves the BUILD, not the session's environment.
#
# A pristine copy of the source, a CLEAN virtualenv, an install from
# pyproject.toml only, a real boot, then every path in contracts/openapi.yaml
# exercised over HTTP plus the installed CLI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== clean source copy =="
# Never install from the working directory: a stale build/ or *.egg-info left by
# an earlier run would let the BVT "prove" an older build. Copied with python3
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
# throughline is a real dependency (breaker/app.py mounts its dataset discovery
# surface) and it is not on PyPI, so it goes in FIRST — with it already present,
# resolving breaker's own dependencies never reaches out for a name PyPI does
# not have. Same mechanism the integration job uses; one way in, not two.
"$WORK/venv/bin/pip" install --quiet --no-cache-dir \
  "$(bash "$ROOT/scripts/fetch-throughline.sh" "$WORK/throughline")"
# --no-cache-dir: pip caches the built wheel by name+version, and the version
# does not move per commit.
"$WORK/venv/bin/pip" install --quiet --no-cache-dir "$SRC"

echo "== boot =="
# A free port, not the service default: binding 8602 would happily smoke a
# breaker that is already running instead of the build under test.
export BREAKER_PORT="${BREAKER_BVT_PORT:-$("$WORK/venv/bin/python" - <<'PORT'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PORT
)}"
echo "port: $BREAKER_PORT"
export BREAKER_HOST="127.0.0.1"
export BREAKER_SUBSTRATE="${BREAKER_SUBSTRATE:-mock}"   # no substrate in CI
export BREAKER_SUBSTRATES="$SRC/config/substrates.yaml"
# The wheel carries the package, not the repo's config files; point the dataset
# registry at the pristine copy the same way the substrate registry is pointed.
export BREAKER_DATASETS="$SRC/config/datasets.yaml"
# A decide is on throughline's PRIVILEGED write surface and needs an
# authenticated caller. The MOCK gate enforces that rule too, on purpose — a
# mock easier to satisfy than the real thing is how integration surprises are
# manufactured — so a breaker booted to answer a decide needs a token whichever
# substrate is behind it. The orchestrator's scripts/up mints one and exports
# it to every child; this harness is the boot path for CI, so it does the same.
# Overridable, like every other setting here.
export THROUGHLINE_CALLER_TOKEN="${THROUGHLINE_CALLER_TOKEN:-bvt-caller-token}"
# POST /fixture/run (scripts/smoke.py's demo-path check) is OFF by default in
# the product — a review found it generating a continuous, unbounded backlog
# of synthetic proposals with nobody having asked for it (see breaker/app.py,
# FIXTURE_DRIVE_ENV). The BVT is exactly the deliberate, operator-run
# exercise that setting exists for, against a throwaway instance on a
# throwaway port that this script tears down on exit — so it opts in here,
# explicitly, rather than the product defaulting to on.
export BREAKER_ENABLE_FIXTURE_DRIVE="${BREAKER_ENABLE_FIXTURE_DRIVE:-1}"
"$WORK/venv/bin/python" -m breaker >"$WORK/server.log" 2>&1 &
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
"$WORK/venv/bin/breaker" --url "http://127.0.0.1:$BREAKER_PORT" substrates >/dev/null
"$WORK/venv/bin/breaker" --url "http://127.0.0.1:$BREAKER_PORT" dataset list >/dev/null
"$WORK/venv/bin/breaker" dataset validate --path "$SRC/config/datasets.yaml" >/dev/null
"$WORK/venv/bin/breaker" --url "http://127.0.0.1:$BREAKER_PORT" stream --ticks 40 --quiet
echo "BVT OK"
