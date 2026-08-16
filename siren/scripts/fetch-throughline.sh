#!/usr/bin/env bash
# Fetch the throughline substrate SOURCE into $1 (default /tmp/throughline),
# so siren can `import throughline` — the dataset registry loader
# (throughline/datasets.py) is imported, not reimplemented here.
#
# This is docket's scripts/fetch-substrate.sh, reused rather than reinvented:
# same trust-discovery order, same CI_JOB_TOKEN clone, same refusal to paper
# over a missing CA by disabling verification. siren (project 1057) is already
# on throughline's job-token allowlist, so the clone is authorised.
#
# Two additions over docket's copy:
#   * a local sibling checkout wins if there is one (THROUGHLINE_SRC, or
#     ../throughline) — no network for a developer;
#   * the ref is checked for throughline/datasets_api.py, and falls back to the
#     branch that carries it, LOUDLY, if main does not yet. See the note at the
#     bottom of this file.
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
REF="${THROUGHLINE_REF:-main}"
#: The branch carrying the dataset registry until throughline!16 lands on main.
PENDING_REF="${THROUGHLINE_PENDING_REF:-feat/dataset-registry}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIBLING="${THROUGHLINE_SRC:-$ROOT/../throughline}"

if [[ -f "$SIBLING/throughline/datasets_api.py" ]]; then
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

echo "== cloning throughline ($REF) =="
rm -rf "$DEST"
if ! git clone --quiet "$URL" "$DEST" 2>/tmp/clone-err.log; then
  echo "  HTTPS clone failed:" >&2
  sed 's/[0-9a-zA-Z_-]\{20,\}/<redacted>/g' /tmp/clone-err.log >&2

  if [[ -n "${SUBSTRATE_SSH_URL:-}" ]]; then
    echo "== retrying over SSH =="
    git clone --quiet "$SUBSTRATE_SSH_URL" "$DEST"
  else
    cat >&2 <<'MSG'

FAIL: could not fetch throughline.

The job image does not trust the internal CA that signs git.nemotron.example.com, and no
CA file was found at any of the paths probed above. Fix the trust (mount the CA
into the job image, or set CI_SERVER_TLS_CA_FILE) rather than disabling
certificate verification — a dependency fetched over an unverified channel is
weaker evidence, not stronger.
MSG
    exit 1
  fi
fi

git -C "$DEST" checkout --quiet "$REF"

# The dataset registry loader is the whole reason siren needs throughline. If
# the requested ref does not carry it, say so and fall back to the branch that
# does — visibly, in the job log, so nobody mistakes the pending branch for
# main. Delete this block once throughline!16 is merged.
if [[ ! -f "$DEST/throughline/datasets_api.py" ]]; then
  echo "  NOTE: throughline@$REF has no throughline/datasets_api.py yet" >&2
  echo "  falling back to the PENDING branch $PENDING_REF (throughline!16)" >&2
  git -C "$DEST" checkout --quiet "$PENDING_REF"
  if [[ ! -f "$DEST/throughline/datasets_api.py" ]]; then
    echo "FAIL: neither $REF nor $PENDING_REF carries throughline/datasets_api.py" >&2
    exit 1
  fi
fi

echo "  throughline at $(git -C "$DEST" rev-parse --short HEAD) ($(git -C "$DEST" rev-parse --abbrev-ref HEAD))"
