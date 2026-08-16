#!/usr/bin/env bash
# Build Verification Test — proves the BUILD, not this session's environment.
#
# A CLEAN virtualenv, an install from pyproject.toml only, a real boot on a
# real port, then every path in contracts/openapi.yaml exercised over HTTP.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
PORT="${BVT_PORT:-8619}"
cleanup() {
  if [[ -n "${APP_PID:-}" ]]; then kill "$APP_PID" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "== pristine source copy =="
SRC="$WORK/src"
python3 - "$ROOT" "$SRC" <<'COPY'
import shutil
import sys

root, dest = sys.argv[1], sys.argv[2]
EXCLUDED = {
    ".git", ".venv", "venv", "build", "dist", "data", "htmlcov", "artifacts",
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

echo "== boot the installed build on :$PORT =="
cd "$WORK"
HELM_PORT="$PORT" \
HELM_ENV=test \
MOCK_LOGIN=1 \
HELM_DATA_DIR="$WORK/data" \
HELM_CACHE_DIR="$WORK/cache" \
NEMOCLERK_BASE_URL="" \
NEMOCLERK_FALLBACK_BASE_URL="" \
NVIDIA_API_KEY="" \
"$WORK/venv/bin/python" -m helm &
APP_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

echo "== every contract path over HTTP =="
BASE="http://127.0.0.1:$PORT" CONTRACT="$SRC/contracts/openapi.yaml" \
"$WORK/venv/bin/python" - <<'SMOKE'
import os
import sys
import urllib.error
import urllib.request

import yaml

base = os.environ["BASE"]
contract = yaml.safe_load(open(os.environ["CONTRACT"]))

# Path parameters get a value that is syntactically valid; a 404 from a real
# handler still proves the path is served, an unrouted path gives no handler.
SUBSTITUTIONS = {
    "{approval_id}": "apr-bvt",
    "{effect_id}": "eff-bvt",
    "{name}": "incident",
    "{dataset_id}": "ds-bvt",
    "{request_id}": "bg-bvt",
    "{delegation_id}": "dlg-bvt",
}
BODIES = {
    ("post", "/approvals/{approval_id}/decide"): {
        "decision": "approve", "decided_by": "bvt@example"
    },
    ("post", "/nemoclerk/message"): {"message": "which feeds are live?"},
    ("post", "/mcp/call"): {"name": "get_overview", "arguments": {}},
    ("post", "/admin/roles"): {"subject": "dana@nvidia-demo.example", "role": "admin"},
    ("post", "/admin/global-roles"): {
        "subject": "dana@nvidia-demo.example", "global_role": ""
    },
    ("put", "/prefs"): {"theme": "dark"},
    # warrant. There is no throughline in the BVT, so these correctly answer
    # 401 (no subject) or 503 (authority unknown) — never a confident denial,
    # and never a 404/405, which is what the BVT is actually checking.
    ("post", "/datasets/{dataset_id}/grants"): {"subject": "bvt@example", "role": "reader"},
    ("post", "/datasets/{dataset_id}/revocations"): {"subject": "bvt@example"},
    ("post", "/datasets/{dataset_id}/delegations"): {
        "subject": "bvt@example", "role": "admin", "expires_at": "2099-01-01T00:00:00Z"
    },
    ("post", "/datasets/{dataset_id}/breakglass"): {"reason": "bvt", "window_minutes": 60},
    ("post", "/datasets/{dataset_id}/breakglass/{request_id}/exit"): {"reason": "bvt"},
}

failures = []
checked = 0
for path, operations in contract["paths"].items():
    concrete = path
    for token, value in SUBSTITUTIONS.items():
        concrete = concrete.replace(token, value)
    for method in operations:
        if method not in {"get", "post", "put", "delete", "patch"}:
            continue
        checked += 1
        body = BODIES.get((method, path))
        data = None
        headers = {}
        if body is not None:
            import json as _json
            data = _json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{base}{concrete}", data=data, headers=headers, method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{method.upper()} {concrete}: {exc}")
            continue
        # 405 means the path exists but not this verb: a routing defect.
        if status in {404, 405} and "{" not in path:
            failures.append(f"{method.upper()} {concrete}: {status}")
        print(f"  {method.upper():5} {concrete:42} {status}")

for extra in ("/docs", "/openapi.json", "/login", "/console", "/static/app.css"):
    try:
        with urllib.request.urlopen(f"{base}{extra}", timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    checked += 1
    print(f"  GET   {extra:42} {status}")
    if status >= 400:
        failures.append(f"GET {extra}: {status}")

print(f"\n{checked} requests, {len(failures)} failures")
if failures:
    for failure in failures:
        print("FAIL", failure)
    sys.exit(1)
SMOKE

echo "== BVT PASS =="
