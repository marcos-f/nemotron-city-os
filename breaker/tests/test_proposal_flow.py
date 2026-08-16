"""test://breaker/gate-holds-forever, approval-executes, ledger-walk-3hops.

The proposal flow's defining property is negative — nothing releases a held
dispatch except a decision carrying a human subject — so it is asserted twice:
behaviourally (it stays held however long you wait and however often you look)
and structurally (no timeout, deadline or auto-release exists in the code that
holds it).
"""

from __future__ import annotations

import io
import re
import tokenize
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from breaker.engine import EFFECT_TYPE, GridWatch, ProposalNotFound
from breaker.models import TelemetryReading
from breaker.telemetry import DIVERGENT_UNIT, fixture, reading_for
from breaker.throughline import (
    GateViolation,
    MockThroughlineClient,
    assert_held,
)

from conftest import drive, rule_registry

PACKAGE = Path(__file__).resolve().parents[1] / "breaker"
SUBJECT = "oidc|ruiz@nvidia-demo.example"


# ------------------------------------------------------------------ the hold

def test_divergence_produces_a_proposal_held_at_the_gate(held):
    watch, proposal = held
    assert proposal.status == "waiting_at_gate"
    assert proposal.unit_id == DIVERGENT_UNIT
    assert proposal.tick == 23
    assert proposal.divergence_type == "soc"
    assert proposal.magnitude > 5.0
    assert proposal.approval_id
    assert proposal.execution_count == 0
    assert watch.dispatches == []


def test_the_proposal_matches_the_contract_shape(held):
    _, proposal = held
    payload = proposal.model_dump(mode="json")
    for required in ("id", "divergence_type", "status"):
        assert payload[required] is not None
    assert payload["divergence_type"] in ("soc", "temperature")
    assert payload["status"] in ("proposed", "waiting_at_gate", "approved", "rejected")


def test_the_effect_is_irreversible_and_typed_as_a_dispatch(held):
    watch, proposal = held
    effect = watch.client.get_effect(proposal.effect_id)
    assert effect["effect_type"] == EFFECT_TYPE
    assert effect["reversibility"] == "irreversible"
    assert effect["status"] in ("queued", "waiting_at_gate")


def test_gate_holds_forever(held):
    """test://breaker/gate-holds-forever — an arbitrary wait changes nothing."""
    watch, proposal = held

    # Look again, and again, and from a clock far in the future. Nothing in
    # breaker is watching a deadline, so nothing can trip one.
    for _ in range(50):
        refreshed = watch.refresh(proposal.id)
        assert refreshed.status == "waiting_at_gate"
        assert refreshed.execution_count == 0

    import breaker.engine as engine

    far_future = datetime.now(timezone.utc) + timedelta(days=3650)
    original = engine._now
    engine._now = lambda: far_future
    try:
        for _ in range(10):
            assert watch.refresh(proposal.id).status == "waiting_at_gate"
    finally:
        engine._now = original

    assert watch.dispatches == []
    assert watch.client.get_effect(proposal.effect_id)["status"] == "queued"


def test_no_timeout_path_exists():
    """test://breaker/gate-holds-forever, structurally.

    Tokenised so that prose about *not* having a timeout is not mistaken for
    having one. breaker/throughline.py is checked separately below, because its
    HTTP client legitimately has a per-request read timeout.
    """
    banned = re.compile(
        r"^(timeout|deadline|expires?|expiry|ttl|auto_approve|auto_release|"
        r"auto_execute|max_wait|sleep|schedule|after_seconds)$",
        re.IGNORECASE,
    )
    offenders = []
    for module in ("engine.py", "rule.py", "models.py", "substrates.py", "service.py"):
        source = (PACKAGE / module).read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME and banned.match(token.string):
                offenders.append(f"{module}:{token.start[0]}: {token.line.strip()}")
    assert not offenders, "timeout path on the hold: " + "; ".join(offenders)


def test_the_only_timeout_in_the_package_is_an_http_read_timeout():
    """The one permitted use, pinned so it cannot grow into a release path."""
    source = (PACKAGE / "throughline.py").read_text(encoding="utf-8")
    uses = [line.strip() for line in source.splitlines()
            if re.search(r"\btimeout\b", line) and not line.strip().startswith("#")]
    assert uses, "expected the HTTP client's read timeout"
    for use in uses:
        assert "httpx" in use or "_timeout" in use or "timeout: float" in use, use
    # And it belongs to the HTTP client, not to the mock that models the hold.
    mock_section = source.split("class MockThroughlineClient")[1]
    assert "timeout" not in mock_section.lower()


def test_engine_never_sleeps_or_schedules():
    source = (PACKAGE / "engine.py").read_text(encoding="utf-8")
    assert "import time" not in source
    assert "threading.Timer" not in source
    assert "asyncio" not in source


# --------------------------------------------------------------- the release

def test_approval_executes(held):
    """test://breaker/approval-executes — released, executed, exactly once."""
    watch, proposal = held

    released = watch.decide(proposal.id, "approve", SUBJECT, "verified on scene")
    assert released.status == "approved"
    assert released.decided_by == SUBJECT
    assert released.execution_count == 1
    assert len(watch.dispatches) == 1

    dispatch = watch.dispatches[0]
    assert dispatch["unit_id"] == DIVERGENT_UNIT
    assert dispatch["released_by"] == SUBJECT
    assert dispatch["action"] == "load_shed"


