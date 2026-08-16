"""test://signet/issuer-in-record — WHO approved, and WHO vouched for them.

An approval record that names only a subject is an assertion: the string
came from whichever console wrote it, and nobody else can check it. A record
that also names the ISSUER can be taken back to that issuer.

throughline does not verify the issuer and never claims to. What it does is
refuse to lose the distinction: an attested decision keeps its authority in
the hash-chained entry, and an unattested one is labelled ``unattested``
rather than sitting silently beside a verified one looking identical.
"""

from __future__ import annotations

IRREVERSIBLE = {
    "id": "eff-issuer",
    "reversibility": "irreversible",
    "status": "proposed",
    "effect_type": "payment.release",
    "description": "release $40k",
    "signal_id": "sig-issuer",
}


def _queue(client, effect_id: str = "eff-issuer") -> str:
    client.post("/signals", json={
        "id": "sig-issuer", "class": "demo.signal", "source": "test",
        "real_or_synthetic": "real",
    })
    payload = dict(IRREVERSIBLE, id=effect_id)
    client.post("/effects", json=payload)
    approvals = client.get("/approvals").json()["approvals"]
    return next(a["id"] for a in approvals if a["effect_id"] == effect_id)


def test_the_ledgered_approval_carries_subject_and_issuer(client):
    approval_id = _queue(client, "eff-issuer-live")
    decided = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "dana@nvidia-demo.example",
            "caller_role": "human",
            "issuer": "https://git.nemotron.example.com",
            "auth_mode": "oidc",
        },
    )
    assert decided.status_code == 200

    record = decided.json()["approval"]
    assert record["decided_by"] == "dana@nvidia-demo.example"
    assert record["issuer"] == "https://git.nemotron.example.com"
    assert record["auth_mode"] == "oidc"

    entries = client.get("/ledger").json()["entries"]
    entry = next(e for e in entries if e["type"] == "approval.decided")
    assert entry["body"]["decided_by"] == "dana@nvidia-demo.example"
    assert entry["body"]["issuer"] == "https://git.nemotron.example.com"
    assert entry["body"]["auth_mode"] == "oidc"

    # The whole point of putting it here rather than in a side table.
    assert client.get("/ledger/verify").json()["valid"] is True


def test_an_unattested_decision_is_labelled_not_omitted(client):
    """A decision with no issuer says so — on the REFUSAL, now.

    Labelling was the first half of this and it is still here: the entry says
    ``unattested`` rather than staying silent. The second half is that the
    entry is an ``approval.refused`` and the effect is still queued. A record
    that honestly described an executed payment as unattested preserved the
    audit and none of the restraint.
    """
    approval_id = _queue(client, "eff-issuer-mock")
    decided = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "dana@nvidia-demo.example",
            "caller_role": "human",
        },
    )
    assert decided.status_code == 403
    assert decided.json()["refusal_reason"] == (
        "unattested-decision-may-not-release-irreversible"
    )

    entry = next(
        e
        for e in client.get("/ledger").json()["entries"]
        if e["type"] == "approval.refused"
    )
    assert entry["body"]["issuer"] is None
    assert entry["body"]["auth_mode"] == "unattested"
    assert entry["body"]["decided_by"] == "dana@nvidia-demo.example"
    assert client.get("/effects/eff-issuer-mock").json()["status"] == "queued"


def test_a_reversible_hold_still_releases_without_an_attestation(client, substrate):
    """The label-only behaviour survives where it is safe: a reversible
    effect held by rule records ``unattested`` and executes, exactly as
    before. The refusal is scoped to the irreversible."""
    from throughline.config import Config, EffectRule

    substrate.config_store._current = Config(source="test", version=1, rules=[
        EffectRule("RULE-T", "review.queue", "reversible", auto_execute=False),
    ])
    client.post("/signals", json={
        "id": "sig-issuer-rev", "class": "demo.signal", "source": "test",
        "real_or_synthetic": "real",
    })
    client.post("/effects", json={
        "effect_type": "review.queue", "id": "eff-issuer-rev",
        "signal_id": "sig-issuer-rev", "description": "hold for review",
    })
    approval_id = client.get("/approvals?state=pending").json()["approvals"][-1]["id"]
    decided = client.post(f"/approvals/{approval_id}/decide", json={
        "decision": "approve", "decided_by": "dana@nvidia-demo.example",
        "caller_role": "human",
    })
    assert decided.status_code == 200
    assert decided.json()["effect"]["status"] == "executed"
    entry = [e for e in client.get("/ledger").json()["entries"]
             if e["type"] == "approval.decided"][-1]
    assert entry["body"]["auth_mode"] == "unattested"


def test_a_refusal_also_records_who_claimed_what(client):
    """The refusals are the entries a reviewer reads first."""
    approval_id = _queue(client, "eff-issuer-refused")
    refused = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "agent|nemo-pilot",
            "caller_role": "agent",
            "auth_mode": "mock",
        },
    )
    assert refused.status_code == 403
    entry = next(
        e
        for e in client.get("/ledger").json()["entries"]
        if e["type"] == "approval.refused"
    )
    assert entry["body"]["decided_by"] == "agent|nemo-pilot"
    assert entry["body"]["auth_mode"] == "mock"
    assert entry["body"]["issuer"] is None


def test_the_issuer_is_recorded_never_treated_as_permission(client):
    """Naming an authority does not grant anything. The role gate still rules."""
    approval_id = _queue(client, "eff-issuer-no-bypass")
    refused = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "agent|nemo-pilot",
            "caller_role": "agent",
            "issuer": "https://git.nemotron.example.com",
            "auth_mode": "oidc",
        },
    )
    assert refused.status_code == 403
    still = next(
        a for a in client.get("/approvals").json()["approvals"] if a["id"] == approval_id
    )
    assert still["state"] == "pending"
    assert still["issuer"] is None
