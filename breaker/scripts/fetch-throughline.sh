#!/usr/bin/env bash
# Produce an installable throughline and print its path on stdout.
#
# This is the mechanism scripts/with-throughline.sh has always used, lifted out
# so it has more than one consumer. breaker now DEPENDS on throughline at import
# time — breaker/app.py mounts throughline's dataset discovery surface rather
# than reimplementing it — so the dependency has to be satisfiable in every job,
# not just the integration one. throughline is not on PyPI, so there is exactly
# one supported way to get it, and this is it:
#
#   * a wheel already sitting in ./dist (a CI artifact, or a local build);
#   * ../throughline, if the sibling is checked out beside us;
#   * otherwise the wheel throughline's own `package` job publishes, fetched
#     through the GitLab API with CI_JOB_TOKEN. The job image has no git, and a
#     job token may download another project's ARTIFACTS but not its repository
#     archive — the archive endpoint answers 404 for a job token — which is why
#     throughline publishes that wheel in the first place.
#
# Usage:  SRC="$(scripts/fetch-throughline.sh [DEST_DIR])"
# Narration goes to stderr so stdout is only ever the path.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$(mktemp -d)}"
mkdir -p "$DEST"
SIBLING="${THROUGHLINE_SRC:-$ROOT/../throughline}"
API="${CI_API_V4_URL:-https://git.nemotron.example.com/api/v4}"
PROJECT_ID="${THROUGHLINE_PROJECT_ID:-1054}"
# Refs to try, in order. `main` first, always — the fallback below exists
# only until throughline!16 (its own dataset registry) merges, and it
# disappears on its own the moment main carries the module, because main is
# tried first and accepted the moment it satisfies the requirement.
REFS="${THROUGHLINE_REFS:-${THROUGHLINE_REF:-main feat/dataset-registry}}"

#: breaker imports this module. A throughline that does not provide it is
#: the wrong throughline, and saying so here beats a ModuleNotFoundError
#: three steps later with no hint of which artifact was installed.
REQUIRED_MODULE="throughline/datasets_api.py"

# One check, applied to whichever source wins: does this throughline actually
# carry the module breaker imports? A stale wheel in ./dist or a sibling on the
# wrong branch is a real failure, and it is cheaper to name it here.
verify_and_emit() {
  python3 - "$1" "$REQUIRED_MODULE" <<'VERIFY' >&2
import os
import sys
import zipfile

src, required = sys.argv[1:3]
if os.path.isdir(src):
    ok = os.path.exists(os.path.join(src, required))
else:
    with zipfile.ZipFile(src) as archive:
        ok = required in archive.namelist()
if not ok:
    raise SystemExit(
        f"{src} does not provide {required}. breaker imports it "
        f"(breaker/app.py), so installing this would fail at import time.")
VERIFY
  echo "$1"
}

WHEEL="$(ls "$ROOT"/dist/throughline-*.whl 2>/dev/null | head -1 || true)"

if [ -n "$WHEEL" ]; then
  echo "== throughline from wheel artifact: $WHEEL ==" >&2
  verify_and_emit "$WHEEL"
  exit 0
fi

if [ -d "$SIBLING/throughline" ]; then
  echo "== throughline from $SIBLING ==" >&2
  SRC="$DEST/throughline-src"
  rm -rf "$SRC"
  python3 - "$SIBLING" "$SRC" <<'COPY' >&2
import shutil
import sys

root, dest = sys.argv[1], sys.argv[2]
EXCLUDED = {".git", ".venv", "venv", "build", "dist", "data", "__pycache__",
            ".pytest_cache", "htmlcov"}
shutil.copytree(root, dest,
                ignore=lambda d, names: [n for n in names
                                         if n in EXCLUDED or n.endswith(".egg-info")])
COPY
  verify_and_emit "$SRC"
  exit 0
fi

