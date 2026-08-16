#!/usr/bin/env python3
"""Exercise every contract path against a live breaker. Used by the BVT."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get(
    "BREAKER_URL", f"http://127.0.0.1:{os.environ.get('BREAKER_PORT', '8602')}"
)
SUBJECT = "oidc|bvt@nvidia-demo.example"


def request(method: str, path: str, body=None, expect=(200,)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload, status = response.read().decode(), response.status
    except urllib.error.HTTPError as exc:
        payload, status = exc.read().decode(), exc.code
    if status not in expect:
        raise SystemExit(f"FAIL {method} {path} -> {status} (expected {expect})\n{payload}")
    print(f"ok   {method} {path} -> {status}")
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


def wait_for_boot(seconds: int = 45) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise SystemExit(f"breaker never came up at {BASE}")


def main() -> None:
    wait_for_boot()
    request("GET", "/docs")
    request("GET", "/openapi.json")
    request("GET", "/healthz")

    request("POST", "/signals/telemetry", {
        "id": "sig-bvt", "class": "microgrid.telemetry",
        "source": "breaker.microgrid.feeder-7", "real_or_synthetic": "synthetic",
    }, expect=(201,))

    run = request("POST", "/fixture/run")
    proposals = run["proposals"]
    assert proposals, "the fixture produced no dispatch proposal"
    proposal = proposals[0]
    assert proposal["status"] == "waiting_at_gate", f"dispatch was not held: {proposal}"
    assert "→ DIVERGENCE" in proposal["evidence"], proposal["evidence"]

    fetched = request("GET", f"/proposals/{proposal['id']}")
    assert fetched["status"] == "waiting_at_gate"

    # A decision without a subject must be refused before anything dispatches.
    request("POST", f"/proposals/{proposal['id']}/decide",
            {"decision": "approve"}, expect=(422,))
    assert request("GET", "/dispatches")["dispatches"] == []

    released = request("POST", f"/proposals/{proposal['id']}/decide", {
        "decision": "approve", "decided_by": SUBJECT, "rationale": "bvt"})
    assert released["status"] == "approved", released
    assert released["execution_count"] == 1, released

    walk = request("GET", f"/proposals/{proposal['id']}/walk")
    assert walk["hop_count"] == 3, walk

    # cuOpt is registered, not running, and must refuse rather than pretend.
    request("POST", "/substrates/cuopt", expect=(409,))
    substrates = request("GET", "/substrates")
    assert substrates["active"] == "rule", substrates

    request("GET", "/evidence/battery_4")
    request("GET", "/telemetry/fixture?ticks=40")
    request("GET", "/telemetry/series?unit=battery_4")
    request("GET", "/abstentions")
    request("GET", "/proposals")
    request("GET", "/substrates/rule/health")

    # Discovery: the registry has to be loaded in the BUILD, not just in the
    # dev shell, and every entry it serves has to carry its licence.
    registry = request("GET", "/datasets")
    assert registry["component"] == "breaker", registry
    assert registry["count"] == len(registry["datasets"]) >= 1, registry
    for entry in registry["datasets"]:
        assert entry["licence"], f"{entry['id']} shipped with no licence"
        assert entry["provenance"], f"{entry['id']} shipped with no provenance"
        if entry["mode"] == "fixture":
            assert entry["real_or_synthetic"] == "synthetic", entry
    request("GET", f"/datasets/{registry['datasets'][0]['id']}")
    request("GET", "/datasets/not-a-dataset", expect=(404,))
    print(f"contract paths OK; proposal {proposal['id']} held then released once")


if __name__ == "__main__":
    sys.exit(main())
