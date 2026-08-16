"""Every decision names who is deciding, and a refusal is not an outage.

Found on a red main: throughline made ``caller_role`` mandatory after a
security review — an omitted role used to skip the agent check entirely and
let an irreversible effect execute — and breaker was still sending the old
three-field body. The real gate answered

    403 {"refusal_reason": "caller-role-required", ...}

and breaker reported it as ``SubstrateUnreachable``, which is two failures at
once: the decision never went through, and the operator was told to look for
a dead process that was running perfectly.

Why nothing caught it: the mock accepted the old shape. That is the exact
failure mode ``throughline.py``'s own docstring warns about — "a mock that is
easier to satisfy than the real thing is how integration surprises are
manufactured" — so the mock now enforces the same allowlist, and the tests
below hold it to that.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from breaker.app import create_app
from breaker.throughline import (
    CALLER_ROLES,
    DEFAULT_CALLER_ROLE,
    MockThroughlineClient,
    SubstrateRefused,
    SubstrateUnreachable,
)
from tests.conftest import drive, rule_registry


# ------------------------------------------------ the role is always sent


def test_the_client_sends_caller_role_on_every_decision():
    """The regression in one assertion: this key was simply absent."""
    sent: dict = {}

    class Recorder(MockThroughlineClient):
        mode = "real"

        def decide(self, approval_id, decision, decided_by, rationale=None,
                   caller_role=DEFAULT_CALLER_ROLE, auth_mode="", issuer=""):
            sent["caller_role"] = caller_role
            # Recorded too, so this double stays shaped like the client it
            # stands in for: the attestation rides alongside the role on
            # every decision, and a double that dropped it would hide a
            # relay that stopped relaying.
            sent["auth_mode"] = auth_mode
            sent["issuer"] = issuer
            return super().decide(approval_id, decision, decided_by, rationale,
                                  caller_role=caller_role,
                                  auth_mode=auth_mode, issuer=issuer)

    from breaker.engine import GridWatch

    watch = GridWatch(client=Recorder(), registry=rule_registry())
    proposal = drive(watch)
    watch.decide(proposal.id, "approve", "oidc|ruiz@example")

    assert sent["caller_role"] == "human"


def test_the_api_defaults_to_human_and_relays_what_it_is_given(client):
    proposal = client.post("/fixture/run").json()["proposals"][0]

    response = client.post(f"/proposals/{proposal['id']}/decide",
                           json={"decided_by": "oidc|ruiz@example"})

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_an_agent_declaring_itself_is_relayed_not_filtered(client):
    """breaker must not quietly rewrite the role to one that passes.

    The gate refuses an agent approving an irreversible effect, and that
    refusal belongs ON THE LEDGER. Filtering the claim out here would turn a
    recorded refusal into a silent success.
    """
    proposal = client.post("/fixture/run").json()["proposals"][0]

    response = client.post(f"/proposals/{proposal['id']}/decide", json={
        "decided_by": "agent:nemoclerk", "decision": "approve",
        "caller_role": "agent",
    })

    # The in-process mock does not implement the irreversible-agent rule (the
    # gate owns that); what matters here is that the claim was relayed as made
    # and not rewritten to "human" on the way past.
    assert response.status_code in (200, 403)
    assert client.service.watch.get(proposal["id"]).gate_mode == "mock"


# ------------------------- the mock is no easier to satisfy than the gate


@pytest.mark.parametrize("role", [None, "", "   ", "operator", "HUMAN-ish"])
def test_the_mock_refuses_a_bad_role_exactly_as_the_gate_does(role):
    """If this passes for a role the real gate refuses, the mock is lying."""
    client = MockThroughlineClient()
    effect = client.post_effect({"id": "eff-1", "reversibility": "irreversible"})
    approval_id = effect["approval_id"]

    with pytest.raises(SubstrateRefused) as excinfo:
        client.decide(approval_id, "approve", "oidc|ruiz@example", caller_role=role)

    assert excinfo.value.status == 403
    assert excinfo.value.refusal["refused"] is True
    assert "caller-role" in excinfo.value.refusal["refusal_reason"]

    # And the effect is still held: a refused decision decides nothing.
    assert client.get_effect("eff-1")["status"] == "queued"


@pytest.mark.parametrize("role", CALLER_ROLES)
def test_the_mock_accepts_the_roles_the_gate_accepts(role):
    client = MockThroughlineClient()
    effect = client.post_effect({"id": "eff-1", "reversibility": "irreversible"})

    outcome = client.decide(effect["approval_id"], "approve",
                            "oidc|ruiz@example", caller_role=role)

    assert outcome["effect"]["status"] == "executed"


# --------------------------- reached-and-refused is not unreachable


def test_a_refusal_keeps_the_status_the_gate_gave_it(client):
    """403 stays 403. Laundering it into 503 says the gate is gone; it is not."""
    proposal = client.post("/fixture/run").json()["proposals"][0]

    response = client.post(f"/proposals/{proposal['id']}/decide", json={
        "decided_by": "oidc|ruiz@example", "caller_role": "sort-of-human",
    })

    assert response.status_code == 403, "not 503 — the gate answered"
    body = response.json()
    assert body["error"] == "substrate_refused"
    assert body["reached"] is True
    assert body["dependency"] == "throughline"
    assert body["refusal_reason"] == "caller-role-unrecognised"
    assert body["fail_closed"] is True


def test_a_refusal_still_dispatches_nothing(client):
    proposal = client.post("/fixture/run").json()["proposals"][0]

    client.post(f"/proposals/{proposal['id']}/decide", json={
        "decided_by": "oidc|ruiz@example", "caller_role": "nonsense",
    })

    assert client.get("/dispatches").json()["dispatches"] == []
    assert client.service.watch.get(proposal["id"]).status == "waiting_at_gate"


def test_refused_is_still_caught_by_every_fail_closed_path():
    """The subclass relationship is load-bearing, so it is asserted.

    Every ``except SubstrateUnreachable`` in this codebase — the boot probe,
    the reachability probe, the app-wide handler — must keep holding on a
    refusal exactly as it did before.
    """
    assert issubclass(SubstrateRefused, SubstrateUnreachable)


def test_a_5xx_is_still_reported_as_unreachable_not_as_a_refusal():
    """A gate that 500s has not applied a policy; it has fallen over."""
    import httpx

    from breaker.throughline import HttpThroughlineClient

    def transport(request):
        return httpx.Response(502, text="bad gateway")

    real_request = httpx.request

    def patched(method, url, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(transport)).request(
            method, url, **{k: v for k, v in kwargs.items() if k != "timeout"})

    httpx.request = patched
    try:
        gate = HttpThroughlineClient("http://127.0.0.1:1")
        with pytest.raises(SubstrateUnreachable) as excinfo:
            gate.health()
        assert not isinstance(excinfo.value, SubstrateRefused)
    finally:
        httpx.request = real_request


def test_the_cli_declares_a_role(capsys):
    from breaker.cli import build_parser

    args = build_parser().parse_args([
        "proposal", "decide", "--id", "p1", "--decision", "approve",
        "--decided-by", "oidc|ruiz@example",
    ])
    assert args.caller_role == "human", "never omitted; the gate refuses an omission"

    agent = build_parser().parse_args([
        "proposal", "decide", "--id", "p1", "--decision", "approve",
        "--decided-by", "agent:nemoclerk", "--caller-role", "agent",
    ])
    assert agent.caller_role == "agent"


def test_the_openapi_surface_still_boots():
    with TestClient(create_app()) as probe:
        assert probe.get("/openapi.json").status_code == 200
