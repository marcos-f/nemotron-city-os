"""An unreachable gate must be LEGIBLE, not merely survivable.

Reported by an independent SRE review: with throughline's gate dead,
``POST /fixture/run`` answered ``500 Internal Server Error`` with no body,
while ``GET /healthz`` answered 200 with ``"note": "Real throughline gate."``,
``"mode": "real"`` and an empty ``gate_violations``.

The nuance that makes this a reporting bug and not a safety bug: the
fail-closed invariant HELD. No proposal was emitted and no effect was created.
breaker behaved correctly and described itself wrongly. Every test in this
file therefore asserts BOTH halves — the new legibility *and* the unchanged
invariant — so a future "fix" to the reporting cannot quietly trade the
holding away.

The gate here is a real HTTP client pointed at a closed port, installed after
boot. That is the actual failure mode: a gate that answered at start-up and
died at 03:00. A service that only probes at boot cannot see it.
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from breaker.app import create_app
from breaker.service import BreakerService, Settings
from breaker.throughline import HttpThroughlineClient, MockThroughlineClient


def closed_port() -> int:
    """A port nothing is listening on: bound to find it, then released."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def dead_gate_url() -> str:
    return f"http://127.0.0.1:{closed_port()}"


@pytest.fixture
def dead_gate_service(dead_gate_url: str) -> BreakerService:
    """A service that booted fine and whose gate then died under it."""
    service = BreakerService(Settings(substrate_mode="mock"))
    # This file is about the GATE dying, not about judgment substrates — pin
    # the rule so a diverging window never reaches out to a live model
    # endpoint (see tests/conftest.py::rule_registry for the same reasoning).
    service.registry.select("rule")
    client = HttpThroughlineClient(dead_gate_url, timeout=1.0)
    service.client = client
    service.watch.client = client
    service.mock_reason = None
    service.settings.throughline_url = dead_gate_url
    service._probe_cache = None
    service._probe_at = 0.0
    assert service.gate_mode == "real", "the gate is CONFIGURED real and is dead"
    return service


@pytest.fixture
def dead_gate_client(dead_gate_service: BreakerService):
    with TestClient(create_app(service=dead_gate_service)) as test_client:
        test_client.service = dead_gate_service
        yield test_client


def assert_fail_closed(service: BreakerService) -> None:
    """The invariant the reviewer confirmed held, and which must keep holding."""
    assert service.watch.all_proposals() == [], "no proposal may be emitted"
    assert service.watch.dispatches == [], "no effect may be dispatched"


# ------------------------------------------------- (a) 503, not a bare 500


def test_fixture_run_is_503_naming_the_dependency(dead_gate_client):
    response = dead_gate_client.post("/fixture/run")

    assert response.status_code == 503, "a dead dependency is 503, never 500"
    body = response.json()
    assert body, "the reviewer got an EMPTY body; that is half the defect"
    assert body["error"] == "substrate_unreachable"
    assert body["dependency"] == "throughline"
    assert body["dependency_url"] == dead_gate_client.service.settings.throughline_url
    assert "throughline" in body["detail"]
    assert body["reason"], "the underlying transport error is reported, not swallowed"
    assert body["fail_closed"] is True
    assert body["gate"]["reachable"] is False

    assert_fail_closed(dead_gate_client.service)


def test_signals_telemetry_is_503_naming_the_dependency(dead_gate_client):
    """The contract path reports the same way the additive path does."""
    body = None
    for tick in range(1, 41):
        response = dead_gate_client.post("/signals/telemetry", json={
            "unit_id": "bess-03", "tick": tick,
            "soc_pct": max(5.0, 90.0 - tick * 3.0),
            "temp_c": 20.0 + tick * 1.9,
            "charge_current_a": max(0.5, 40.0 - tick * 2.0),
        })
        if response.status_code != 201:
            body = response.json()
            assert response.status_code == 503
            break
    assert body is not None, "divergence must eventually reach the dead gate"
    assert body["dependency"] == "throughline"
    assert_fail_closed(dead_gate_client.service)


def test_decide_on_an_unknown_proposal_is_still_404_not_503(dead_gate_client):
    """The handler must not turn every failure into a substrate failure."""
    response = dead_gate_client.post(
        "/proposals/prop-nope/decide", json={"decided_by": "dana@example"})
    assert response.status_code == 404


