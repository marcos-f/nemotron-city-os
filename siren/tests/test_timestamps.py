"""test://siren/timestamps — a Seattle wall clock is not UTC.

Reported by an independent Data Engineer review: ``_parse_reported_at``
knowingly stamped a ``Z`` on Socrata's floating LOCAL timestamps instead of
converting them. Live evidence at the time: ``as_of 2026-08-16T06:49:13Z``
against a newest ``reported_at 2026-08-15T23:42:00Z`` on a feed ordered
``datetime DESC`` — the freshest incident apparently seven hours old, which
is also seven hours in the *future* relative to when Seattle wrote it.

Two things made it dangerous rather than merely wrong. The values were
well-formed, so nothing downstream could detect the error; and every age
computation plus the on-disk snapshot inherited it. For a 911 feed, "how old
is this incident" is the entire question.

The second half of the same defect: a missing source timestamp was filled in
with ``utcnow()``. An undated row then sorted as the freshest thing on the
board. In a product whose thesis is that nothing is invented, that is not a
rounding error.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from siren import feed
from siren.models import Incident

#: Seattle is UTC-7 in August (PDT) and UTC-8 in January (PST). The bug was
#: worth exactly one of those, depending on the month, which is why the fix
#: converts through a named zone rather than subtracting a constant.
SUMMER_LOCAL = "2026-08-15T23:42:00.000"
SUMMER_UTC = "2026-08-16T06:42:00Z"
WINTER_LOCAL = "2026-01-15T23:42:00.000"
WINTER_UTC = "2026-01-16T07:42:00Z"


def test_a_summer_wall_clock_is_converted_not_relabelled():
    parsed = feed.parse_reported_at(SUMMER_LOCAL)

    assert parsed.utc == SUMMER_UTC, (
        "23:42 in Seattle in August is 06:42Z the next day; the bug emitted "
        "23:42Z, which is a seven-hour error dressed as a valid timestamp"
    )
    assert parsed.local == "2026-08-15T23:42:00-07:00"
    assert parsed.tz == "America/Los_Angeles"
    assert parsed.missing is False


def test_the_offset_follows_the_calendar_not_a_constant():
    """A hard-coded -7 would be wrong for half the year."""
    assert feed.parse_reported_at(WINTER_LOCAL).utc == WINTER_UTC
    assert feed.parse_reported_at(WINTER_LOCAL).local.endswith("-08:00")
    assert feed.parse_reported_at(SUMMER_LOCAL).local.endswith("-07:00")


def test_an_already_zoned_timestamp_is_respected():
    """If the source ever starts sending an offset, believe it."""
    assert feed.parse_reported_at("2026-08-16T06:42:00Z").utc == SUMMER_UTC
    assert feed.parse_reported_at("2026-08-15T23:42:00-07:00").utc == SUMMER_UTC


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-date", "2026-13-45"])
def test_a_missing_source_timestamp_is_null_and_flagged_not_invented(raw):
    parsed = feed.parse_reported_at(raw)

    assert parsed.utc is None, "utcnow() here made an undated row the freshest one"
    assert parsed.local is None
    assert parsed.missing is True


def test_an_undated_row_is_still_mapped_and_says_so():
    """Location and time fail differently: an unplaceable row has nowhere to
    go on a map, an undated one just does not know when."""
    incident = feed.record_to_incident({
        "incident_number": "F1", "type": "Aid Response",
        "latitude": "47.6", "longitude": "-122.3",
    })

    assert incident is not None
    assert incident.reported_at is None
    assert incident.reported_at_local is None
    assert incident.reported_at_missing is True
    assert incident.tz == "America/Los_Angeles"


def test_a_mapped_row_carries_both_representations_and_the_zone():
    incident = feed.record_to_incident({
        "incident_number": "F2", "type": "Brush Fire", "datetime": SUMMER_LOCAL,
        "latitude": "47.6", "longitude": "-122.3",
    })

    assert incident.reported_at == SUMMER_UTC
    assert incident.reported_at_local == "2026-08-15T23:42:00-07:00"
    assert incident.tz == "America/Los_Angeles"
    assert incident.reported_at_missing is False


# --------------------------------------------------- the age, as published


def _age_seconds(status: dict) -> int:
    as_of = datetime.strptime(status["as_of"], "%Y-%m-%dT%H:%M:%SZ")
    newest = datetime.strptime(status["newest_reported_at"], "%Y-%m-%dT%H:%M:%SZ")
    return int((as_of - newest).total_seconds())


def test_the_newest_incidents_age_is_plausible(monkeypatch, data_dir):
    """The regression assertion, stated as the reviewer stated the defect.

    The feed is ordered newest-first, so the newest row must be *behind* the
    as-of by a small amount. Under the bug this number was 25 633 seconds —
    7.12 hours — on live data. It must not be negative (a row from the
    future) and it must not be anywhere near a whole-hour offset.
    """
    monkeypatch.delenv("OFFLINE_MODE", raising=False)
    taken = datetime(2026, 8, 16, 7, 0, 0, tzinfo=timezone.utc)
    rows = [
        {"incident_number": "F1", "type": "Aid Response",
         "datetime": "2026-08-15T23:54:00.000",  # 06:54Z — six minutes old
         "latitude": "47.6", "longitude": "-122.3"},
        {"incident_number": "F2", "type": "Brush Fire",
         "datetime": "2026-08-15T23:30:00.000",
         "latitude": "47.61", "longitude": "-122.31"},
    ]
    incidents = [i for i in (feed.record_to_incident(r) for r in rows) if i]
    monkeypatch.setattr(feed, "fetch_live", lambda **kw: incidents)
    monkeypatch.setattr(feed, "utcnow", lambda: taken)

    status = feed.refresh(feed.FeedState()).status("mock").model_dump()

    age = _age_seconds(status)
    assert age == 360, "six minutes, which is what the source actually said"
    assert age >= 0, "a newest-first feed cannot lead its own as-of"
    assert abs(age - 7 * 3600) > 3600, "this is the seven-hour skew, back again"
    assert status["newest_age_seconds"] == age, "and siren publishes the number"
    assert status["tz"] == "America/Los_Angeles"
    assert status["undated_incidents"] == 0


def test_the_packaged_seed_snapshot_carries_corrected_timestamps():
    """The cache inherited the bug, so the shipped seed was re-cut.

    This guards the artifact, not the code: an offline demo serves this file,
    and a re-introduced seven-hour skew would be least visible exactly there.
    """
    raw = json.loads(feed.SEED_SNAPSHOT.read_text(encoding="utf-8"))

    assert raw["schema"] == feed.SNAPSHOT_SCHEMA, "re-cut, not merely migrated"
    assert raw["tz"] == "America/Los_Angeles"

    as_of = datetime.strptime(raw["as_of"], "%Y-%m-%dT%H:%M:%SZ")
    stamps = [r["reported_at"] for r in raw["incidents"] if r.get("reported_at")]
    assert stamps, "the seed carries dated rows"
    newest = datetime.strptime(max(stamps), "%Y-%m-%dT%H:%M:%SZ")
    age = (as_of - newest).total_seconds()

    assert age >= 0, "no incident may post-date the snapshot that contains it"
    assert age < 6 * 3600, f"newest row is {age / 3600:.2f}h behind the as-of"
    for row in raw["incidents"]:
        assert row.get("tz") == "America/Los_Angeles"
        if row.get("reported_at"):
            assert row["reported_at_local"], "both representations, always"


# ------------------------------------------------- the cache, brought forward


def _legacy_snapshot() -> dict:
    """A snapshot as it was written before the fix: local time stamped Z."""
    return {
        "as_of": "2026-08-16T07:00:00Z",
        "source": "seattle.fire.911",
        "dataset": feed.SOCRATA_DATASET,
        "incidents": [
            {"id": "F1", "incident_type": "Aid Response", "lat": 47.6,
             "lon": -122.3, "reported_at": "2026-08-15T23:54:00Z"},
        ],
    }


def test_a_pre_fix_snapshot_is_corrected_on_read():
    """Serving it unchanged would keep the seven-hour error alive offline,
    which is where it would be least visible."""
    snapshot = feed.Snapshot.from_dict(_legacy_snapshot())

    assert snapshot.migrated is True
    assert snapshot.incidents[0].reported_at == "2026-08-16T06:54:00Z"
    assert snapshot.incidents[0].reported_at_local == "2026-08-15T23:54:00-07:00"


def test_migration_is_idempotent():
    """A corrected snapshot must not be corrected again, or every restart
    would shift the demo another seven hours."""
    once = feed.Snapshot.from_dict(_legacy_snapshot())
    twice = feed.Snapshot.from_dict(json.loads(json.dumps(once.as_dict())))

    assert twice.migrated is False
    assert twice.incidents[0].reported_at == once.incidents[0].reported_at


def test_offline_ages_are_consistent_with_the_offline_as_of(monkeypatch, data_dir):
    """The interaction the reviewer flagged: offline serves the cache, so the
    cache's ages and the offline as-of label must agree."""
    monkeypatch.setenv("OFFLINE_MODE", "1")
    feed.write_snapshot(feed.Snapshot(
        as_of="2026-08-16T07:00:00Z",
        incidents=[Incident(**feed.migrate_incident_row(row, 1))
                   for row in _legacy_snapshot()["incidents"]],
        source="seattle.fire.911",
    ))

    state = feed.refresh(feed.FeedState())
    status = state.status("mock").model_dump()

    assert status["mode"] == "offline"
    assert status["source"] == "snapshot"
    assert status["as_of"] == "2026-08-16T07:00:00Z"
    assert _age_seconds(status) == 360, "six minutes, offline as well as live"
    assert status["snapshot_migrated"] is False, "written already corrected"


