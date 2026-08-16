"""test://helm/agent-refusal — the MCP host, and the one thing it cannot do."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import sign_in


def test_mcp_exposes_the_same_tools_the_console_uses(client: TestClient) -> None:
    tools = {t["function"]["name"] for t in client.get("/mcp/tools").json()["tools"]}
    assert {"get_overview", "list_approvals", "approve_effect"} <= tools
    assert {"read_ledger", "verify_ledger", "walk_effect", "list_feeds"} <= tools


def test_agent_holds_every_read_tool(client: TestClient, substrate) -> None:
    """The copilot is not crippled: it holds all read and reload tools."""
    substrate.propose("eff-read")
    for name in ("get_overview", "list_approvals", "read_ledger", "verify_ledger", "list_feeds"):
        response = client.post(
            "/mcp/call",
            json={"name": name, "arguments": {}, "principal": "agent:nemoclerk(m@t)"},
        )
        assert response.status_code == 200, f"{name}: {response.text}"
        assert response.json()["ok"] is True


def test_agent_holds_the_reload_tool(client: TestClient, substrate) -> None:
    response = client.post(
        "/mcp/call",
        json={
            "name": "reload_signal_class",
            "arguments": {"name": "incident"},
            "principal": "agent:nemoclerk(m@t)",
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_agent_refusal(client: TestClient, substrate) -> None:
    """test://helm/agent-refusal — approve on irreversible → REFUSED + ledgered.

    The beat is the ATTEMPT: the assistant reaches for the one tool it does
    not hold, is refused by ROLE, and its OWN principal goes on the chain.
    """
    approval_id = substrate.propose("eff-refuse", reversibility="irreversible")
    derived = client.get("/nemoclerk/runtime").json()["principal"]

    response = client.post(
        "/mcp/call",
        json={
            "name": "approve_effect",
            "arguments": {"id": approval_id, "decision": "approve"},
        },
    )
    assert response.status_code == 403
    payload = response.json()
    assert payload["refused"] is True
    assert payload["chip"] == "tool: approve_effect → REFUSED"
    assert payload["principal"] == derived
    assert derived in payload["summary"]

    # The effect did NOT happen.
    assert substrate.approvals[approval_id]["state"] == "pending"

    # The refusal is on the ledger, carrying the principal.
    refusals = [e for e in substrate.entries if e["type"] == "approval.refused"]
    assert len(refusals) == 1
    body = refusals[0]["body"]
    assert body["decided_by"] == derived
    assert body["caller_role"] == "agent"
    assert body["effect_id"] == "eff-refuse"


def test_a_cookieless_caller_cannot_name_the_principal_on_the_chain(
    client: TestClient, substrate
) -> None:
    """test://helm/agent-refusal — the third route to approval impersonation.

    ``POST /mcp/call`` has no session guard by design: it is how the agent
    reaches its own tools. It used to read the principal out of the REQUEST
    BODY, so a cookieless caller could pick the name throughline hash-chains
    forever — and, because the body also promoted itself to role "agent", it
    skipped the anonymous refusal and never tripped the impersonation check.
    """
    approval_id = substrate.propose("eff-forge", reversibility="irreversible")
    derived = client.get("/nemoclerk/runtime").json()["principal"]
    forged = "dana@nvidia-demo.example"

    response = client.post(
        "/mcp/call",
        json={
            "name": "approve_effect",
            "arguments": {"id": approval_id, "decision": "approve"},
            "principal": forged,
        },
    )
    assert response.status_code == 403
    assert response.json()["principal"] == derived
    assert substrate.approvals[approval_id]["state"] == "pending"

    # The chosen name is nowhere on the chain: not as the decider, not
    # anywhere else in the bodies helm wrote.
    for entry in substrate.entries:
        assert entry["body"].get("decided_by") != forged
    refusals = [e for e in substrate.entries if e["type"] == "approval.refused"]
    assert [r["body"]["decided_by"] for r in refusals] == [derived]


def test_an_agent_cannot_decide_a_reversible_approval_either(
    client: TestClient, substrate
) -> None:
    """throughline's gate blocks agents on IRREVERSIBLE effects only.

    A queued REVERSIBLE row would be decided, not refused, down there. Today
    no effects.yaml rule produces one; that is luck. helm refuses on role
    before it forwards, so the luck is not load-bearing.
    """
    approval_id = substrate.propose("eff-reversible", reversibility="reversible")
    response = client.post(
        "/mcp/call",
        json={"name": "approve_effect", "arguments": {"id": approval_id}},
    )
    assert response.status_code == 403
    assert response.json()["data"]["refusal_kind"] == "role"
    assert substrate.approvals[approval_id]["state"] == "pending"
    # Refused BEFORE the forward: the substrate was never asked to decide it.
    assert not [
        e for e in substrate.entries
        if e["body"].get("effect_id") == "eff-reversible"
        and e["type"].startswith("approval.decided")
    ]


def test_the_refusal_summary_survives_an_empty_principal(client: TestClient, substrate) -> None:
    """The refusal path used to reference an undefined name.

    ``summary`` read ``principal or decided_by``, and ``decided_by`` did not
    exist in that scope — a NameError, i.e. a 500 on beat 4, one empty
    principal away. The scripted path always had a principal, so it never
    fired.
    """
    approval_id = substrate.propose("eff-empty-principal", reversibility="irreversible")
    registry = client.app.state.registry
    result = registry.approve_effect(id=approval_id, principal="", role="agent")
    assert result.refused is True
    assert result.summary.startswith("REFUSED · ledgered as ")
    assert "anonymous" in result.summary or result.summary.split(" · ")[1].strip()


def test_agent_refusal_is_by_role_not_transport(client: TestClient, substrate) -> None:
    """A human session does NOT let a declared agent principal through."""
    approval_id = substrate.propose("eff-transport")
    sign_in(client, "dana@nvidia-demo.example", "admin")
    response = client.post(
        "/mcp/call",
        json={
            "name": "approve_effect",
            "arguments": {"id": approval_id},
            "principal": "agent:nemoclerk(m@t)",
        },
    )
    assert response.status_code == 403
    assert substrate.approvals[approval_id]["state"] == "pending"


def test_the_same_human_over_mcp_without_an_agent_principal_succeeds(
    client: TestClient, substrate
) -> None:
    """The gate judges the ROLE. An admin's own MCP session is an admin."""
    approval_id = substrate.propose("eff-human-mcp")
    sign_in(client, "dana@nvidia-demo.example", "admin")
    response = client.post(
        "/mcp/call", json={"name": "approve_effect", "arguments": {"id": approval_id}}
    )
    assert response.status_code == 200
    assert substrate.approvals[approval_id]["state"] == "approved"
    assert substrate.approvals[approval_id]["decided_by"] == "dana@nvidia-demo.example"


def test_approve_with_no_id_targets_what_is_waiting(client: TestClient, substrate) -> None:
    approval_id = substrate.propose("eff-implicit")
    response = client.post(
        "/mcp/call",
        json={"name": "approve_effect", "arguments": {}, "principal": "agent:nemoclerk(m@t)"},
    )
    assert response.status_code == 403
    assert response.json()["arguments"]["id"] == approval_id


def test_unknown_tool_is_reported_not_guessed(client: TestClient) -> None:
    response = client.post("/mcp/call", json={"name": "delete_ledger", "arguments": {}})
    assert response.status_code == 502
    assert "unknown tool" in response.json()["error"]


def test_refusal_shows_on_the_approval_record_page(client: TestClient, substrate) -> None:
    approval_id = substrate.propose("eff-shown")
    derived = client.get("/nemoclerk/runtime").json()["principal"]
    client.post("/mcp/call", json={"name": "approve_effect", "arguments": {"id": approval_id}})
    sign_in(client, "dana@nvidia-demo.example", "admin")
    page = client.get("/approval-detail").text
    assert 'id="agent-refusal"' in page
    assert derived in page
    assert "REFUSED VARIANT" in page
