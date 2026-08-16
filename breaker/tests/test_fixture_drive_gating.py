"""Issue #3 — POST /fixture/run must not auto-submit synthetic proposals.

A review found a 346-deep backlog of near-duplicate SYNTHETIC battery_4
load-shed approvals sitting in a live approval queue, oldest ~10 hours,
generated continuously with no throttling. The generator was
``POST /fixture/run``: mounted unconditionally on every boot, unauthenticated,
and driving the entire synthetic fixture (breaker/telemetry.py) through
intake — and hence into a real, irreversible dispatch proposal at
throughline's gate — on every call, with nothing requiring an operator to
have deliberately asked for a demo run.

Per the operator directive, synthetic data is permitted in the PRODUCT only
behind an explicitly-labelled, non-default setting. These tests pin that:
the endpoint refuses by default, and only runs — and stays bounded — once an
operator has explicitly opted in.
"""

from __future__ import annotations

from breaker.telemetry import DIVERGENT_UNIT


def test_fixture_run_is_disabled_by_default(client, monkeypatch):
    """The generator must not run unless an operator explicitly asked for it.

    The suite's autouse ``enable_fixture_drive`` fixture flips this on for
    every other test — that IS the "permitted in tests" half of the
    directive. This test undoes it, to assert the PRODUCT default is off.
    """
    monkeypatch.delenv("BREAKER_ENABLE_FIXTURE_DRIVE", raising=False)

    response = client.post("/fixture/run")

    assert response.status_code == 403
    body = response.json()
    assert "BREAKER_ENABLE_FIXTURE_DRIVE" in body["detail"]

    # And, just as important: nothing was submitted to the gate. A 403 that
    # still leaked a proposal through would be no fix at all.
    assert client.service.watch.all_proposals() == []
    assert client.service.watch.dispatches == []


def test_fixture_run_stays_bounded_once_enabled(client, monkeypatch):
    """Enabled, it must still not grow an unbounded backlog on repeat calls.

    breaker's idempotency key — unit:tick:kind (GridWatch._idempotency_key) —
    bounds this: a repeat observation of the same divergence event UPDATES
    the existing proposal instead of minting a near-duplicate, so the second
    run's re-observation of battery_4's tick-23 divergence reports back the
    SAME proposal id it reported the first time, not a second one. This is
    the failure mode the review actually found: 216 near-identical
    "Load-shed dispatch for battery_4" proposals from exactly this endpoint
    being re-driven against a running service.
    """
    monkeypatch.setenv("BREAKER_ENABLE_FIXTURE_DRIVE", "1")

    first = client.post("/fixture/run")
    assert first.status_code == 200
    assert len(first.json()["proposals"]) == 1
    first_id = first.json()["proposals"][0]["id"]

    second = client.post("/fixture/run")
    assert second.status_code == 200
    # Whatever the second run reports back for battery_4, it must be a
    # RE-OBSERVATION of the same proposal — never a second, distinct one.
    assert all(p["id"] == first_id for p in second.json()["proposals"]), (
        "a second run must not raise a second near-duplicate proposal for "
        "the same unit while the first is still open"
    )

    proposals = [
        p for p in client.service.watch.all_proposals() if p.unit_id == DIVERGENT_UNIT
    ]
    assert len(proposals) == 1, "the backlog must not grow across repeated runs"
    assert proposals[0].id == first_id