def test_an_undated_row_does_not_become_the_newest(monkeypatch, data_dir):
    """The fabrication bug's real cost: a row with no time sorted first."""
    monkeypatch.delenv("OFFLINE_MODE", raising=False)
    taken = datetime(2026, 8, 16, 7, 0, 0, tzinfo=timezone.utc)
    dated = feed.record_to_incident({
        "incident_number": "F1", "type": "Aid Response",
        "datetime": "2026-08-15T23:54:00.000",
        "latitude": "47.6", "longitude": "-122.3"})
    undated = feed.record_to_incident({
        "incident_number": "F2", "type": "Brush Fire",
        "latitude": "47.61", "longitude": "-122.31"})
    monkeypatch.setattr(feed, "fetch_live", lambda **kw: [undated, dated])
    monkeypatch.setattr(feed, "utcnow", lambda: taken)

    status = feed.refresh(feed.FeedState()).status("mock").model_dump()

    assert status["undated_incidents"] == 1
    assert status["newest_reported_at"] == "2026-08-16T06:54:00Z", (
        "the dated row is the newest; the undated one has no claim to the title"
    )
    assert status["incident_count"] == 2, "and the undated row is still served"


def test_the_seven_hour_skew_would_fail_this_suite():
    """A guard on the guard: reproduce the old behaviour and show it is caught.

    If this ever stops raising, the assertions above have gone soft.
    """
    old_style = datetime.fromisoformat(SUMMER_LOCAL.split(".")[0]).replace(
        tzinfo=timezone.utc
    )
    correct = datetime.fromisoformat(feed.parse_reported_at(SUMMER_LOCAL).utc[:-1])
    skew = correct.replace(tzinfo=timezone.utc) - old_style

    assert skew == timedelta(hours=7), (
        "the old code and the new differ by exactly the PDT offset, which is "
        "the whole defect"
    )
