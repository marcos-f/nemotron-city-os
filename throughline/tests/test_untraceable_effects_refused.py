"""throughline#11 — the gate must refuse an effect that names no signal.

Before this fix, ``POST /effects`` minted a fallback id
(``eff-{12 hex}``) for a proposal missing one and admitted it — with a null
``effect_type``, ``description`` and ``signal_id`` — permanently into the
pending-approval queue. A support reviewer found 206 of 452 pending
approvals in exactly that shape: untraceable to any cause, forever.

The fix: a queued effect that names no ingested signal, no effect_type or
no description is refused outright — ledgered by name, exactly like the
anonymous-decision refusal — and no id is invented for it. This module
covers the DISCIPLINE minimum from the issue: a no-signal effect is
refused, the refusal is ledgered by name, an unknown-signal effect is
refused, and ``/walk`` on a newly admitted (properly traceable) effect
comes back ``complete: true``.
"""

from __future__ import annotations


def test_an_effect_naming_no_signal_is_refused(client):
    """A queued effect with an id, effect_type and description, but no
    signal_id, is refused — not admitted with a null cause."""
    response = client.post("/effects", json={
        "id": "eff-no-signal", "effect_type": "payment.release",
        "description": "release $40k",
    })
    assert response.status_code == 422
    body = response.json()
    assert body["refused"] is True
    assert body["refusal_reason"] == "effect-names-no-signal"
    assert body["effect_id"] == "eff-no-signal"

    # Nothing was queued, and no fallback id was minted for it.
    assert client.get("/effects/eff-no-signal").status_code == 404
    assert client.get("/approvals?state=pending").json()["approvals"] == []


def test_the_refusal_is_ledgered_by_name(client):
    """Refusing loudly and traceably is the product's whole thesis — a
    silent 400 would be a missed opportunity to demonstrate it."""
    client.post("/effects", json={
        "id": "eff-no-signal-2", "effect_type": "payment.release",
        "description": "release $40k",
    })
    entries = [
        e for e in client.get("/ledger", params={"limit": 1000}).json()["entries"]
        if e["type"] == "effect.refused"
    ]
    assert entries, "the refusal was not written to the ledger at all"
    refusal = entries[-1]["body"]
    assert refusal["id"] == "eff-no-signal-2"
    assert refusal["policy"] == "effect-names-no-signal"
    assert refusal["reason"]
    assert client.get("/ledger/verify").json()["valid"] is True


def test_an_effect_naming_an_unknown_signal_is_refused(client):
    """A signal_id that was never ingested cannot be a cause — a proposal
    naming one is refused, not admitted with a dangling reference."""
    response = client.post("/effects", json={
        "id": "eff-unknown-signal", "effect_type": "payment.release",
        "description": "release $40k", "signal_id": "sig-never-ingested",
    })
    assert response.status_code == 422
    body = response.json()
    assert body["refused"] is True
    assert body["refusal_reason"] == "effect-names-unresolvable-signal"
    assert client.get("/effects/eff-unknown-signal").status_code == 404


def test_an_effect_with_no_id_is_refused(client):
    """The uuid fallback is gone: an absent id is an error, never a prompt
    to invent one — even when everything else is present."""
    response = client.post("/effects", json={
        "effect_type": "payment.release", "description": "release $40k",
    })
    assert response.status_code == 422
    assert response.json()["refusal_reason"] == "effect-id-required"


def test_walk_on_a_newly_admitted_effect_is_complete(client):
    """Every consequence traceable to its cause: a properly-named effect
    walks back to its signal in one hop and reports complete: true."""
    client.post("/signals", json={
        "id": "sig-newly-admitted", "class": "fire.incident",
        "source": "test", "real_or_synthetic": "real",
    })
    effect = client.post("/effects", json={
        "id": "eff-newly-admitted", "effect_type": "payment.release",
        "description": "release $40k", "signal_id": "sig-newly-admitted",
    }).json()
    assert effect["id"] == "eff-newly-admitted"

    walk = client.get(f"/effects/{effect['id']}/walk").json()
    assert walk["complete"] is True
    assert walk["missing"] == []
    assert [hop["kind"] for hop in walk["hops"]] == ["effect", "signal"]
