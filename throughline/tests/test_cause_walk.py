"""scenario://throughline/cause-walk — effect to cause in <=3 verified hops.

helm renders these hops with a tick per hash. The ticks mean something only if
they can go red, so the tamper case is here too.
"""

from __future__ import annotations

import json

import pytest

SIGNAL = {
    "id": "sig-walk-1", "class": "fire.incident", "source": "seattle.fire.911",
    "real_or_synthetic": "real",
}
JUDGMENT = {
    "id": "jud-walk-1", "signal_id": "sig-walk-1", "verdict": "dispatch",
    "produced_by": "docket", "confidence": 0.82,
}


@pytest.fixture
def walked(client):
    client.post("/signals", json=SIGNAL)
    client.post("/judgments", json=JUDGMENT)
    effect = client.post("/effects", json={
        "id": "eff-walk-1",
        "effect_type": "payment.release", "signal_id": SIGNAL["id"],
        "judgment_id": JUDGMENT["id"], "description": "release contractor funds",
    }).json()
    return client, effect


def test_walk_resolves_effect_to_signal_in_three_hops(walked):
    client, effect = walked
    walk = client.get(f"/effects/{effect['id']}/walk").json()

    assert [hop["kind"] for hop in walk["hops"]] == ["effect", "judgment", "signal"]
    assert walk["hop_count"] == 3
    assert walk["complete"] is True
    assert walk["missing"] == []
    assert walk["all_hops_verified"] is True
    assert [hop["id"] for hop in walk["hops"]] == [
        effect["id"], JUDGMENT["id"], SIGNAL["id"]
    ]


def test_each_hop_carries_a_verified_hash(walked):
    client, effect = walked
    for hop in client.get(f"/effects/{effect['id']}/walk").json()["hops"]:
        assert hop["verified"] is True
        assert len(hop["sha256"]) == 64
        assert hop["hash_prefix"] == hop["sha256"][:8]
        assert hop["seq"] >= 1


def test_the_hops_run_backwards_in_the_chain(walked):
    """Cause precedes consequence, and the ledger proves the order."""
    client, effect = walked
    seqs = [hop["seq"] for hop in client.get(f"/effects/{effect['id']}/walk").json()["hops"]]
    assert seqs == sorted(seqs, reverse=True)


def test_a_tampered_hop_shows_as_unverified(walked):
    """The tick can go red — otherwise it is decoration."""
    client, effect = walked
    path = client.substrate.ledger.path
    lines = path.read_text(encoding="utf-8").splitlines()
    victim = next(i for i, line in enumerate(lines) if '"jud-walk-1"' in line)
    lines[victim] = lines[victim].replace('"dispatch"', '"disPatch"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    walk = client.get(f"/effects/{effect['id']}/walk").json()
    judgment_hop = next(hop for hop in walk["hops"] if hop["kind"] == "judgment")
    assert judgment_hop["verified"] is False
    assert walk["all_hops_verified"] is False
    assert client.get("/ledger/verify").json()["valid"] is False


def test_walk_without_a_judgment_is_two_hops(client):
    client.post("/signals", json=SIGNAL)
    effect = client.post("/effects", json={
        "id": "eff-walk-2",
        "effect_type": "notify.operator", "signal_id": SIGNAL["id"]}).json()
    walk = client.get(f"/effects/{effect['id']}/walk").json()
    assert [hop["kind"] for hop in walk["hops"]] == ["effect", "signal"]
    assert walk["complete"] is True


def test_walk_names_what_is_missing(client):
    effect = client.post("/effects", json={
        "id": "eff-walk-3", "effect_type": "notify.operator"}).json()
    walk = client.get(f"/effects/{effect['id']}/walk").json()
    assert walk["complete"] is False
    assert walk["missing"] == ["signal (the effect names none)"]


def test_walk_reports_a_dangling_reference(client):
    effect = client.post("/effects", json={
        "id": "eff-walk-4",
        "effect_type": "notify.operator", "signal_id": "sig-never-ingested"}).json()
    walk = client.get(f"/effects/{effect['id']}/walk").json()
    assert walk["missing"] == ["signal sig-never-ingested"]
    assert walk["complete"] is False


def test_walk_of_an_unknown_effect_is_404(client):
    assert client.get("/effects/eff-nope/walk").status_code == 404


def test_judgments_are_ledgered(client):
    client.post("/signals", json=SIGNAL)
    response = client.post("/judgments", json=JUDGMENT)
    assert response.status_code == 201
    entries = client.get("/ledger").json()["entries"]
    recorded = [e for e in entries if e["type"] == "judgment.recorded"]
    assert recorded[-1]["body"]["signal_id"] == SIGNAL["id"]
    assert client.get("/ledger/verify").json()["valid"] is True


def test_ledger_rows_carry_their_own_verification(client):
    client.post("/signals", json=SIGNAL)
    entries = client.get("/ledger").json()["entries"]
    assert entries and all(entry["verified"] is True for entry in entries)

    path = client.substrate.ledger.path
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1].replace('"seattle.fire.911"', '"forged.source"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = client.get("/ledger").json()["entries"]
    assert rows[-1]["verified"] is False
    assert all(row["verified"] for row in rows[:-1])


def test_walk_survives_a_restart(client, substrate):
    """The walk is read from the ledger, so it outlives the process."""
    client.post("/signals", json=SIGNAL)
    client.post("/judgments", json=JUDGMENT)
    effect = client.post("/effects", json={
        "id": "eff-walk-5",
        "effect_type": "order.cancel", "signal_id": SIGNAL["id"],
        "judgment_id": JUDGMENT["id"], "description": "cancel the order"}).json()

    from throughline.service import Substrate
    from throughline.walk import walk_effect

    reopened = Substrate(substrate.settings)
    walk = walk_effect(reopened.ledger, effect["id"])
    assert walk["hop_count"] == 3
    assert walk["all_hops_verified"] is True