def test_execution_happens_exactly_once_however_often_we_look(held):
    watch, proposal = held
    watch.decide(proposal.id, "approve", SUBJECT)
    for _ in range(25):
        watch.refresh(proposal.id)
    assert watch.get(proposal.id).execution_count == 1
    assert len(watch.dispatches) == 1


def test_a_decision_without_a_subject_is_refused(held):
    watch, proposal = held
    with pytest.raises(ValueError, match="decided_by"):
        watch.decide(proposal.id, "approve", "")
    assert watch.get(proposal.id).status == "waiting_at_gate"
    assert watch.dispatches == []


def test_a_rejected_proposal_never_dispatches(held):
    watch, proposal = held
    rejected = watch.decide(proposal.id, "reject", SUBJECT, "manual inspection first")
    assert rejected.status == "rejected"
    assert rejected.execution_count == 0
    assert watch.dispatches == []


def test_deciding_an_unknown_proposal_raises(watch):
    with pytest.raises(ProposalNotFound):
        watch.decide("prop-nope", "approve", SUBJECT)


# ------------------------------------------------------------------ the walk

def test_ledger_walk_3hops(held):
    """test://breaker/ledger-walk-3hops — effect → judgment → signal."""
    watch, proposal = held
    watch.decide(proposal.id, "approve", SUBJECT)

    walk = watch.walk(proposal.id)
    assert walk["hop_count"] == 3
    assert [hop["kind"] for hop in walk["hops"]] == ["effect", "judgment", "signal"]
    assert walk["complete"] is True

    ids = walk["ids"]
    assert ids["effect"] == proposal.effect_id
    assert ids["judgment"] == proposal.judgment_id
    assert ids["signal"] == proposal.signal_id
    # Traversable from breaker's own records, not just from the substrate's.
    assert [hop["id"] for hop in walk["hops"]] == [
        ids["effect"], ids["judgment"], ids["signal"]
    ]


def test_the_judgment_carries_the_rules_working(held):
    watch, proposal = held
    judgment = watch.client.judgments[proposal.judgment_id]
    assert judgment["signal_id"] == proposal.signal_id
    assert judgment["abstained"] is False
    assert judgment["substrate"] == "rule"
    assert any("soc_delta" in citation for citation in judgment["citations"])
    assert any("→" not in citation for citation in judgment["citations"])


# ------------------------------------------------------- the gate's own honesty

def test_breaker_refuses_to_dispatch_when_the_gate_did_not_hold():
    """A substrate that returns an executed irreversible effect is a violation."""

    class PermissiveGate(MockThroughlineClient):
        mode = "permissive-test-double"

        def post_effect(self, effect):
            record = super().post_effect(effect)
            record["status"] = "executed"     # the failure we must not accept
            return record

    watch = GridWatch(client=PermissiveGate(), registry=rule_registry())
    with pytest.raises(GateViolation, match="was not held"):
        drive(watch)

    assert watch.dispatches == []
    assert watch.gate_violations
    assert all(p.status != "approved" for p in watch.all_proposals())


def test_a_reversible_dispatch_is_also_a_violation():
    effect = {"id": "eff-1", "reversibility": "reversible", "status": "queued"}
    with pytest.raises(GateViolation, match="irreversible"):
        assert_held(effect, "test")


def test_one_open_proposal_per_unit(watch):
    """A diverging battery is one event, not one per tick after it."""
    drive(watch)
    for record in fixture():
        if record.unit_id == DIVERGENT_UNIT and record.tick > 23:
            watch.ingest(TelemetryReading(**record.as_dict()))
    assert len(watch.all_proposals()) == 1


def test_a_new_divergence_after_a_decision_proposes_again(watch):
    proposal = drive(watch)
    watch.decide(proposal.id, "reject", SUBJECT)
    watch.ingest(TelemetryReading(**reading_for(DIVERGENT_UNIT, 30).as_dict()))
    assert len(watch.all_proposals()) == 2


def test_the_decision_says_which_kind_of_caller_is_deciding():
    """throughline's gate refuses a decision that does not name the caller's
    role — an approval that does not say who is deciding is not an approval.

    breaker has one answer and no other: its only decide route demands an
    identified human's OIDC subject and refuses 422 without one, so the role it
    relays is `human`, sent on every decision rather than left to a default at
    the far end.
    """
    import httpx

    from breaker.throughline import DEFAULT_CALLER_ROLE, HttpThroughlineClient

    assert DEFAULT_CALLER_ROLE == "human"
    sent: dict = {}

    headers_sent: dict = {}

    def capture(method, url, json=None, timeout=None, headers=None):
        sent.update(json or {})
        headers_sent.update(headers or {})
        return httpx.Response(200, json={"approval": {}, "effect": {}})

    client = HttpThroughlineClient("http://gate.invalid")
    original = httpx.request
    httpx.request = capture
    try:
        client.decide("apr-1", "approve", SUBJECT, "because")
    finally:
        httpx.request = original

    assert sent["caller_role"] == "human"
    assert sent["decided_by"] == SUBJECT
    # A decide is on throughline's PRIVILEGED write surface: the gate
    # authenticates the caller before it will look at the decision at all.
    from breaker.throughline import CALLER_TOKEN_HEADER

    assert headers_sent[CALLER_TOKEN_HEADER] == "test-caller-token"


def test_the_mock_gate_records_the_role_the_real_gate_demands(held):
    """The mock may not be more permissive than the substrate it stands in for."""
    watch, proposal = held
    watch.decide(proposal.id, "approve", SUBJECT)
    approval = watch.client.approvals()[0]
    assert approval["caller_role"] == "human"
    assert approval["decided_by"] == SUBJECT
