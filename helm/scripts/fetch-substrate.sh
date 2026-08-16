#!/usr/bin/env bash
# Fetch the throughline substrate source into $1 (default /tmp/throughline).
#
# git.nemotron.example.com presents an internally-signed certificate. The runner's helper
# image trusts it; a plain python:*-slim job image does not. This tries the
# legitimate trust paths in order and reports which one worked, so a failure
# names the missing CA rather than being papered over by disabling TLS
# verification.
set -euo pipefail

DEST="${1:-/tmp/throughline}"
REPO_PATH="nvidia-hackathon/nemo-nvidia-demo/throughline.git"
HOST="${CI_SERVER_HOST:-git.nemotron.example.com}"

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

echo "== cloning throughline =="
if git clone --quiet --depth 1 "$URL" "$DEST" 2>/tmp/clone-err.log; then
  echo "  cloned over HTTPS with a trusted certificate"
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
