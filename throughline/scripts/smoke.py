#!/usr/bin/env python3
"""Exercise every path in contracts/openapi.yaml against a live service.

Used by the BVT step. Fails loudly: a path that does not respond is a broken
build, not a warning.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get(
    "THROUGHLINE_URL", f"http://127.0.0.1:{os.environ.get('THROUGHLINE_PORT', '8600')}"
)


#: How a local caller authenticates to the privileged half of the write
#: surface. The substrate mints this file at boot with mode 0600; being able
#: to read it is the credential.
def caller_token() -> str:
    from_env = os.environ.get("THROUGHLINE_CALLER_TOKEN")
    if from_env:
        return from_env
    path = os.path.join(os.environ.get("THROUGHLINE_DATA_DIR", "data"), "caller-token")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


#: What a decision must carry to release an irreversible effect: an auth mode
#: the substrate accepts, naming the authority that vouched for the subject.
ATTESTED = {"issuer": "https://git.nemotron.example.com", "auth_mode": "oidc"}


def request(method: str, path: str, body=None, expect=(200,), token: bool = True):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token and caller_token():
        headers["X-Throughline-Caller-Token"] = caller_token()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = response.read().decode()
            status = response.status
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
    raise SystemExit(f"service never came up at {BASE}")


def main() -> None:
    wait_for_boot()
    request("GET", "/docs")
    request("GET", "/openapi.json")
    request("POST", "/signals", {
        "id": "sig-bvt", "class": "bvt.signal", "source": "bvt",
        "real_or_synthetic": "synthetic",
    }, expect=(201,))

    effect = request("POST", "/effects", {
        "id": "eff-bvt", "reversibility": "irreversible", "status": "proposed",
        "effect_type": "payment.release", "signal_id": "sig-bvt",
        "description": "bvt release",
    }, expect=(202,))
    assert effect["status"] == "queued", f"irreversible effect was not held: {effect}"

    request("GET", f"/effects/{effect['id']}")

    request("POST", "/judgments", {
        "id": "jud-bvt", "signal_id": "sig-bvt", "verdict": "escalate",
        "produced_by": "bvt",
    }, expect=(201,))
    walk = request("GET", f"/effects/{effect['id']}/walk")
    assert walk["all_hops_verified"] is True, f"a hop failed verification: {walk}"
    assert walk["hop_count"] <= 3, f"walk took more than three hops: {walk}"

    verify = request("GET", "/ledger/verify")
    assert verify["valid"] is True, f"ledger did not verify: {verify}"

    approvals = request("GET", "/approvals")["approvals"]
    pending = [a for a in approvals if a["state"] == "pending"]
    assert pending, "no approval was queued for the irreversible effect"
    # A decision that does not declare a role is refused, and the effect stays
    # held — omitting this key used to be enough to execute it.
    request("POST", f"/approvals/{pending[0]['id']}/decide",
            {"decision": "approve", "decided_by": "agent:bvt"}, expect=(403,))
    still_held = request("GET", f"/effects/{effect['id']}")
    assert still_held["status"] == "queued", f"refused decision executed it: {still_held}"

    # An UNATTESTED decision is refused even from an authenticated caller: the
    # substrate will not release an irreversible effect on a decision no
    # authority vouched for. The effect must still be held afterwards.
    request("POST", f"/approvals/{pending[0]['id']}/decide",
            {"decision": "approve", "decided_by": "attacker@example.com",
             "caller_role": "human"}, expect=(403,))
    still_held = request("GET", f"/effects/{effect['id']}")
    assert still_held["status"] == "queued", f"unattested decision executed it: {still_held}"

    # ...and an UNAUTHENTICATED caller is refused before the gate is reached,
    # however well-formed its attestation looks.
    request("POST", f"/approvals/{pending[0]['id']}/decide",
            {"decision": "approve", "decided_by": "mallory@example.com",
             "caller_role": "human", **ATTESTED}, expect=(403,), token=False)
    still_held = request("GET", f"/effects/{effect['id']}")
    assert still_held["status"] == "queued", f"unauthenticated decide executed it: {still_held}"

    # An authz effect is part of the permission graph, so it needs a caller too.
    request("POST", "/effects", {"effect_type": "authz.grant", "id": "eff-bvt-authz"},
            expect=(403,), token=False)

    request("POST", f"/approvals/{pending[0]['id']}/decide", {
        "decision": "approve", "decided_by": "oidc|bvt", "caller_role": "human",
        **ATTESTED, "rationale": "bvt",
    })

    executed = request("GET", f"/effects/{effect['id']}")
    assert executed["status"] == "executed", f"released effect did not execute: {executed}"

    # An approval without a subject must be refused at the schema level.
    request("POST", f"/approvals/{pending[0]['id']}/decide",
            {"decision": "approve", "caller_role": "human"}, expect=(422,))

    # A reload from outside the allowlisted config directory is refused, and
    # the running registry is untouched.
    before = request("GET", "/config")["config"]
    request("POST", "/config/reload", {"path": "/tmp/not-allowlisted.yaml"},
            expect=(403,))
    assert request("GET", "/config")["config"] == before, "refused reload still applied"

    final = request("GET", "/ledger/verify")
    assert final["valid"] is True, f"ledger broke during the smoke: {final}"
    print(f"contract paths OK; chain_length={final['chain_length']}")


if __name__ == "__main__":
    sys.exit(main())
