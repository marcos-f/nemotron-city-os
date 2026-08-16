"""test://siren/offline-runtime — offline for a siren that is already running.

Reported by an independent SRE review: ``demo.py --offline`` was offline only
for the demo's own process. The demo arms a socket guard inside itself, but
siren runs beside it in a separate process, and ``OFFLINE_MODE`` is read from
the environment — which cannot be set on a process that has already started.
So an "offline" run went on pulling live Socrata through siren, while the UI
labelled the data cached. A demo risk and an honesty problem at once.

(``--self-contained --offline`` was and remains airtight; the hole was
specific to plain ``--offline`` against the real federation.)

These tests exercise the runtime switch on an app that booted LIVE, because
that is the situation the demo is actually in. The guard is armed AFTER the
switch, so it proves the switch rather than the fixture.
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from siren import feed
from siren.service import create_app
from siren.substrate import substrate_choice


@pytest.fixture
def live_client(monkeypatch, data_dir, seeded_snapshot):
    """An app that booted in LIVE mode, with the poll stubbed out.

    ``fetch_live`` is patched rather than really polled: the point of these
    tests is what happens after the switch, and a test that reaches Seattle
    to prove it does not reach Seattle would be its own kind of joke.
    """
    monkeypatch.delenv("OFFLINE_MODE", raising=False)
    incidents = list(seeded_snapshot.incidents)
    monkeypatch.setattr(feed, "fetch_live", lambda **kw: list(incidents))
    with TestClient(create_app()) as client:
        assert client.get("/healthz").json()["mode"] == "live"
        yield client


@pytest.fixture
def armed(monkeypatch):
    """Arm the socket guard now. Returns nothing; it fails the test instead."""

    def forbid(*args, **kwargs):
        raise AssertionError(f"network access attempted in offline mode: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)


# ------------------------------------------------------- the switch exists


def test_a_running_siren_can_be_switched_offline(live_client):
    """The defect in one assertion: before, there was no way to do this."""
    assert live_client.get("/healthz").json()["mode"] == "live"

    body = live_client.post("/feed/mode", json={"mode": "offline"}).json()

    assert body["offline"] is True
    assert body["source"] == "runtime-switch"
    assert live_client.get("/healthz").json()["mode"] == "offline"


def test_offline_serves_only_the_cached_snapshot_with_its_as_of_label(live_client):
    cached_as_of = live_client.get("/feed/status").json()["as_of"]

    body = live_client.post("/feed/mode", json={"mode": "offline"}).json()
    status = body["feed"]

    assert status["source"] == "snapshot"
    assert status["mode"] == "offline"
    assert "OFFLINE_MODE" in status["label"], "the pane says cached, in words"
    assert status["as_of"] == cached_as_of, (
        "the as-of is when the data was TAKEN, not when we went offline; "
        "re-stamping cached rows with 'now' is the lie the label exists to "
        "prevent"
    )

    pulse = live_client.get("/pulse").json()
    assert pulse["status"]["as_of"] == cached_as_of
    assert pulse["status"]["source"] == "snapshot"
    assert len(pulse["incidents"]) == status["incident_count"]

    # And it does not drift on a re-read, either.
    assert live_client.post("/feed/refresh").json()["as_of"] == cached_as_of


def test_no_outbound_connection_is_attempted_once_offline(live_client, armed):
    """The guard is armed AFTER the switch, so it tests the switch."""
    live_client.post("/feed/mode", json={"mode": "offline"})

    # Every path a demo touches, with the guard live the whole way.
    assert live_client.get("/healthz").status_code == 200
    assert live_client.get("/pulse").status_code == 200
    assert live_client.get("/incidents").status_code == 200
    assert live_client.get("/feed/status").status_code == 200
    assert live_client.post("/feed/refresh").json()["source"] == "snapshot"
    assert live_client.post("/feed/emit", params={"limit": 2}).status_code == 200
    dropped = live_client.post("/hot-reload", json={"path": "config/incident.yaml",
                                                    "emit": 2})
    assert dropped.status_code == 200


def test_the_guard_used_here_actually_bites(armed):
    """If the guard did not bite, the test above would prove nothing."""
    with pytest.raises(AssertionError, match="network access attempted"):
        socket.create_connection(("data.seattle.gov", 443))


def test_the_poll_refuses_before_it_can_open_a_socket(monkeypatch, armed):
    """Belt and braces: fetch_live itself refuses, not just its caller.

    ``refresh`` checks the switch first, so this guard is redundant on the
    happy path. That is deliberate — it makes offline a property of the
    function that opens the socket, so a future caller cannot reach Socrata
    by coming in a different door. The armed guard proves nothing was
    attempted: an OfflineRefusal means no request was ever made, where a
    URLError would only mean one failed.
    """
    monkeypatch.delenv("OFFLINE_MODE", raising=False)
    feed.set_offline(True)

    with pytest.raises(feed.OfflineRefusal) as excinfo:
        feed.fetch_live(limit=1)

    assert "offline" in str(excinfo.value)
    assert "runtime-switch" in str(excinfo.value)


def test_refresh_degrades_to_the_snapshot_if_the_switch_flips_mid_poll(
    monkeypatch, data_dir, seeded_snapshot
):
    """A race between the check and the call must become a cache read, not
    a socket."""
    monkeypatch.delenv("OFFLINE_MODE", raising=False)

    real_fetch = feed.fetch_live

    def flip_then_poll(**kwargs):
        feed.set_offline(True)
        return real_fetch(**kwargs)

    monkeypatch.setattr(feed, "fetch_live", flip_then_poll)

    state = feed.refresh(feed.FeedState())

    assert state.serving == "snapshot"
    assert state.label == feed.OFFLINE_LABEL


def test_offline_forces_the_mock_substrate_even_when_asked_for_real(
    live_client, monkeypatch
):
    """Offline is a guarantee. A stale SIREN_SUBSTRATE=real must not survive
    it, or the switch is only half true and a socket stays open behind it."""
    monkeypatch.setenv("SIREN_SUBSTRATE", "real")
    assert substrate_choice() == "real"

    body = live_client.post("/feed/mode", json={"mode": "offline"}).json()

    assert substrate_choice() == "mock"
    assert body["substrate"] == "mock"
    assert live_client.get("/healthz").json()["substrate"]["substrate"] == "mock"


# ------------------------------------------- live mode is not taken away


def test_live_mode_still_polls_after_being_switched_back(live_client):
    """This is an added guarantee, not a replacement. Live must still be live."""
    live_client.post("/feed/mode", json={"mode": "offline"})
    assert live_client.get("/healthz").json()["mode"] == "offline"

    body = live_client.post("/feed/mode", json={"mode": "live"}).json()

    assert body["offline"] is False
    assert body["feed"]["source"] == "live"
    assert body["feed"]["mode"] == "live"
    assert body["feed"]["label"] == feed.LIVE_LABEL


def test_env_clears_the_override_and_hands_control_back(live_client, monkeypatch):
    live_client.post("/feed/mode", json={"mode": "offline"})
    assert feed.offline_source() == "runtime-switch"

    body = live_client.post("/feed/mode", json={"mode": "env"}).json()

    assert body["override"] is None
    assert body["source"] == "default"
    assert body["offline"] is False


def test_the_env_switch_still_wins_when_no_override_is_set(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    feed.set_offline(None)
    assert feed.offline_mode() is True
    assert feed.offline_source() == "OFFLINE_MODE"

    # ...and the override beats it in both directions, so an operator can
    # force a live poll on a box whose environment says otherwise.
    feed.set_offline(False)
    assert feed.offline_mode() is False
    assert feed.offline_source() == "runtime-switch"


def test_substrate_keep_holds_the_loopback_substrate_while_going_offline(
    live_client, monkeypatch
):
    """The orchestrator boots siren with SIREN_SUBSTRATE=real, and siren's
    refusal beat is only ledgered by throughline in that mode.

    throughline is on LOOPBACK, and a scripted offline run allows loopback
    while forbidding the internet. Dropping to the mock by default is the
    safe thing; forcing it unconditionally would take the demo's refusal off
    the ledger to prevent a connection the run was never going to make. So
    `keep` exists, and it buys a loopback substrate — never an outbound poll.
    """
    monkeypatch.setenv("SIREN_SUBSTRATE", "real")

    body = live_client.post(
        "/feed/mode", json={"mode": "offline", "substrate": "keep"}).json()

    assert body["offline"] is True
    assert body["substrate_option"] == "keep"
    assert body["feed"]["source"] == "snapshot", "the FEED is offline regardless"
    assert "OFFLINE_MODE" in body["feed"]["label"]


def test_keep_does_not_re_enable_the_poll(live_client, armed):
    """`keep` is about the substrate, never about the 911 feed."""
    live_client.post("/feed/mode", json={"mode": "offline", "substrate": "keep"})

    assert live_client.post("/feed/refresh").json()["source"] == "snapshot"
    assert live_client.get("/pulse").status_code == 200


def test_the_default_still_drops_to_the_mock(live_client, monkeypatch):
    monkeypatch.setenv("SIREN_SUBSTRATE", "real")

    body = live_client.post("/feed/mode", json={"mode": "offline"}).json()

    assert body["substrate"] == "mock"
    assert body["substrate_option"] == "follow"


def test_an_unknown_substrate_option_is_refused(live_client):
    response = live_client.post(
        "/feed/mode", json={"mode": "offline", "substrate": "sometimes"})

    assert response.status_code == 422
    assert live_client.get("/healthz").json()["mode"] == "live", "unchanged"


def test_an_unknown_mode_is_refused_and_changes_nothing(live_client):
    before = live_client.get("/feed/mode").json()

    response = live_client.post("/feed/mode", json={"mode": "sort-of-offline"})

    assert response.status_code == 422
    assert live_client.get("/feed/mode").json()["offline"] == before["offline"]


def test_mode_is_required(live_client):
    assert live_client.post("/feed/mode", json={}).status_code == 422


def test_get_feed_mode_reports_the_switch_without_changing_it(live_client):
    body = live_client.get("/feed/mode").json()
    assert body["offline"] is False
    assert body["source"] == "default"
    assert "live polling is permitted" in body["guarantee"]
    assert live_client.get("/healthz").json()["mode"] == "live"
