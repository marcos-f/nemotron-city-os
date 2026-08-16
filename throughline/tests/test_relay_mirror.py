"""test://throughline/relay-mirror — Relay's verdict, mirrored into our ledger.

Honesty is under test here as much as plumbing: where the nemo_relay package
is absent the mirror must say ``mock``, and it must never claim otherwise.
"""

from __future__ import annotations

import asyncio

import pytest

from throughline.ledger import Ledger
from throughline.models import Decision, EffectProposal
from throughline.relay import RELAY_AVAILABLE, RelayGateMirror, _default_decider


@pytest.fixture
def mirror(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    instance = RelayGateMirror(ledger=ledger, name="throughline.test")
    yield instance
    instance.deregister()


def test_relay_mirror(client):
    """test://throughline/relay-mirror — the guardrail record reaches the ledger."""
    client.post("/signals", json={
        "id": "sig-relay-mirror", "class": "demo.signal", "source": "test",
        "real_or_synthetic": "real",
    })
    effect = client.post("/effects", json={
        "id": "eff-relay-mirror",
        "effect_type": "payment.release", "description": "release $40k",
        "signal_id": "sig-relay-mirror",
    }).json()
    assert effect["status"] == "queued"

    records = [
        e for e in client.get("/ledger").json()["entries"] if e["type"] == "relay.guardrail"
    ]
    assert records, "no Relay guardrail record was mirrored"
    body = records[-1]["body"]
    assert body["allowed"] is False
    assert body["rejected"] is True
    assert "held for human approval" in body["rejection_reason"]
    assert body["effect_id"] == effect["id"]
    assert body["mode"] in ("nemo-relay", "mock")
    assert client.get("/ledger/verify").json()["valid"] is True


def test_relay_mirror_records_the_allowed_case(client):
    approved = client.post("/effects", json={
        "id": "eff-relay-mirror-allowed", "effect_type": "notify.operator",
    }).json()
    assert approved["status"] == "executed"
    record = [
        e["body"] for e in client.get("/ledger").json()["entries"]
        if e["type"] == "relay.guardrail"
    ][-1]
    assert record["allowed"] is True
    assert record["rejected"] is False
    assert record["rejection_reason"] is None


def test_release_flips_the_guardrail_verdict(client):
    """Same effect, two verdicts: held before the decision, allowed after."""
    client.post("/signals", json={
        "id": "sig-relay-flip", "class": "demo.signal", "source": "test",
        "real_or_synthetic": "real",
    })
    effect = client.post("/effects", json={
        "id": "eff-relay-flip", "effect_type": "order.cancel",
        "signal_id": "sig-relay-flip", "description": "cancel order",
    }).json()
    approval = client.get("/approvals").json()["approvals"][0]
    client.post(f"/approvals/{approval['id']}/decide", json={
        "decision": "approve", "decided_by": "oidc|operator-7", "caller_role": "human",
        "issuer": "https://git.nemotron.example.com", "auth_mode": "oidc"})

    verdicts = [
        e["body"] for e in client.get("/ledger").json()["entries"]
        if e["type"] == "relay.guardrail" and e["body"]["effect_id"] == effect["id"]
    ]
    assert [v["allowed"] for v in verdicts] == [False, True]


def test_the_guardrail_blocks_the_callback_itself(mirror):
    """Not a status field: the tool callback genuinely does not run."""
    ran = []

    async def tool(args):
        ran.append(args)
        return {"ran": True}

    result, record = asyncio.run(mirror.execute(
        "effect.execute",
        {"effect_id": "eff-1", "reversibility_class": "irreversible", "approved": False},
        tool,
    ))
    assert ran == []
    assert result is None
    assert record["rejected"] is True


def test_the_guardrail_allows_an_approved_irreversible_effect(mirror):
    async def tool(args):
        return {"ran": True}

    result, record = asyncio.run(mirror.execute(
        "effect.execute",
        {"effect_id": "eff-1", "reversibility_class": "irreversible", "approved": True},
        tool,
    ))
    assert result == {"ran": True}
    assert record["allowed"] is True


def test_every_record_is_mirrored_to_the_ledger(mirror):
    async def tool(args):
        return {"ran": True}

    asyncio.run(mirror.execute(
        "effect.execute", {"effect_id": "eff-1", "reversibility_class": "reversible"}, tool
    ))
    mirrored = [e for e in mirror.ledger.read() if e["type"] == "relay.guardrail"]
    assert len(mirrored) == 1
    assert mirror.ledger.verify()["valid"] is True


def test_decider_holds_only_unapproved_irreversible_effects():
    assert _default_decider("t", {"reversibility_class": "reversible"}) is None
    assert _default_decider("t", {"reversibility_class": "irreversible", "approved": True}) is None
    assert _default_decider("t", {"reversibility_class": "irreversible"}) is not None


def test_mode_is_labelled_honestly(mirror):
    """No screen may claim NeMo Relay where none ran."""
    status = mirror.status()
    if RELAY_AVAILABLE:
        assert status["mode"] == "nemo-relay"
        assert status["relay_version"] == "0.7.3"
        assert "integrity" in status["note"]
    else:
        assert status["mode"] == "mock"
        assert status["note"].startswith("MOCK:")


@pytest.mark.skipif(not RELAY_AVAILABLE, reason="nemo-relay is not installed")
def test_the_real_relay_pipeline_is_used(mirror):
    """The verdict comes from Relay's registry, not from our own decider."""
    import nemo_relay

    async def tool(args):
        return {"ran": True}

    mirror.register()
    assert mirror._registered is True

    async def call_relay_directly():
        # Relay resolves the guardrail chain inside the running loop, so the
        # call has to be made there rather than handed to asyncio.run().
        return await nemo_relay.tools.execute(
            "effect.execute",
            {"effect_id": "eff-1", "reversibility_class": "irreversible"},
            tool,
        )

    with pytest.raises(RuntimeError, match="guardrail rejected"):
        asyncio.run(call_relay_directly())


@pytest.mark.skipif(not RELAY_AVAILABLE, reason="nemo-relay is not installed")
def test_registration_replaces_a_same_named_guardrail(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    first = RelayGateMirror(ledger=ledger, name="throughline.dup")
    second = RelayGateMirror(ledger=ledger, name="throughline.dup")
    try:
        first.register()
        second.register()  # must replace, not raise
        assert second._registered is True
    finally:
        second.deregister()
        first.deregister()