def test_gate_violation_is_502_with_a_reason(service):
    """A gate that answers but does not hold is a different failure, said so."""

    class LooseGate(MockThroughlineClient):
        mode = "real"

        def post_effect(self, effect):
            record = super().post_effect(effect)
            record["status"] = "executed"  # the gate did NOT hold it
            return record

    loose = LooseGate()
    service.client = loose
    service.watch.client = loose

    with TestClient(create_app(service=service)) as client:
        response = client.post("/fixture/run")

    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "gate_violation"
    assert body["fail_closed"] is True
    assert "not held" in body["detail"] or "irreversible" in body["detail"]
    assert service.watch.dispatches == [], "a violated gate dispatches nothing"


# ------------------------------------- (b) /healthz separates the two claims


def test_healthz_reports_the_gate_as_unreachable(dead_gate_client):
    response = dead_gate_client.get("/healthz")

    assert response.status_code == 200, (
        "the process is alive; liveness stays 200 so probes keep working"
    )
    body = response.json()
    gate = body["gate"]

    assert gate["mode"] == "real", "mode still says how the gate is CONFIGURED"
    assert gate["reachable"] is False, "and reachable says whether it ANSWERED"
    assert gate["dependency"] == "throughline"
    assert "NOT REACHABLE" in gate["note"]
    assert gate["note"] != "Real throughline gate.", (
        "this is the exact string the reviewer saw while the gate was dead"
    )
    assert "throughline" in gate["reachability"]["detail"]
    assert gate["reachability"]["label"] == "gate offline"

    assert body["status"] == "degraded"
    assert any("throughline" in line for line in body["degraded"])


def test_healthz_is_ok_when_the_gate_is_reachable(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["degraded"] == []
    assert body["gate"]["reachable"] is True
    assert body["gate"]["mode"] == "mock"
    assert "MOCK GATE" in body["gate"]["note"], "the mock is still labelled a mock"


def test_healthz_never_raises_when_the_gate_is_dead(dead_gate_client):
    """A health endpoint that can 500 is not a health endpoint."""
    for _ in range(3):
        assert dead_gate_client.get("/healthz").status_code == 200


def test_reachability_is_cached_rather_than_probed_per_read(dead_gate_service):
    probes = {"n": 0}
    original = dead_gate_service._probe_gate

    def counted():
        probes["n"] += 1
        return original()

    dead_gate_service._probe_gate = counted
    dead_gate_service._probe_cache = None

    first = dead_gate_service.gate_reachability()
    second = dead_gate_service.gate_reachability()

    assert probes["n"] == 1, "the second read comes from the cache, as helm's does"
    assert first["cached"] is False and second["cached"] is True
    assert second["cache_age_seconds"] >= 0.0

    forced = dead_gate_service.gate_reachability(force=True)
    assert probes["n"] == 2 and forced["cached"] is False


def test_the_503_body_carries_a_probe_taken_after_the_failure(dead_gate_client):
    """A cached 'reachable' from two seconds ago would contradict the 503."""
    dead_gate_client.service._probe_cache = {
        "reachable": True, "label": "online", "detail": "", "checked_at": "stale",
    }
    dead_gate_client.service._probe_at = 1e18  # a cache that would never expire

    body = dead_gate_client.post("/fixture/run").json()
    assert body["gate"]["reachable"] is False


# ------------------------- (c) audit: available is a claim, reachable is a probe


def test_healthz_separates_substrate_available_from_substrate_reachable(client):
    body = client.get("/healthz").json()
    substrate = body["substrate"]

    assert substrate["reachable"] is True, "the active rule runs in-process"
    by_id = {entry["id"]: entry for entry in substrate["registered"]}

    assert by_id["rule"]["available"] is True
    assert by_id["rule"]["reachable"] is True

    # cuOpt: registered, honestly unavailable, and therefore not reachable.
    assert by_id["cuopt"]["available"] is False
    assert by_id["cuopt"]["reachable"] is False

    # The model endpoint claims available: true in config. That is a claim
    # about permission, not about the DGX Spark answering — and /healthz must
    # not launder one into the other. Unprobed reads null, never true.
    assert by_id["dsv4"]["available"] is True
    assert by_id["dsv4"]["reachable"] is None
    assert "not probed" in by_id["dsv4"]["reachable_detail"]


def test_an_unreachable_active_model_endpoint_degrades_healthz(service):
    service.registry.select("dsv4")
    service._model_probe_cache = {
        "reachable": False, "detail": "connection refused", "model_served": False,
    }
    service._model_probe_at = 1e18

    with TestClient(create_app(service=service)) as client:
        body = client.get("/healthz").json()

    assert body["substrate"]["reachable"] is False
    assert body["status"] == "degraded"
    assert any("dsv4" in line for line in body["degraded"])
