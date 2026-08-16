"""The incident map's layout, asserted as geometry rather than by eye.

A label that overprints another label is unreadable, and a marker that
crosses the map frame is clipped. Both shipped once. These tests make the
regression impossible to reintroduce quietly.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from helm.views import MAP_PLOT, PIN_PITCH, _classify, _incident_list, _place_incidents
from tests.conftest import sign_in

# Real rows from siren's live Seattle Fire feed, including the cluster around
# Pioneer Square that produced the collision.
LIVE_ROWS = [
    {"id": "F260115336", "incident_type": "Medic Response- 6 per Rule",
     "lat": 47.576212, "lon": -122.419013, "address": "3201 Alki Ave Sw"},
    {"id": "F260115335", "incident_type": "Aid Response",
     "lat": 47.601718, "lon": -122.331581, "address": "223 Yesler Way"},
    {"id": "F260115334", "incident_type": "Natural Gas Odor",
     "lat": 47.577732, "lon": -122.411994, "address": "3003 62nd Ave Sw"},
    {"id": "F260115333", "incident_type": "Aid Response",
     "lat": 47.599968, "lon": -122.328965, "address": "308 4th Ave S"},
    {"id": "F260115332", "incident_type": "Aid Response",
     "lat": 47.650562, "lon": -122.349849, "address": "3515 Fremont Ave N"},
    {"id": "F260115331", "incident_type": "Illegal Burn",
     "lat": 47.547001, "lon": -122.271952, "address": "5600 S Bangor St"},
]


def test_no_two_pins_collide() -> None:
    markers = _place_incidents(LIVE_ROWS)
    assert len(markers) == 6
    for a, b in ((a, b) for i, a in enumerate(markers) for b in markers[i + 1:]):
        distance = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
        assert distance >= PIN_PITCH - 1, (
            f"pins {a['n']} and {b['n']} overlap: {distance:.1f}px apart"
        )


def test_pins_stay_inside_the_map_frame() -> None:
    """Nothing may cross the right edge — that clipped an id once."""
    x0, x1, y0, y1 = MAP_PLOT
    for marker in _place_incidents(LIVE_ROWS):
        assert x0 <= marker["x"] <= x1, f"pin {marker['n']} left the plot box"
        assert y0 <= marker["y"] <= y1, f"pin {marker['n']} left the plot box"
        # the widest glyph is the diamond at 12px half-width
        assert marker["x"] + 12 <= 620, f"pin {marker['n']} crosses the right edge"
        assert marker["x"] - 12 >= 0, f"pin {marker['n']} crosses the left edge"


def test_a_dense_cluster_still_separates() -> None:
    """Twenty calls on one block must not become one blob."""
    cluster = [
        {"id": f"F{i:09d}", "incident_type": "Aid Response",
         "lat": 47.6018 + (i % 3) * 0.0002, "lon": -122.3320 + (i % 2) * 0.0002}
        for i in range(20)
    ]
    markers = _place_incidents(cluster, limit=12)
    assert len(markers) == 12
    for a, b in ((a, b) for i, a in enumerate(markers) for b in markers[i + 1:]):
        assert math.hypot(a["x"] - b["x"], a["y"] - b["y"]) >= PIN_PITCH - 1


def test_pins_are_numbered_from_one_without_gaps() -> None:
    markers = _place_incidents(LIVE_ROWS)
    assert [m["n"] for m in markers] == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize(
    ("kind", "shape"),
    [
        ("Aid Response", "circle"),
        ("Medic Response- 6 per Rule", "circle"),
        ("Rescue Elevator", "circle"),
        ("MVI - Motor Vehicle Incident", "square"),
        ("Brush Fire", "triangle"),
        ("Illegal Burn", "triangle"),
        ("Auto Fire Alarm", "triangle"),
        ("Natural Gas Odor", "diamond"),
        ("Activated CO Detector", "diamond"),
    ],
)
def test_type_is_encoded_by_shape_over_the_real_vocabulary(kind: str, shape: str) -> None:
    """Every marker was a circle because the tidy category words never matched."""
    assert _classify(kind)[0] == shape


def test_more_than_one_shape_appears_on_a_real_feed() -> None:
    shapes = {m["shape"] for m in _place_incidents(LIVE_ROWS)}
    assert len(shapes) >= 3, f"the type encoding is dead: only {shapes}"


def test_state_is_encoded_by_colour() -> None:
    rows = [dict(LIVE_ROWS[0]), dict(LIVE_ROWS[1], status="closed")]
    markers = _place_incidents(rows)
    assert markers[0]["tone"] == "amber"
    assert markers[1]["tone"] == "muted"


def test_siren_serves_a_bare_list_and_an_envelope() -> None:
    assert len(_incident_list(LIVE_ROWS)) == 6
    assert len(_incident_list({"incidents": LIVE_ROWS})) == 6
    assert _incident_list(None) == []


def test_the_map_carries_no_inline_marker_labels(admin: TestClient) -> None:
    """The ids belong in the keyed table, not overprinted on the map."""
    page = admin.get("/siren").text
    svg = page.split('id="incident-map"', 1)[1].split("</svg>", 1)[0]
    districts = {
        "PUGET SOUND", "I-5", "BALLARD", "FREMONT", "U-DISTRICT", "QUEEN ANNE",
        "S. LAKE UNION", "CAPITOL HILL", "BELLTOWN", "DOWNTOWN", "CENTRAL DIST",
        "SODO", "BEACON HILL", "RAINIER",
    }
    for chunk in svg.split("<text")[1:]:
        text = chunk.split(">", 1)[1].split("<")[0].strip()
        if text in districts or text.endswith("ACTIVE"):
            continue  # the map's own furniture
        assert text.isdigit(), f"a marker label is back on the map: {text!r}"
        assert len(text) <= 2, f"a pin number should be short, got {text!r}"

    # An incident id anywhere outside a <title> tooltip means a label came back.
    drawn = "".join(part.split("</title>")[-1] for part in svg.split("<title>"))
    assert "F26" not in drawn


def test_the_key_table_lists_every_pin(admin: TestClient) -> None:
    page = admin.get("/siren").text
    assert 'id="incident-key"' in page
    assert page.count('class="pin-key') >= 1


def test_pins_do_not_sit_on_the_maps_own_lettering() -> None:
    """A pin on a district name splits it: "S. L(5)KE UNION"."""
    from helm.views import _on_a_label

    for marker in _place_incidents(LIVE_ROWS):
        assert not _on_a_label(marker["x"], marker["y"]), (
            f"pin {marker['n']} at ({marker['x']}, {marker['y']}) sits on a district label"
        )


def test_separating_pins_wins_when_the_map_is_full() -> None:
    """Two merged pins are a worse defect than one obscured word."""
    import math

    cluster = [
        {"id": f"F{i}", "incident_type": "Aid Response",
         "lat": 47.612, "lon": -122.335}
        for i in range(10)
    ]
    markers = _place_incidents(cluster, limit=10)
    assert len(markers) == 10
    for a, b in ((a, b) for i, a in enumerate(markers) for b in markers[i + 1:]):
        assert math.hypot(a["x"] - b["x"], a["y"] - b["y"]) >= PIN_PITCH - 1
