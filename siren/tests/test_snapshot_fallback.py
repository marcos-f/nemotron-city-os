"""SG1 — the feed degrades to cache and says so.

Cut order #2: live mode degrades first. These tests hold that promise to its
word — a failed poll must cost the demo a label, never a beat.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from siren import feed
from tests.conftest import SAMPLE_ROWS


def test_unmappable_rows_are_dropped_not_placed_at_null_island():
    incidents = [feed.record_to_incident(row) for row in SAMPLE_ROWS]
    assert incidents[2] is None, "a row with no coordinates cannot be mapped"
    assert [i.id for i in incidents if i] == ["F260115303", "F260115302"]
    assert incidents[0].incident_type == "Activated CO Detector"
    assert incidents[0].reported_at.endswith("Z")


def test_zero_zero_is_not_a_location():
    assert feed.record_to_incident(
        {"incident_number": "X", "latitude": "0", "longitude": "0"}
    ) is None


def test_live_poll_writes_a_snapshot_and_labels_itself_live(monkeypatch, data_dir):
    monkeypatch.delenv("OFFLINE_MODE", raising=False)
    incidents = [i for i in (feed.record_to_incident(r) for r in SAMPLE_ROWS) if i]
    monkeypatch.setattr(feed, "fetch_live", lambda **kw: incidents)

    state = feed.refresh(feed.FeedState())

    assert state.serving == "live"
    assert state.label == feed.LIVE_LABEL
    assert state.fetched_at is not None
    written = json.loads((data_dir / "snapshot.json").read_text())
    assert [row["id"] for row in written["incidents"]] == ["F260115303", "F260115302"]
    assert written["as_of"] == state.as_of


def test_failed_poll_serves_the_snapshot_with_its_own_as_of(monkeypatch, seeded_snapshot):
    """The fallback keeps the snapshot's timestamp. Re-stamping cached rows
    with 'now' is the specific lie this test exists to prevent."""
    monkeypatch.delenv("OFFLINE_MODE", raising=False)

    def unreachable(**kwargs):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(feed, "fetch_live", unreachable)

    state = feed.refresh(feed.FeedState())

    assert state.serving == "snapshot"
    assert state.label == feed.SNAPSHOT_LABEL
    assert state.as_of == "2026-08-15T14:42:00Z"
    assert state.fetched_at is None
    assert "URLError" in state.last_error
    assert len(state.incidents) == 2


def test_empty_live_response_falls_back_rather_than_serving_a_quiet_night(
    monkeypatch, seeded_snapshot
):
    monkeypatch.delenv("OFFLINE_MODE", raising=False)
    monkeypatch.setattr(feed, "fetch_live", lambda **kw: [])

    state = feed.refresh(feed.FeedState())

    assert state.serving == "snapshot"
    assert len(state.incidents) == 2
    assert state.last_error == "live feed returned no rows"


def test_corrupt_cache_falls_through_to_the_packaged_seed(data_dir, monkeypatch):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "snapshot.json").write_text("{ not json", encoding="utf-8")

    snapshot = feed.read_snapshot()

    assert snapshot is not None, "a corrupt cache must not take the service down"
    assert snapshot.incidents, "the packaged seed carries real rows"


def test_snapshot_write_is_atomic(data_dir, seeded_snapshot):
    """The temp file is renamed into place and never left behind."""
    assert (data_dir / "snapshot.json").exists()
    assert not (data_dir / "snapshot.json.tmp").exists()


def test_no_snapshot_anywhere_says_so(monkeypatch, data_dir):
    monkeypatch.setattr(feed, "read_snapshot", lambda: None)
    state = feed._serve_snapshot(feed.FeedState(), feed.OFFLINE_LABEL, error=None)
    assert state.incidents == []
    assert state.label == "no snapshot available"
    assert state.last_error


def test_incidents_endpoint_carries_the_as_of_label(client):
    response = client.get("/incidents")
    assert response.status_code == 200
    assert len(response.json()) == 2
    assert response.headers["x-siren-as-of"] == "2026-08-15T14:42:00Z"
    assert response.headers["x-siren-source"] == "snapshot"


def test_pulse_binds_rows_to_their_provenance(client):
    body = client.get("/pulse").json()
    assert body["status"]["source"] == "snapshot"
    assert "OFFLINE_MODE" in body["status"]["label"]
    assert body["status"]["as_of"] == "2026-08-15T14:42:00Z"
    assert body["status"]["incident_count"] == len(body["incidents"]) == 2


def test_a_parsed_timestamp_is_always_zoned():
    """A timestamp that exists is UTC and says so."""
    assert feed._parse_reported_at("2026-08-15T20:31:00.000").endswith("Z")


@pytest.mark.parametrize("raw", ["not-a-date", None, ""])
def test_an_unparseable_timestamp_is_null_not_now(raw):
    """This test used to assert the opposite, and the opposite was the bug.

    It required every input to come back with a ``Z``, which the old code
    satisfied by returning ``utcnow()`` for anything it could not parse. A
    row with no timestamp then rendered as the freshest incident on the
    board. Null is the honest answer; the caller pairs it with
    ``reported_at_missing`` and renders "time unknown".
    """
    assert feed._parse_reported_at(raw) is None
    parsed = feed.parse_reported_at(raw)
    assert parsed.utc is None and parsed.local is None
    assert parsed.missing is True
