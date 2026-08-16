#!/usr/bin/env bash
# Block until throughline answers, or fail loudly. An int+ criterion that
# "passed" because the substrate was never up is worse than a red pipeline.
set -euo pipefail
URL="${THROUGHLINE_URL:-http://127.0.0.1:8600}"
for _ in $(seq 1 "${SUBSTRATE_WAIT_SECONDS:-60}"); do
  if curl -fsS -m 2 "$URL/docs" >/dev/null 2>&1; then
    echo "substrate up at $URL"
    exit 0
  fi
  sleep 1
done
echo "throughline never came up at $URL — int+ cannot be claimed" >&2
[[ -f /tmp/throughline.log ]] && tail -40 /tmp/throughline.log >&2
exit 1
