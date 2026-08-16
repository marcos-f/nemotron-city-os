"""The approvals JSON is a cache. The ledger is the record.

``ApprovalQueue._load`` used to swallow ``OSError`` and ``JSONDecodeError``
and return an EMPTY queue. A truncated write therefore showed an operator an
empty gate while the ledger still said ``effect.queued`` for every held
effect — holds silently dropped, in the one system whose whole claim is that
nothing is dropped silently. These tests corrupt the cache in every way we
can think of and assert that the queue still reflects the chain, and that the
service says so out loud.
"""

from __future__ import annotations

from pathlib import Path

from throughline.approvals import ApprovalQueue
from throughline.service import Settings, Substrate

from conftest import GOOD_CONFIG


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", config_path=GOOD_CONFIG)


def _hold_two(settings: Settings) -> Substrate:
    """A substrate with two irreversible effects held at the gate."""
    import asyncio

    from throughline.models import EffectProposal

    substrate = Substrate(settings)
    for effect_id, description in (
        ("eff-40k", "release $40k"),
        ("eff-cancel7", "cancel order 7"),
    ):
        signal_id = f"sig-{effect_id}"
        substrate.ledger.append("signal.ingested", {
            "id": signal_id, "class": "fire.incident", "source": "test",
            "real_or_synthetic": "real",
        })
        asyncio.run(substrate.gate.submit(EffectProposal(
            id=effect_id, effect_type="payment.release", description=description,
            signal_id=signal_id)))
    assert len(substrate.queue.pending()) == 2
    return substrate


def test_a_corrupt_cache_still_reflects_the_ledgers_held_effects(tmp_path):
    """The exact failure: corrupt the JSON, reboot, count the holds."""
    settings = _settings(tmp_path)
    first = _hold_two(settings)
    expected = {a.id for a in first.queue.pending()}

    first.queue.path.write_text('[{"id": "apr-1", "trunc', encoding="utf-8")

    second = Substrate(settings)
    assert {a.id for a in second.queue.pending()} == expected
    assert len(second.queue.pending()) == 2, "a corrupt cache must not empty the gate"


def test_a_corrupt_cache_reports_degraded_health(tmp_path):
    settings = _settings(tmp_path)
    first = _hold_two(settings)
    first.queue.path.write_text("not json at all", encoding="utf-8")

    second = Substrate(settings)
    health = second.queue.health
    assert health["degraded"] is True
    assert health["cache"] in ("unreadable", "rebuilt")
    assert "JSONDecodeError" in health["cache_error"]
    assert health["rebuilt_from_ledger"] == 2


def test_healthz_surfaces_the_degradation(tmp_path):
    from fastapi.testclient import TestClient

    from throughline.app import create_app

    settings = _settings(tmp_path)
    _hold_two(settings)
    (settings.approvals_path).write_text("{ truncated", encoding="utf-8")

    substrate = Substrate(settings)
    with TestClient(create_app(substrate=substrate)) as client:
        health = client.get("/healthz").json()
        assert health["status"] == "degraded"
        assert health["degraded"] is True
        assert any(a["signal"] == "approvals_degraded" for a in health["alarms"])
        assert health["approvals_pending"] == 2
        assert health["approvals"]["health"]["degraded"] is True
        # ...and the queue endpoint agrees.
        assert client.get("/approvals").json()["pending"] == 2
        assert client.get("/approvals").json()["health"]["degraded"] is True


def test_a_degraded_load_is_ledgered_as_an_alarm(tmp_path):
    settings = _settings(tmp_path)
    first = _hold_two(settings)
    first.queue.path.write_text("]]]", encoding="utf-8")

    second = Substrate(settings)
    alarms = [e for e in second.ledger.read() if e["type"] == "approvals.alarm"]
    assert alarms, "a degraded queue load must appear in the chain"
    assert alarms[-1]["body"]["path"] == str(second.queue.path)
    assert second.ledger.verify()["valid"] is True


def test_a_deleted_cache_is_rebuilt_from_the_ledger(tmp_path):
    settings = _settings(tmp_path)
    first = _hold_two(settings)
    first.queue.path.unlink()

    second = Substrate(settings)
    assert len(second.queue.pending()) == 2
    # A missing file is recoverable, not a corruption: no alarm needed, but the
    # holds are still there, which is the property that matters.
    assert second.queue.health["rebuilt_from_ledger"] == 2


