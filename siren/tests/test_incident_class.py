"""SG2 — the incident class, and the envelope that does not change for it.

siren exists to prove the envelope generalizes: an incident is not telemetry,
not a permit and not a video frame, yet it rides the identical Signal. These
tests pin the class config and the emitted envelope to the federation
contract rather than to siren's convenience.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from siren import reload as reload_mod
from siren.models import Signal
from siren.service import signal_for
from siren.substrate import MockSubstrate

CONFIG = Path("config/incident.yaml")
INVALID = Path("config/incident.invalid.yaml")


def test_incident_yaml_exists_and_is_the_drop_artifact():
    assert CONFIG.exists(), "config/incident.yaml is the file the demo drops in"
    parsed = reload_mod.validate_config_file(CONFIG)
    assert parsed["signal_classes"] == ["fire.incident"]
    assert parsed["version"] == 1


def test_incident_yaml_registers_the_incident_effects():
    parsed = reload_mod.validate_config_file(CONFIG)
    by_id = {rule["id"]: rule for rule in parsed["effects"]}
    assert "incident_notify" in by_id and "dispatch_units" in by_id
    assert by_id["dispatch_units"]["reversibility_class"] == "irreversible"
    assert by_id["dispatch_units"]["auto_execute"] is False, (
        "rolling apparatus is irreversible; it is held for a human, always"
    )
    assert by_id["incident_notify"]["auto_execute"] is True


def test_incident_yaml_carries_the_federation_baseline_forward():
    """A reload replaces the whole registry. Dropping incident.yaml must not
    deregister the effects the other four components depend on."""
    parsed = reload_mod.validate_config_file(CONFIG)
    types = {rule["effect_type"] for rule in parsed["effects"]}
    for baseline in ("notify.operator", "draft.hold", "payment.release", "order.cancel"):
        assert baseline in types, f"{baseline} would be deregistered by the drop"


def test_incident_yaml_matches_throughlines_published_schema():
    """Every rule carries the four keys throughline's loader requires."""
    raw = yaml.safe_load(CONFIG.read_text())
    for rule in raw["effects"]:
        assert set(("id", "effect_type", "reversibility_class")) <= set(rule)
        assert rule["reversibility_class"] in ("reversible", "irreversible")


def test_signal_uses_the_federation_envelope_unchanged(client):
    state = client.app.state.feed
    incident = state.incidents[0]

    signal = signal_for(incident, state)
    wire = signal.wire()

    assert wire["class"] == "fire.incident"
    assert wire["source"] == "seattle.fire.911"
    assert wire["real_or_synthetic"] == "real"
    assert wire["id"] == f"siren-incident-{incident.id}"
    assert wire["staleness"].startswith("PT") and wire["staleness"].endswith("S")
    assert set(("id", "class", "source", "ingest_time", "real_or_synthetic")) <= set(wire)


def test_cached_rows_stay_real_and_carry_their_age(client):
    """Replayed 911 records are still real records. The honest move is to
    keep real_or_synthetic=real and let staleness say how old they are."""
    state = client.app.state.feed
    signal = signal_for(state.incidents[0], state)
    assert signal.real_or_synthetic == "real"
    seconds = int(signal.staleness.removeprefix("PT").removesuffix("S"))
    assert seconds > 0, "a snapshot served now is not zero seconds old"


def test_emit_sends_incident_signals_to_the_substrate(client):
    body = client.post("/feed/emit?limit=2").json()

    assert body["emitted_count"] == 2
    assert body["signal_class"] == "fire.incident"
    assert body["substrate"] == "mock"
    assert body["source"] == "snapshot"
    sent = client.app.state.substrate.signals
    assert [s["class"] for s in sent] == ["fire.incident", "fire.incident"]


def test_post_incident_signal_relays_and_reports_the_relay(client):
    payload = {
        "id": "siren-incident-F260815-114",
        "class": "fire.incident",
        "source": "seattle.fire.911",
        "ingest_time": "2026-08-15T18:02:00Z",
        "real_or_synthetic": "real",
        "payload_ref": "https://data.seattle.gov/incident/F260815-114",
        "staleness": "PT2M",
    }
    response = client.post("/signals/incident", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["class"] == "fire.incident"
    assert body["relayed"] is True
    assert body["substrate"] == "mock"


def test_relay_failure_is_reported_as_failure_not_swallowed(client, monkeypatch):
    from siren.substrate import SubstrateError

    def refuse(signal):
        raise SubstrateError("throughline unreachable at http://127.0.0.1:8600")

    monkeypatch.setattr(client.app.state.substrate, "post_signal", refuse)

    body = client.post("/signals/incident", json={
        "id": "s1", "class": "fire.incident", "source": "seattle.fire.911",
        "ingest_time": "2026-08-15T18:02:00Z", "real_or_synthetic": "real",
    }).json()

    assert body["relayed"] is False
    assert "unreachable" in body["relay_error"]


def test_signal_model_round_trips_the_class_alias():
    signal = Signal(**{"id": "x", "class": "fire.incident", "source": "s",
                       "ingest_time": "2026-08-15T18:02:00Z", "real_or_synthetic": "real"})
    assert signal.signal_class == "fire.incident"
    assert "signal_class" not in signal.wire()


def test_mock_substrate_opens_nothing():
    mock = MockSubstrate()
    assert mock.health()["reachable"] is True
    assert "mock" in mock.health()["note"]
    assert mock.reload_config("config/incident.yaml")["accepted"] is True


def test_invalid_variant_is_refused_naming_file_rule_and_line():
    with pytest.raises(reload_mod.ConfigRefusal) as caught:
        reload_mod.validate_config_file(INVALID)

    refusal = caught.value.as_dict()
    assert refusal["rule"] == "dispatch_units"
    assert refusal["policy"] == "no-auto-execute-on-irreversible"
    assert refusal["line"] == 28
    assert str(INVALID) in refusal["file"]
