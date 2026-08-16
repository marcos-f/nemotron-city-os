"""test://siren/offline-complete — the whole demo path, zero sockets.

The guard is the assertion. Every test here runs with socket.connect
replaced by a function that fails the test, so 'offline' is demonstrated
rather than declared.
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from siren import feed
from siren.service import create_app
from siren.substrate import substrate_choice


@pytest.fixture
def offline_client(offline, data_dir, seeded_snapshot, no_network):
    with TestClient(create_app()) as client:
        yield client


def test_offline_mode_is_read_from_the_environment(monkeypatch):
    for truthy in ("1", "true", "yes", "on"):
        monkeypatch.setenv("OFFLINE_MODE", truthy)
        assert feed.offline_mode() is True
    monkeypatch.setenv("OFFLINE_MODE", "0")
    assert feed.offline_mode() is False


def test_offline_forces_the_mock_substrate(monkeypatch):
    """Even asked for the real substrate, offline means offline: a stale
    SIREN_SUBSTRATE=real must not open a socket behind the demo's back."""
    monkeypatch.setenv("OFFLINE_MODE", "1")
    monkeypatch.setenv("SIREN_SUBSTRATE", "real")
    assert substrate_choice() == "mock"


def test_the_guard_itself_bites(no_network):
    with pytest.raises(AssertionError, match="network access attempted"):
        socket.create_connection(("data.seattle.gov", 443))


def test_full_demo_path_offline_opens_no_socket(offline_client):
    """The beats, in order, with the guard armed the whole way through."""
    health = offline_client.get("/healthz").json()
    assert health["mode"] == "offline"
    assert health["substrate"]["substrate"] == "mock"

    pulse = offline_client.get("/pulse").json()
    assert pulse["status"]["source"] == "snapshot"
    assert "OFFLINE_MODE" in pulse["status"]["label"]
    assert len(pulse["incidents"]) == 2

    incidents = offline_client.get("/incidents")
    assert incidents.status_code == 200
    assert incidents.headers["x-siren-source"] == "snapshot"

    refreshed = offline_client.post("/feed/refresh").json()
    assert refreshed["source"] == "snapshot"

    dropped = offline_client.post("/hot-reload", json={"path": "config/incident.yaml",
                                                       "emit": 2})
    assert dropped.status_code == 200
    assert [step["state"] for step in dropped.json()["steps"]] == ["ok", "ok", "ok"]
    assert dropped.json()["signals"]["emitted_count"] == 2

    refused = offline_client.post("/hot-reload",
                                  json={"path": "config/incident.invalid.yaml"})
    assert refused.status_code == 422
    assert refused.json()["refusal"]["rule"] == "dispatch_units"

    timeline = offline_client.get("/hot-reload/timeline").json()
    assert timeline["registered_classes"] == ["fire.incident"], (
        "the refused drop must leave the previously registered class flowing"
    )


def test_offline_refresh_never_attempts_a_poll(offline_client, monkeypatch):
    def explode(**kwargs):
        raise AssertionError("fetch_live must not be called in OFFLINE_MODE")

    monkeypatch.setattr(feed, "fetch_live", explode)
    assert offline_client.post("/feed/refresh").json()["mode"] == "offline"


def test_offline_serves_from_the_packaged_seed_with_no_cache_on_disk(
    offline, data_dir, no_network
):
    """A clean install that has never polled still serves the demo."""
    with TestClient(create_app()) as client:
        body = client.get("/pulse").json()
    assert body["status"]["source"] == "snapshot"
    assert body["incidents"], "the packaged seed carries the demo through"