def test_the_rebuilt_queue_carries_the_effect_details(tmp_path):
    settings = _settings(tmp_path)
    first = _hold_two(settings)
    first.queue.path.write_text("nonsense", encoding="utf-8")

    second = Substrate(settings)
    rebuilt = sorted(second.queue.pending(), key=lambda a: a.description or "")
    assert [a.description for a in rebuilt] == ["cancel order 7", "release $40k"]
    assert {a.reversibility_class for a in rebuilt} == {"irreversible"}
    assert all(a.effect_id.startswith("eff-") for a in rebuilt)


def test_a_decision_the_chain_records_survives_a_corrupt_cache(tmp_path):
    """Rebuilding must not un-decide a decision the ledger already carries."""
    import asyncio

    from throughline.models import Decision

    settings = _settings(tmp_path)
    first = _hold_two(settings)
    approval = first.queue.pending()[0]
    asyncio.run(first.gate.decide(approval.id, Decision(
        decision="approve", decided_by="oidc|operator-7", caller_role="human",
        issuer="https://git.nemotron.example.com", auth_mode="oidc")))

    first.queue.path.write_text("corrupt", encoding="utf-8")
    second = Substrate(settings)
    assert second.queue.get(approval.id).state == "approved"
    assert second.queue.get(approval.id).decided_by == "oidc|operator-7"
    assert len(second.queue.pending()) == 1


def test_the_ledger_wins_when_the_cache_claims_a_decision_it_does_not(tmp_path):
    """Fail closed: an unrecorded release leaves the effect held."""
    import json

    settings = _settings(tmp_path)
    first = _hold_two(settings)
    approval = first.queue.pending()[0]

    forged = json.loads(first.queue.path.read_text(encoding="utf-8"))
    for row in forged:
        if row["id"] == approval.id:
            row["state"] = "approved"
            row["decided_by"] = "oidc|not-in-the-chain"
    first.queue.path.write_text(json.dumps(forged), encoding="utf-8")

    second = Substrate(settings)
    assert second.queue.get(approval.id).state == "pending"
    assert len(second.queue.pending()) == 2
    assert second.queue.health["degraded"] is False  # the cache parsed fine...
    assert second.queue.health["reconciled"][0]["resolved_to"] == "pending"  # ...but lost


def test_a_cache_only_entry_is_kept(tmp_path):
    """A hold with no gate behind it is still a hold. Never drop one."""
    queue = ApprovalQueue(tmp_path / "approvals.json")
    approval = queue.enqueue("eff-direct", "irreversible")
    reopened = ApprovalQueue(tmp_path / "approvals.json")
    assert reopened.get(approval.id).state == "pending"
    assert reopened.health["cache_only_entries"] == 1


def test_a_malformed_row_degrades_rather_than_disappearing(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text('[{"id": "apr-1"}]', encoding="utf-8")
    queue = ApprovalQueue(path)
    assert queue.all() == []
    assert queue.health["degraded"] is True
    assert queue.health["cache"] == "partial"


def test_a_cache_that_is_not_a_list_degrades(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text('{"approvals": []}', encoding="utf-8")
    queue = ApprovalQueue(path)
    assert queue.health["degraded"] is True
    assert queue.health["cache"] == "malformed"


def test_a_healthy_boot_is_not_degraded(tmp_path):
    settings = _settings(tmp_path)
    _hold_two(settings)
    second = Substrate(settings)
    assert second.queue.health["degraded"] is False
    assert second.queue.health["cache"] == "ok"
    assert len(second.queue.pending()) == 2


def test_the_repaired_cache_is_usable_on_the_next_boot(tmp_path):
    settings = _settings(tmp_path)
    first = _hold_two(settings)
    first.queue.path.write_text("corrupt", encoding="utf-8")

    Substrate(settings)  # rebuilds and rewrites the cache
    third = Substrate(settings)
    assert third.queue.health["degraded"] is False
    assert len(third.queue.pending()) == 2
