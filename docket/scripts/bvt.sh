#!/usr/bin/env bash
# Build Verification Test.
#
# Proves the BUILD, not this session's environment: a pristine copy of the
# source, a clean venv, an install from pyproject, a real uvicorn boot on the
# real port, and every path in contracts/openapi.yaml answered over HTTP.
#
# Runs entirely offline against data/permits.json. No key, no substrate needed.
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
PORT="${DOCKET_BVT_PORT:-8651}"
trap 'rc=$?; [[ -n "${SVC_PID:-}" ]] && kill "$SVC_PID" 2>/dev/null || true; rm -rf "$WORK"; exit $rc' EXIT

say() { printf "\n\033[1m== %s\033[0m\n" "$*"; }
fail() { printf "\033[31mBVT FAIL: %s\033[0m\n" "$*" >&2; exit 1; }

say "pristine source copy"
# git archive takes only tracked content: an untracked stray file cannot make
# the BVT pass for a build that would fail from a fresh clone.
git -C "$REPO_ROOT" archive --format=tar HEAD | (mkdir -p "$WORK/src" && tar -x -C "$WORK/src")
[[ -f "$WORK/src/pyproject.toml" ]] || fail "pyproject.toml not in tracked source"
[[ -f "$WORK/src/data/permits.json" ]] || fail "permit snapshot not in tracked source"

say "substrate: throughline, which pyproject now declares as a dependency"
# docket imports throughline's dataset loader and its /datasets router.
# throughline is not on PyPI, so the clean venv gets it from source using the
# same fetch-substrate.sh the int+ stage uses. Point DOCKET_SUBSTRATE_SRC at a
# local checkout to skip the clone (useful offline); otherwise it is cloned.
SUBSTRATE="${DOCKET_SUBSTRATE_SRC:-}"
if [[ -z "$SUBSTRATE" ]]; then
  SUBSTRATE="$WORK/throughline"
  bash "$REPO_ROOT/scripts/fetch-substrate.sh" "$SUBSTRATE" \
    || fail "could not fetch the throughline substrate"
fi
[[ -f "$SUBSTRATE/pyproject.toml" ]] || fail "no throughline source at $SUBSTRATE"
echo "  substrate source: $SUBSTRATE"

say "clean venv + install from pyproject"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --upgrade pip
"$WORK/venv/bin/pip" install --quiet "$SUBSTRATE"
"$WORK/venv/bin/pip" install --quiet "$WORK/src"

say "boot the service on :$PORT"
(
  cd "$WORK/src"
  MOCK_JUDGMENT=1 MOCK_THROUGHLINE=1 DOCKET_PORT="$PORT" \
    "$WORK/venv/bin/python" -m docket
) >"$WORK/boot.log" 2>&1 &
SVC_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  kill -0 "$SVC_PID" 2>/dev/null || { cat "$WORK/boot.log"; fail "service died on boot"; }
  sleep 0.5
done
curl -fsS -m 5 "http://127.0.0.1:$PORT/healthz" >/dev/null || {
  cat "$WORK/boot.log"; fail "service never became healthy"
}

check() { # method path expected-status label
  local code
  code="$(curl -s -o "$WORK/body.json" -w '%{http_code}' -m 15 -X "$1" \
          ${4:+-H 'Content-Type: application/json' -d "$4"} \
          "http://127.0.0.1:$PORT$2")"
  [[ "$code" == "$3" ]] || fail "$1 $2 -> $code (want $3)"
  printf "  ok  %-6s %-34s %s\n" "$1" "$2" "$code"
}

say "/docs and the contract surface"
check GET /docs 200
check GET /openapi.json 200
check GET /healthz 200
check GET /permits?limit=3 200
check GET /permits/7110794-CN 200
check POST /signals/document 201 \
  '{"id":"sig-bvt-1","class":"permit.document","source":"socrata:76t5-zqzr","ingest_time":"2026-08-16T00:00:00Z","real_or_synthetic":"real"}'

say "the dataset registry, from a clean install"
check GET /datasets 200
check GET /datasets/docket.seattle-building-permits 200
check GET /datasets/nope 404
"$WORK/venv/bin/python" - "$PORT" <<'PY'
import json, sys, urllib.request
port = sys.argv[1]
body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/datasets"))
refusal = body["refusal"]
assert refusal is None, f"registry refused at boot: {refusal}"
assert body["count"] == len(body["datasets"]) > 0, "registry loaded empty"
for d in body["datasets"]:
    assert d["licence"], f"{d['id']} has no licence"
    assert d["provenance"], f"{d['id']} has no provenance"
    if d["mode"] == "cached":
        assert d["as_of"], f"{d['id']} is cached with no as-of time"
# The declared-but-unbuilt embedding index is listed as unavailable, never
# omitted and never dressed up as present.
assert "docket.retriever-embedding-index" in body["unavailable"]
one = json.load(urllib.request.urlopen(
    f"http://127.0.0.1:{port}/datasets/docket.retriever-embedding-index"))
assert one["availability"] == "unavailable" and one["record_count"] is None
print(f"  ok  {body['count']} datasets, all with licence + provenance; "
      f"{len(body['unavailable'])} declared unavailable, none faked")
PY

say "every declared contract path is served by the running app"
"$WORK/venv/bin/python" - "$PORT" "$WORK/src/contracts/openapi.yaml" <<'PY'
import json, sys, urllib.request
port, spec_path = sys.argv[1], sys.argv[2]
try:
    import yaml
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "PyYAML"])
    import yaml
declared = set(yaml.safe_load(open(spec_path))["paths"])
served = set(json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json"))["paths"])
missing = sorted(declared - served)
if missing:
    print(f"BVT FAIL: contract paths not served: {missing}", file=sys.stderr)
    sys.exit(1)
print(f"  ok  {len(declared)} declared paths all served")
PY

say "the demo path, end to end, offline"
JUDGMENT_ID="$(curl -fsS -m 20 -X POST "http://127.0.0.1:$PORT/permits/7110794-CN/judge" \
  | "$WORK/venv/bin/python" -c 'import json,sys; j=json.load(sys.stdin)["judgment"]; assert j["abstained"] is False, "demo permit abstained"; assert j["quote"], "no verbatim quote"; print(j["id"])')"
echo "  ok  judged 7110794-CN -> $JUDGMENT_ID"

check POST "/judgments/$JUDGMENT_ID/route" 202
check GET "/judgments/$JUDGMENT_ID" 200

say "abstention path"
ABSTAIN_ID="$(curl -fsS -m 20 -X POST "http://127.0.0.1:$PORT/permits/7132584-CN/judge" \
  | "$WORK/venv/bin/python" -c 'import json,sys; j=json.load(sys.stdin)["judgment"]; assert j["abstained"] is True, "empty description did not abstain"; assert j["route_proposal"] is None; print(j["id"])')"
echo "  ok  abstained on 7132584-CN -> $ABSTAIN_ID"
check POST "/judgments/$ABSTAIN_ID/route" 409

say "uncited judgment never leaves the service"
curl -fsS -m 20 -X POST "http://127.0.0.1:$PORT/permits/7110791-EX/judge" \
  | "$WORK/venv/bin/python" -c 'import json,sys; j=json.load(sys.stdin)["judgment"]; assert j["abstained"] is True and j["quote"] is None, "uncited judgment escaped"; print("  ok  uncited judgment rejected ->", j["abstain_reason"][:60])'

printf "\n\033[32mBVT PASS\033[0m — clean install, real boot, contract surface served, demo path offline\n"
