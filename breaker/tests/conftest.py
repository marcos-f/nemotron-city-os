"""Shared fixtures. Tests run against the mock gate unless they say otherwise."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from breaker.app import create_app
from breaker.engine import GridWatch
from breaker.models import TelemetryReading
from breaker.service import BreakerService, Settings
from breaker.substrates import SubstrateRegistry, load_registry
from breaker.telemetry import DIVERGENT_UNIT, fixture
from breaker.throughline import MockThroughlineClient, read_caller_token

REPO_ROOT = Path(__file__).resolve().parents[1]


def rule_registry() -> SubstrateRegistry:
    """A registry pinned to the deterministic rule.

    Production now defaults to nemotron (config/substrates.yaml `active`),
    the NVIDIA substrate the bounty requires in the CONDITION slot. Most of
    this suite is not ABOUT substrate selection — it is about idempotency,
    resolution, the gate, caller roles — and those tests should not depend
    on network reachability of a model endpoint. So the shared fixtures pin
    the rule explicitly, the same way tests/test_model_substrate.py already
    pins ``dsv4`` for the tests that ARE about a model substrate.
    """
    registry = load_registry()
    registry.select("rule")
    return registry


#: throughline authenticates the CALLER on its privileged write surface, and
#: the MOCK gate enforces the same rule — a mock easier to satisfy than the
#: real thing is how integration surprises are manufactured. So the suite
#: holds a token like any other client. It goes into the ENVIRONMENT rather
#: than being injected, because that is the resolution path a deployed breaker
#: uses; a test that bypassed it would not be testing it.
#:
#: Tests that are ABOUT the missing-token path delete it themselves.
TEST_CALLER_TOKEN = "test-caller-token"


@pytest.fixture(autouse=True)
def enable_fixture_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test suite is the permitted place for the synthetic demo driver.

    POST /fixture/run is OFF by default in the product (see
    breaker.app.FIXTURE_DRIVE_ENV) — the operator directive permits
    mock/stub/synthetic submission only in tests or behind an explicit,
    non-default setting. This is that explicit setting, flipped on for the
    whole suite. Tests that are ABOUT the default-off behaviour delete it
    themselves (monkeypatch.delenv).
    """
    monkeypatch.setenv("BREAKER_ENABLE_FIXTURE_DRIVE", "1")


@pytest.fixture(autouse=True)
def caller_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """A token for the mocked suite — but NEVER over the top of a real one.

    This is a DEFAULT, not an override. int+ runs against a real substrate
    that minted its own token into ``<data-dir>/caller-token``, and clobbering
    the environment with a stand-in there made every real decide fail
    authentication — the suite would have been proving that breaker cannot
    talk to the gate while reporting it as a gate problem. So: resolve the
    token the way a deployed breaker resolves it, and only invent one when
    that resolution comes up empty.
    """
    resolved, _ = read_caller_token()
    if resolved:
        return resolved
    monkeypatch.setenv("THROUGHLINE_CALLER_TOKEN", TEST_CALLER_TOKEN)
    return TEST_CALLER_TOKEN


@pytest.fixture
def settings() -> Settings:
    return Settings(substrate_mode="mock")


@pytest.fixture
def service(settings: Settings) -> BreakerService:
    service = BreakerService(settings)
    service.registry.select("rule")  # see rule_registry() above
    return service


@pytest.fixture
def client(service: BreakerService):
    with TestClient(create_app(service=service)) as test_client:
        test_client.service = service
        yield test_client


@pytest.fixture
def watch() -> GridWatch:
    return GridWatch(client=MockThroughlineClient(), registry=rule_registry())


def drive(watch: GridWatch, ticks: int = 40, unit: str | None = None):
    """Push the fixture through a GridWatch, returning the first proposal."""
    for record in fixture(ticks):
        if unit is not None and record.unit_id != unit:
            continue
        outcome = watch.ingest(TelemetryReading(**record.as_dict()))
        if outcome["proposal"]:
            return watch.get(outcome["proposal"]["id"])
    return None


@pytest.fixture
def held(watch: GridWatch):
    """A proposal sitting at the gate, which is where they sit."""
    proposal = drive(watch)
    assert proposal is not None and proposal.status == "waiting_at_gate"
    return watch, proposal


__all__ = ["drive", "rule_registry", "DIVERGENT_UNIT", "REPO_ROOT"]
