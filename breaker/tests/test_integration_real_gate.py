"""int+ — the proposal is HELD by the REAL throughline, and released on approval.

These tests need a real throughline on ``THROUGHLINE_URL`` (default :8600).
They are skipped when it is not there — and a skip is reported as exactly that
in `.build-status.json`, never as a pass. Run them with:

    THROUGHLINE_URL=http://127.0.0.1:8600 pytest tests/test_integration_real_gate.py

Nothing here is mocked: breaker posts to the real gate, the real gate holds the
real effect, and the release comes back through the real approval queue.
"""

from __future__ import annotations

import os

import pytest

from breaker.engine import GridWatch
from breaker.throughline import (
    HttpThroughlineClient,
    SubstrateUnreachable,
    assert_held,
)

from conftest import drive

THROUGHLINE_URL = os.environ.get("THROUGHLINE_URL", "http://127.0.0.1:8600")
SUBJECT = "oidc|ruiz@nvidia-demo.example"

#: The ATTESTATION an approver's console relays with the decision: which
#: authority authenticated them. Releasing an IRREVERSIBLE effect requires one
#: on the substrate's allowlist, whose DEFAULT is ``oidc`` alone — so these
#: tests deliberately exercise the STRICT default path and need no widening
#: env var to pass. ``issuer`` is not decorative: the gate refuses ``oidc``
#: without one, because a mode claiming an authority vouched for the approver
#: must name the authority.
ATTESTATION = {"auth_mode": "oidc", "issuer": "https://git.nemotron.example.com"}


def _reachable() -> bool:
    try:
        return HttpThroughlineClient(THROUGHLINE_URL).health().get("component") == "throughline"
    except SubstrateUnreachable:
        return False


#: In the integration job a skip would be a false green, so BREAKER_REQUIRE_REAL_GATE
#: turns "no substrate" into a failure instead of a skip.
REQUIRE_REAL_GATE = os.environ.get("BREAKER_REQUIRE_REAL_GATE") == "1"
REACHABLE = _reachable()

if REQUIRE_REAL_GATE and not REACHABLE:
    raise RuntimeError(
        f"BREAKER_REQUIRE_REAL_GATE=1 but no throughline answered at {THROUGHLINE_URL}. "
        "int+ cannot pass without the real gate."
    )

real_gate = pytest.mark.skipif(
    not REACHABLE, reason=f"no real throughline at {THROUGHLINE_URL}"
)


@pytest.fixture
def real_client() -> HttpThroughlineClient:
    return HttpThroughlineClient(THROUGHLINE_URL)


@pytest.fixture
def real_watch(real_client) -> GridWatch:
    return GridWatch(client=real_client)


@real_gate
def test_the_real_gate_identifies_itself(real_client):
    health = real_client.health()
    assert health["component"] == "throughline"
    assert health["ledger"]["valid"] is True


@real_gate
def test_proposal_is_held_by_the_real_gate(real_watch):
    """test://breaker/gate-holds-forever, against the real substrate."""
    proposal = drive(real_watch)
    assert proposal is not None
    assert proposal.gate_mode == "real"
    assert proposal.status == "waiting_at_gate"
    assert proposal.tick == 23

    effect = real_watch.client.get_effect(proposal.effect_id)
    assert_held(effect, "real")
    assert effect["reversibility"] == "irreversible"
    assert effect["status"] == "queued"

    # It stays held however often we look. The real gate has no deadline either.
    for _ in range(20):
        assert real_watch.refresh(proposal.id).status == "waiting_at_gate"
    assert real_watch.dispatches == []


@real_gate
def test_the_real_gate_refuses_a_release_without_a_subject(real_watch):
    proposal = drive(real_watch)
    with pytest.raises(SubstrateUnreachable) as excinfo:
        real_watch.client.decide(proposal.approval_id, "approve", "")
    assert "422" in str(excinfo.value)
    assert real_watch.refresh(proposal.id).status == "waiting_at_gate"
    assert real_watch.dispatches == []


@real_gate
def test_approval_executes_against_the_real_gate(real_watch):
    """test://breaker/approval-executes, against the real substrate."""
    proposal = drive(real_watch)
    released = real_watch.decide(
        proposal.id, "approve", SUBJECT, "integration test", **ATTESTATION
    )

    assert released.status == "approved"
    assert released.decided_by == SUBJECT
    assert released.execution_count == 1
    assert len(real_watch.dispatches) == 1

    for _ in range(10):
        real_watch.refresh(proposal.id)
    assert real_watch.get(proposal.id).execution_count == 1

    effect = real_watch.client.get_effect(proposal.effect_id)
    assert effect["status"] == "executed"


@real_gate
def test_ledger_walk_3hops_against_the_real_gate(real_watch):
    """test://breaker/ledger-walk-3hops, with the real ledger's hashes."""
    proposal = drive(real_watch)
    real_watch.decide(proposal.id, "approve", SUBJECT, **ATTESTATION)

    walk = real_watch.walk(proposal.id)
    assert walk["hop_count"] == 3
    assert [hop["kind"] for hop in walk["hops"]] == ["effect", "judgment", "signal"]
    assert walk["complete"] is True
    assert walk["all_hops_verified"] is True
    assert all(len(hop["sha256"]) == 64 for hop in walk["hops"])

    assert walk["ids"] == {
        "effect": proposal.effect_id,
        "judgment": proposal.judgment_id,
        "signal": proposal.signal_id,
    }
    assert real_watch.client.walk(proposal.effect_id)["hops"][0]["id"] == proposal.effect_id


@real_gate
def test_the_real_ledger_still_verifies_after_breaker_wrote_to_it(real_client):
    """breaker's records join the chain without breaking it."""
    assert real_client.health()["ledger"]["valid"] is True


@real_gate
def test_the_relay_guardrail_mirrored_the_hold(real_watch):
    """throughline mirrors the gate decision; breaker's dispatch shows up there."""
    proposal = drive(real_watch)
    entries = real_watch.client._request("GET", "/ledger?limit=200")["entries"]
    mirrored = [
        entry for entry in entries
        if entry["type"] == "relay.guardrail"
        and entry["body"].get("effect_id") == proposal.effect_id
    ]
    assert mirrored, "no guardrail record for breaker's dispatch"
    assert mirrored[-1]["body"]["allowed"] is False
    assert mirrored[-1]["body"]["rejected"] is True
