#!/usr/bin/env bash
# Fetch the throughline substrate source into $1 (default /tmp/throughline).
#
# git.nemotron.example.com presents an internally-signed certificate. The runner's helper
# image trusts it; a plain python:*-slim job image does not. This tries the
# legitimate trust paths in order and reports which one worked, so a failure
# names the missing CA rather than being papered over by disabling TLS
# verification.
#
# ORDERING NOTE — why this script exists at all.
#
# It deliberately runs NO pip. It only produces a local, installable copy of
# the substrate; the CALLER installs it from that PATH, and does so BEFORE
# resolving its own pyproject. Once `throughline>=0.2` is already satisfied,
# pip never asks a package index for the name.
#
# That ordering is load-bearing, and it stays load-bearing no matter which
# index is configured. `throughline` is not published to the fleet's Nexus
# hosted repo, and `pypi-all` is a GROUP that includes the public PyPI proxy,
# so the name remains resolvable from public PyPI — as an unrelated project of
# the same name. That is exactly how a clean venv once came up green with no
# `throughline.datasets_api` and the service died at import. Pointing pip at an
# internal mirror does NOT close that; only installing the real thing from this
# path first does. Do not "simplify" the caller into an index lookup.
# See nemo-nvidia-demo-system/docs/PACKAGE-INDEX.md.
set -euo pipefail

DEST="${1:-/tmp/throughline}"
REPO_PATH="nvidia-hackathon/nemo-nvidia-demo/throughline.git"
HOST="${CI_SERVER_HOST:-git.nemotron.example.com}"

# Which throughline to build against. Defaults to the substrate's main.
#
# SUBSTRATE_REF exists so docket can integrate against a throughline branch
# whose work docket already depends on but which has not merged yet. That is a
# deliberate, temporary coupling and it is loud: the script prints the ref it
# actually got, and if the pinned ref has gone (typically because it merged and
# the source branch was removed) it says so and falls back to the default
# branch rather than failing the pipeline over a race. It never silently
# substitutes one for the other.
SUBSTRATE_REF="${SUBSTRATE_REF:-}"
DEFAULT_REF="${CI_DEFAULT_BRANCH:-main}"

# A checked-out sibling wins, and no network is touched. This is what siren's
# copy of this script already does, and it is what lets the federation's
# `scripts/up` provision docket from the working tree the operator is actually
# booting — rather than cloning a second, possibly different throughline to
# stand behind it. CI has no sibling checkout and takes the clone path below,
# unchanged. An explicit SUBSTRATE_REF pin always wins over the sibling: a pin
# is a deliberate statement about which throughline to build against.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIBLING="${THROUGHLINE_SRC:-$ROOT/../throughline}"
if [[ -z "$SUBSTRATE_REF" && -f "$SIBLING/throughline/datasets_api.py" ]]; then
  echo "== throughline from the sibling checkout $SIBLING =="
  rm -rf "$DEST"
  python3 - "$SIBLING" "$DEST" <<'COPY'
import shutil
import sys

root, dest = sys.argv[1], sys.argv[2]
EXCLUDED = {".git", ".venv", "venv", "build", "dist", "data", "__pycache__",
            ".pytest_cache", "htmlcov"}
shutil.copytree(root, dest,
                ignore=lambda d, names: [n for n in names
                                         if n in EXCLUDED or n.endswith(".egg-info")])
COPY
  exit 0
fi

echo "== substrate fetch: trust discovery =="
for candidate in \
  "${CI_SERVER_TLS_CA_FILE:-}" \
  /usr/local/share/ca-certificates/internal-ca.crt \
  /etc/gitlab-runner/certs/ca.crt \
  /etc/ssl/certs/internal-ca.crt
do
  [[ -n "$candidate" && -f "$candidate" ]] && echo "  found CA: $candidate"
done
echo "  CI_SERVER_TLS_CA_FILE=${CI_SERVER_TLS_CA_FILE:-<unset>}"

install_ca() {
  local src="$1"
  command -v update-ca-certificates >/dev/null 2>&1 || return 1
  cp "$src" /usr/local/share/ca-certificates/substrate-ca.crt 2>/dev/null || return 1
  update-ca-certificates >/dev/null 2>&1 || return 1
  echo "  installed CA from $src into the system trust store"
}

for candidate in \
  "${CI_SERVER_TLS_CA_FILE:-}" \
  /etc/gitlab-runner/certs/ca.crt \
  /usr/local/share/ca-certificates/internal-ca.crt
do
  if [[ -n "$candidate" && -f "$candidate" ]]; then
    install_ca "$candidate" && break
  fi
done

# Authenticated URL: CI_JOB_TOKEN is scoped to this pipeline and needs no
# secret of ours. Falls back to the plain URL when run outside CI.
if [[ -n "${CI_JOB_TOKEN:-}" ]]; then
  URL="https://gitlab-ci-token:${CI_JOB_TOKEN}@${HOST}/${REPO_PATH}"
else
  URL="https://${HOST}/${REPO_PATH}"
fi

report_ref() {
  local ref
  ref="$(git -C "$DEST" rev-parse --short HEAD)"
  echo "  substrate is $(git -C "$DEST" rev-parse --abbrev-ref HEAD) @ $ref"
  git -C "$DEST" log -1 --pretty="  %s" | cat
}

clone_ref() { # $1 = ref or empty for the remote default
  if [[ -n "$1" ]]; then
    git clone --quiet --depth 1 --branch "$1" "$URL" "$DEST" 2>/tmp/clone-err.log
  else
    git clone --quiet --depth 1 "$URL" "$DEST" 2>/tmp/clone-err.log
  fi
}

echo "== cloning throughline =="
if [[ -n "$SUBSTRATE_REF" ]]; then
  echo "  SUBSTRATE_REF is pinned to '$SUBSTRATE_REF'"
  if clone_ref "$SUBSTRATE_REF"; then
    echo "  cloned over HTTPS with a trusted certificate"
    report_ref
    exit 0
  fi
  echo "  pinned ref '$SUBSTRATE_REF' could not be cloned; it has probably" >&2
  echo "  merged and been removed. Falling back to '$DEFAULT_REF' AND SAYING SO." >&2
  rm -rf "$DEST"
fi

if clone_ref ""; then
  echo "  cloned over HTTPS with a trusted certificate"
  report_ref
  exit 0
fi

echo "  HTTPS clone failed:" >&2
sed 's/[0-9a-zA-Z_-]\{20,\}/<redacted>/g' /tmp/clone-err.log >&2

# SSH, if a deploy key is configured for the runner.
if [[ -n "${SUBSTRATE_SSH_URL:-}" ]]; then
  echo "== retrying over SSH =="
  git clone --quiet --depth 1 "$SUBSTRATE_SSH_URL" "$DEST" && {
    echo "  cloned over SSH"; exit 0; }
fi

cat >&2 <<'MSG'

FAIL: could not fetch throughline.

The job image does not trust the internal CA that signs git.nemotron.example.com, and no
CA file was found at any of the paths probed above. Fix the trust (mount the CA
into the job image, or set CI_SERVER_TLS_CA_FILE) rather than disabling
certificate verification — int+ is a claim about a REAL substrate, and a
pipeline that reached it over an unverified channel is weaker evidence, not
stronger.
MSG
exit 1