echo "== throughline from $API (refs: $REFS) ==" >&2
python3 - "$API" "$PROJECT_ID" "$DEST" "$REQUIRED_MODULE" $REFS <<'FETCH' >&2
import glob
import os
import shutil
import ssl
import sys
import urllib.request
import zipfile

api, project_id, work, required = sys.argv[1:5]
refs = sys.argv[5:]

# The server presents leaf + MyPrivateCA intermediate and expects the client to
# hold the ROOT, so one named file is not enough — take every certificate the
# image was given. CI_SERVER_TLS_CA_FILE is the one that actually chains here.
context = ssl.create_default_context()
candidates = [
    os.environ.get("SSL_CERT_FILE"),
    os.environ.get("REQUESTS_CA_BUNDLE"),
    os.environ.get("CURL_CA_BUNDLE"),
    os.environ.get("CI_SERVER_TLS_CA_FILE"),
    "/etc/ssl/certs/ca-certificates.crt",
]
candidates += sorted(glob.glob("/usr/local/share/ca-certificates/*.crt"))
candidates += sorted(glob.glob("/etc/gitlab-runner/certs/*.crt"))
for candidate in candidates:
    if not candidate or not os.path.exists(candidate):
        continue
    try:
        if os.path.isdir(candidate):
            context.load_verify_locations(capath=candidate)
        else:
            context.load_verify_locations(cafile=candidate)
    except OSError as exc:
        print(f"ignoring unusable CA path {candidate}: {exc}")


def fetch(ref):
    """Download the `package` artifact for one ref; return the wheel path."""
    # The CI job token may download another project's ARTIFACTS (with that
    # project allowing it) but not its repository archive — the archive endpoint
    # answers 404 for a job token. throughline publishes an installable wheel
    # from its pipelines for exactly this purpose.
    url = f"{api}/projects/{project_id}/jobs/artifacts/{ref}/download?job=package"
    request = urllib.request.Request(url)
    token = os.environ.get("CI_JOB_TOKEN")
    if token:
        request.add_header("JOB-TOKEN", token)
    elif os.environ.get("GITLAB_TOKEN"):
        request.add_header("PRIVATE-TOKEN", os.environ["GITLAB_TOKEN"])

    staging = os.path.join(work, "candidate")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    archive = os.path.join(staging, "artifacts.zip")
    with urllib.request.urlopen(request, timeout=120, context=context) as response, \
            open(archive, "wb") as out:
        out.write(response.read())
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(staging)

    wheels = glob.glob(os.path.join(staging, "dist", "throughline-*.whl"))
    if not wheels:
        raise SystemExit(f"no throughline wheel in the {ref} artifact")
    return wheels[0]


def provides(wheel, module):
    with zipfile.ZipFile(wheel) as archive:
        return module in archive.namelist()


chosen = None
for ref in refs:
    try:
        wheel = fetch(ref)
    except Exception as exc:                      # noqa: BLE001 — report and try next
        print(f"  {ref}: no usable package artifact ({exc})")
        continue
    if not provides(wheel, required):
        # Not an error worth stopping on: the earlier refs in the list are the
        # preferred ones, and a ref that simply has not merged the module yet is
        # expected to fail this check until it does.
        print(f"  {ref}: {os.path.basename(wheel)} does not provide {required}")
        continue
    print(f"  {ref}: {os.path.basename(wheel)} provides {required} — using it")
    chosen = wheel
    break

if chosen is None:
    raise SystemExit(
        f"no throughline wheel from {refs} provides {required}. breaker imports "
        f"it (breaker/app.py), so this is a real missing dependency, not a "
        f"warning: publish it from throughline or point THROUGHLINE_REFS at a "
        f"ref whose `package` job has.")

final = os.path.join(work, "dist")
os.makedirs(final, exist_ok=True)
shutil.copy(chosen, final)
print(f"fetched {os.path.basename(chosen)}")
FETCH

verify_and_emit "$(ls "$DEST"/dist/throughline-*.whl | head -1)"
