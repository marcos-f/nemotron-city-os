#!/usr/bin/env python3
"""int+ — helm against a REAL throughline, on its fixed port.

With ``HELM_REQUIRE_REAL_SUBSTRATE=1`` a missing substrate is a FAILURE, not
a skip: the job can never pass by quietly doing nothing.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from helm.app import create_app  # noqa: E402
from helm.config import load_settings  # noqa: E402

REQUIRE_REAL = os.environ.get("HELM_REQUIRE_REAL_SUBSTRATE", "0") == "1"
THROUGHLINE = os.environ.get("THROUGHLINE_URL", "http://127.0.0.1:8600")

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


def federation_probe() -> str | None:
    """``None`` when we can authenticate; otherwise why we cannot, in a sentence.

    Uses helm's OWN client, so this proves the thing under test — that helm
    presents the header the substrate demands — rather than proving curl can.
    The approval id is deliberately nonexistent: the substrate authenticates
    before it looks up the approval, so an authenticated caller gets 404 and
    the chain is untouched.
    """
    from helm.clients import Throughline, read_caller_token

    token = read_caller_token()
    client = Throughline("throughline", THROUGHLINE)
    reply = client.decide(
        approval_id="apr-int-preflight-does-not-exist",
        decision="approve",
        decided_by="preflight@helm.invalid",
        caller_role="human",
    )
    if reply.status == 404:
        return None
    if reply.unauthenticated:
        if not token.present:
            return (
                "no caller token was configured. "
                f"{token.detail or 'no token file and no environment variable'}"
            )
        return (
            f"the substrate rejected the token helm presented "
            f"(source: {token.source}). It is readable but not the value this "
            f"throughline validates against — a stale token will do this."
        )
    # Authenticated far enough to get a different answer entirely. Not our
    # problem to diagnose here; the checks below will describe it.
    return None


def main() -> int:
    try:
        health = httpx.get(f"{THROUGHLINE}/healthz", timeout=5)
        reachable = health.is_success
    except httpx.HTTPError as exc:
        reachable = False
        print(f"throughline unreachable: {exc}")

    if not reachable:
        if REQUIRE_REAL:
            print("FAIL: HELM_REQUIRE_REAL_SUBSTRATE=1 and no substrate is serving")
            return 1
        print("SKIP: no substrate; set HELM_REQUIRE_REAL_SUBSTRATE=1 to make this fatal")
        return 0

    # ------------------------------------------------- can we authenticate?
    #
    # ASKED FIRST, AND ANSWERED IN ITS OWN WORDS.
    #
    # throughline authenticates the caller on its privileged write surface, so
    # a run with no usable token fails three checks further down — "the refusal
    # is on the real ledger", "the refusal names helm's own principal" and
    # "human approval executes the effect" — none of which mention
    # authentication. That reads as a broken ledger and a broken gate. It cost
    # a reviewer a full investigation across a live federation before the
    # cause turned out to be an unexported environment variable.
    #
    # The whole point of this branch of work is that a refusal because we could
    # not authenticate is NOT a refusal by the gate. A harness that reports the
    # first as the second breaks the same rule the product is being held to.
    #
    # This is a LIVE probe, not an environment-variable check: it decides a
    # deliberately nonexistent approval, so the substrate authenticates the
    # caller and only then discovers there is nothing to decide. A 404 means we
    # are authenticated; nothing is written to the chain either way. That also
    # catches the case an env check cannot see — holding a token the substrate
    # no longer accepts, e.g. a stale <data-dir>/caller-token left by an
    # earlier boot.
    probe = federation_probe()
    if probe is not None:
        print(f"\nCANNOT AUTHENTICATE TO THE SUBSTRATE — {probe}")
        print(
            "\nThis is an AUTHENTICATION problem between this process and "
            "throughline.\nThe gate has not refused anything: it was never "
            "asked. No conclusion can be\ndrawn here about the ledger, the "
            "principal binding, or the approve path.\n\n"
            "Remedy: export THROUGHLINE_CALLER_TOKEN with the value the "
            "serving throughline\nvalidates against (scripts/up exports it to "
            "every child automatically), or run\nwhere that substrate's "
            "0600 <data-dir>/caller-token is readable."
        )
        return 1
    print("caller token: accepted by the substrate")


    os.environ.setdefault("HELM_ENV", "test")
    os.environ.setdefault("HELM_OFFLINE", "1")
    os.environ["HELM_DATA_DIR"] = str(ROOT / "artifacts" / "int-data")
    settings = load_settings()
    app = create_app(settings)
    client = TestClient(app)

    # Mint the session directly against this run's signet, the same way
    # ``tests/conftest.py::sign_in`` does. There is no product route that
    # mints a fixed identity any more — ``/auth/mock`` does not exist — so
    # this harness constructs the session instead of reaching one. It still
    # calls ``signet.issue(..., mock=True)`` rather than a plain
    # ``provider="github"`` session: a hand-rolled session with no issuer
    # and no mock flag comes back ``auth_mode: unverified``, a shape no
    # console can actually produce, and releasing an irreversible effect
    # requires an attestation on the substrate's allowlist that shape does
    # not carry. ``mock=True`` reproduces what a demo operator really holds:
    # ``auth_mode: mock``, no issuer, labelled MOCK LOGIN on every page.
    from helm.signet import SESSION_COOKIE

    token = app.state.signet.issue(
        "dana@nvidia-demo.example", "mock", "dana", mock=True
    )
    client.cookies.set(SESSION_COOKIE, token)

    print("\nINTEGRATION — helm against the real throughline\n" + "=" * 60)

    effect_id = f"eff-int-{int(time.time())}"
    signal_id = f"sig-int-{int(time.time())}"
    httpx.post(
        f"{THROUGHLINE}/signals",
        json={
            "id": signal_id,
            "class": "grid.telemetry",
            "source": "helm/int",
            "ingest_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "real_or_synthetic": "synthetic",
        },
        timeout=10,
    )
    proposed = httpx.post(
        f"{THROUGHLINE}/effects",
        json={
            "id": effect_id,
            "effect_type": "dispatch.load_shed",
            "reversibility": "irreversible",
            "status": "proposed",
            "description": "microgrid load-shed battery_4 (integration)",
            "signal_id": signal_id,
        },
        timeout=10,
    )
    check("effect queued by the real gate", proposed.is_success, str(proposed.status_code))
    approval_id = proposed.json().get("approval_id", "")

    overview = client.get("/overview").json()
    check(
        "helm reads the real overview",
        overview["substrate_online"] and overview["chain_length"] > 0,
        f"chain_length={overview['chain_length']}",
    )

    composed = client.get("/composed/state").json()
    check(
        "composed assembles on the real gate-hold",
        composed["assembled"] and composed["trigger"] == "gate-hold",
    )

    # This client holds dana's session, so it declares itself an agent — a
    # DEMOTION, which helm honours. What it may not do is pick the name: the
    # principal on the chain is helm's own, read back from the runtime it is
    # actually serving on. The obviously-false string below proves it.
    principal = client.get("/nemoclerk/runtime").json()["principal"]
    refused = client.post(
        "/mcp/call",
        json={
            "name": "approve_effect",
            "arguments": {"id": approval_id},
            "principal": "dana@nvidia-demo.example",
        },
    )
    check("agent refused by the real substrate", refused.status_code == 403)

    ledger = client.get("/ledger", params={"limit": 50}).json()
    refusals = [row for row in ledger["rows"] if row["kind"] == "approval.refused"]
    check("the refusal is on the real ledger", bool(refusals))
    # Scoped to THIS effect: an older row from another run must not stand in
    # for the one this check is about, in either direction.
    mine = [row for row in refusals if row["effect_id"] == effect_id]
    check(
        "the refusal names helm's own principal",
        bool(mine) and all(row["subject"] == principal for row in mine),
        f"principal={principal}",
    )
    check(
        "the name the caller supplied is nowhere on the chain",
        all(row["subject"] != "dana@nvidia-demo.example" for row in mine),
    )

    subjectless = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approve"}
    )
    check("subject-less decide is 422", subjectless.status_code == 422)

    approved = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "dana@nvidia-demo.example",
            "caller_role": "human",
        },
    )
    check("human approval executes the effect", approved.status_code == 200)

    walk = client.get(f"/walk/{effect_id}/json").json()
    check(
        "the chain walks back, hash-verified",
        bool(walk.get("hops")) and all(h["verified"] for h in walk["hops"]),
        f"{walk.get('hop_count')} hops",
    )

    verify = client.get("/ledger/verify").json()
    check("the real ledger still verifies", verify["valid"], f"{verify['chain_length']} entries")

    print("=" * 60)
    print(f"{checks - len(failures)}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
