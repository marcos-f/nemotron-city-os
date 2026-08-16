"""Issue (mock/stub data in the product): a repeatedly-firing feed must not
grow the live approval queue without bound.

A review found every approval stamped ``auth_mode: unattested`` /
``verified: false``, and a 346-deep backlog of near-duplicate SYNTHETIC
``battery_4`` load-shed approvals, oldest ~10h, generated continuously with
no throttling. throughline does not own the sibling feed that generated
them and cannot delete ledger history — the chain is append-only, and
rewriting it would be the one unforgivable act for this product. What it
does own is the gate every proposal passes through on the way IN, so the
fix caps a near-duplicate backlog there, BY DEFAULT, with no special
configuration required. A proposal that is a genuine duplicate past the cap
is refused (429), ledgered by name, and never silently dropped; a proposal
that differs in type or description is untouched.
"""

from __future__ import annotations

from throughline.gate import DEFAULT_MAX_PENDING_DUPLICATES, resolve_max_pending_duplicates

DUPLICATE_PROPOSAL = {
    "id": "eff-dup",
    "effect_type": "dispatch.load_shed",
    "reversibility": "irreversible",
    "description": "Load-shed dispatch for battery_4 (soc_delta -7.06%, tick 23)",
    "signal_id": "sig-dup",
}

DUPLICATE_SIGNAL = {
    "id": "sig-dup", "class": "dispatch.load_shed", "source": "test",
    "real_or_synthetic": "real",
}


def test_the_cap_is_a_small_positive_default_with_no_configuration():
    """The product default must actually bound the backlog — not "0" (which
    would silently disable it) and not left unbounded."""
    assert resolve_max_pending_duplicates(None) == DEFAULT_MAX_PENDING_DUPLICATES
    assert 0 < DEFAULT_MAX_PENDING_DUPLICATES < 50


def test_a_repeatedly_firing_feed_is_capped_by_default(client):
    client.post("/signals", json=DUPLICATE_SIGNAL)
    for _ in range(DEFAULT_MAX_PENDING_DUPLICATES):
        response = client.post("/effects", json=DUPLICATE_PROPOSAL)
        assert response.status_code == 202

    refused = client.post("/effects", json=DUPLICATE_PROPOSAL)
    assert refused.status_code == 429
    body = refused.json()
    assert body["refused"] is True
    assert body["refusal_reason"] == "pending-duplicate-cap"

    pending = [
        a for a in client.get("/approvals").json()["approvals"]
        if a["effect_type"] == DUPLICATE_PROPOSAL["effect_type"] and a["state"] == "pending"
    ]
    assert len(pending) == DEFAULT_MAX_PENDING_DUPLICATES, (
        "the backlog must stop growing at the cap, not merely error on the "
        "one call over it"
    )


def test_the_refusal_is_ledgered_by_name(client):
    client.post("/signals", json=DUPLICATE_SIGNAL)
    for _ in range(DEFAULT_MAX_PENDING_DUPLICATES):
        client.post("/effects", json=DUPLICATE_PROPOSAL)
    client.post("/effects", json=DUPLICATE_PROPOSAL)

    entries = [
        e["body"] for e in client.get("/ledger", params={"limit": 1000}).json()["entries"]
        if e["type"] == "effect.refused"
    ]
    assert entries, "a capped proposal must be a named, ledgered refusal, never a silent drop"
    assert entries[-1]["policy"] == "pending-duplicate-cap"


def test_a_genuinely_distinct_effect_is_not_capped(client):
    client.post("/signals", json=DUPLICATE_SIGNAL)
    for _ in range(DEFAULT_MAX_PENDING_DUPLICATES):
        client.post("/effects", json=DUPLICATE_PROPOSAL)

    distinct = client.post("/effects", json={
        **DUPLICATE_PROPOSAL,
        "description": "Load-shed dispatch for battery_9 (soc_delta -3.1%, tick 40)",
    })
    assert distinct.status_code == 202


def test_repeated_ledger_history_from_before_the_fix_is_never_rewritten(client):
    """The cap governs NEW proposals only. It must never touch, hide or
    shrink what the chain already recorded."""
    client.post("/signals", json=DUPLICATE_SIGNAL)
    length_before = client.get("/ledger/verify").json()["chain_length"]
    for _ in range(DEFAULT_MAX_PENDING_DUPLICATES + 2):
        client.post("/effects", json=DUPLICATE_PROPOSAL)
    verification = client.get("/ledger/verify").json()
    assert verification["valid"] is True
    assert verification["chain_length"] >= length_before
