"""The one principle, asserted once, across every surface.

Twelve of seventeen reviewers refused this build, and every refusal was the
same defect wearing a different hat: **the system asserted something it
could not verify.** It appeared in the approve path (the caller's name), the
gate count (a confident zero while blind), the stage labels (a human
signature over an auto-executed route), the offline pane (announcing the
substrate was never built), an admin action (a class "flowing" while its
component was down), and the ledgered model principal (a rung that had died).

So the rule is one rule, and this file is its test:

    A value has THREE states, never two: known-true, known-false, and
    UNKNOWN. Unknown may never render as a confident zero, empty, green or
    success — in any layer, on any surface.

The tests below take the whole federation away and then read every page and
every endpoint, asserting that nothing claims to know what it cannot.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from tests.conftest import sign_in

#: Phrases that assert knowledge. Any of these appearing while the source is
#: unreachable is the bug, whatever page it is on.
CONFIDENT_CLAIMS = (
    "nothing is held at the gate",
    "the gate is empty",
    "the loop is at rest",
    "queue empty",
    "0 waiting",
    "no entries",
    "the chain is empty",
    "designed, not built",  # true of blindspot ONLY
)

PAGES = (
    "/console",
    "/docket",
    "/breaker",
    "/siren",
    "/composed",
    "/approval-detail",
    "/admin",
)


@pytest.fixture
def blind(client: TestClient, substrate) -> TestClient:
    """Every sibling gone, including the substrate."""
    substrate.propose("eff-known", description="microgrid load-shed battery_4")
    client.get("/overview")  # one good read, so a last-known value exists
    substrate.online.clear()
    # the health cache is a real 3s behaviour; expire it rather than sleep
    client.app.state.console._status_at = 0.0
    client.app.state.console._ledger_at = 0.0
    sign_in(client, "dana@nvidia-demo.example", "admin")
    return client


# ============================================================ the displays
def test_no_page_claims_the_gate_is_empty_while_blind(blind: TestClient) -> None:
    for path in PAGES:
        page = blind.get(path).text
        lowered = page.lower()
        for claim in CONFIDENT_CLAIMS:
            if claim == "designed, not built":
                continue  # checked separately: it is true of blindspot
            assert claim not in lowered, f"{path} claims {claim!r} while blind"


def test_the_gate_says_unknown_and_cites_what_it_last_saw(blind: TestClient) -> None:
    page = blind.get("/console").text
    assert "gate state unknown" in page.lower()
    assert "not responding" in page.lower()
    assert "last seen" in page.lower()
    assert "not asserting that nothing is held" in page


def test_the_ledger_tail_does_not_read_as_an_empty_chain(blind: TestClient) -> None:
    page = blind.get("/console").text
    assert "ledger unreadable" in page.lower()
    assert "not asserting anything about the chain" in page


def test_only_blindspot_is_ever_called_never_built(blind: TestClient) -> None:
    """A blip on :8600 made the console announce its own substrate was a mock-up."""
    for path in ("/console", "/docket", "/breaker", "/siren"):
        page = blind.get(path).text
        assert "designed, not built" not in page, f"{path} called a built sibling a mock-up"
        assert 'data-reason="unreachable"' in page or "not responding" in page

    # /composed shows a pane per component, so blindspot's never_built copy
    # legitimately appears there — but only ever attached to blindspot.
    composed = blind.get("/composed").text
    for chunk in composed.split('<div class="offline-pane"')[1:]:
        pane = chunk[: chunk.index("</div>")] if "</div>" in chunk else chunk
        if "designed, not built" in pane:
            assert 'id="offline-blindspot"' in pane, (
                "a built sibling was called a mock-up on /composed"
            )

    built = blind.get("/blindspot").text
    assert "designed, not built" in built  # blindspot genuinely was not built
    assert 'data-reason="never_built"' in built


def test_the_unreachable_pane_cites_the_last_verified_head(blind: TestClient) -> None:
    page = blind.get("/console").text
    assert "is built and is not answering" in page
    assert "Last verified" in page


# ================================================================ the API
def test_the_api_never_reports_absence_as_emptiness(blind: TestClient) -> None:
    overview = blind.get("/overview").json()
    assert overview["gate_known"] is False
    assert overview["pending_approvals"] is None, "a blind gate reported a count"
    assert overview["ledger_known"] is False
    assert overview["ledger_valid"] is None, "a blind ledger reported validity"
    assert overview["chain_length"] is None


def test_the_approvals_endpoint_marks_itself_offline(blind: TestClient) -> None:
    """A consumer must not be able to read absence as an empty queue."""
    response = blind.get("/approvals")
    body = response.json()
    assert body["known"] is False
    assert body["pending"] is None
    assert "detail" in body


def test_the_ledger_endpoint_marks_itself_offline(blind: TestClient) -> None:
    body = blind.get("/ledger").json()
    assert body["online"] is False
    assert body["chain_length"] is None
    assert body["rows"] == []
    assert body["detail"]


# ============================================================== the actions
def test_an_action_never_reports_an_effect_that_did_not_happen(
    blind: TestClient,
) -> None:
    """The sharpest form of the rule: a false record of the world."""
    result = blind.post("/admin/classes/incident/reload").json()
    assert result["performed"] is False
    assert result["ok"] is False
    assert "DID NOT RUN" in result["summary"]
    assert result["counts"] == {}, "an action counted things that did not happen"
    assert all(step["state"] != "ok" for step in result["steps"])
    assert "✓" not in result["chip"]


def test_a_reload_honours_the_class_it_was_asked_for(client: TestClient, substrate) -> None:
    """Every class returned a byte-identical body reporting incident results."""
    sign_in(client, "dana@nvidia-demo.example", "admin")
    incident = client.post("/admin/classes/incident/reload").json()
    telemetry = client.post("/admin/classes/telemetry/reload").json()
    assert incident != telemetry
    assert telemetry["performed"] is False
    assert "breaker" in telemetry["detail"]
    assert "no reload surface" in telemetry["detail"]
    assert incident["target"] == "incident"
    assert telemetry["target"] == "telemetry"


def test_a_reload_surfaces_the_substrate_it_actually_used(
    client: TestClient, substrate
) -> None:
    sign_in(client, "dana@nvidia-demo.example", "admin")
    result = client.post("/admin/classes/incident/reload").json()
    assert result["substrate"], "the substrate never reached the screen"
    if result["substrate"] != "real":
        assert result["substrate"] in result["summary"]


# ============================================================== the labels
def test_a_reversible_route_never_claims_a_human_signature(
    client: TestClient, substrate
) -> None:
    """docket showed 'human-signed' beside its own 'auto-executes' banner."""
    sign_in(client, "dana@nvidia-demo.example", "admin")
    page = client.get("/docket").text
    assert "human-signed" not in page
    assert "auto — reversible" in page
    assert "no signature required (reversible)" in page


def test_an_irreversible_effect_keeps_the_human_signature_copy(
    client: TestClient, substrate
) -> None:
    approval_id = substrate.propose("eff-signed")
    sign_in(client, "ruiz@nvidia-demo.example", "operator")
    client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "ruiz@nvidia-demo.example"},
    )
    page = client.get("/approval-detail").text
    assert "signed by ruiz@nvidia-demo.example" in page


# ========================================================== the assistant
def test_the_assistant_reports_unknown_rather_than_zero(
    blind: TestClient,
) -> None:
    answer = blind.post(
        "/nemoclerk/message",
        json={"message": "what's waiting for approval?", "feature_area": "helm"},
    ).json()
    text = answer["text"].lower()
    assert "unreachable" in text or "not responding" in text or "cannot" in text
    assert not re.search(r"\b0 approval", text)
    for chip in answer["chips"]:
        if not chip["ok"]:
            assert "unreachable" in chip["summary"] or "no " in chip["summary"]


# ====================================================== the whole surface
def test_no_endpoint_returns_a_confident_zero_while_blind(blind: TestClient) -> None:
    """A sweep, so a new endpoint cannot quietly reintroduce the bug."""
    schema = blind.get("/openapi.json").json()
    checked = 0
    for path, operations in schema["paths"].items():
        if "get" not in operations or "{" in path:
            continue
        if path.startswith(("/auth", "/logout", "/docs", "/openapi")):
            continue
        response = blind.get(path)
        if response.status_code >= 400:
            continue
        checked += 1
        body = response.json()
        if not isinstance(body, dict):
            continue
        blob = json.dumps(body)
        # Any count that survived a total outage must be accompanied by a
        # known-flag saying it is real.
        for key in ("pending_approvals", "chain_length", "pending"):
            if key in body and isinstance(body[key], int):
                assert body.get("known") is True or body.get(f"{key}_known") is True or (
                    body.get("gate_known") is True
                ), f"{path} reported {key}={body[key]} while every source was down: {blob[:200]}"
    assert checked >= 4, "the sweep did not actually reach the endpoints"


# ============================================== the substrate's own hardening
def test_a_held_reload_is_not_reported_as_success(client: TestClient, substrate) -> None:
    """A reversibility DOWNGRADE is itself an irreversible effect.

    throughline holds it at the gate (202 + config.downgrade_held) rather
    than applying it. Reporting that as success is the same class of lie as
    reporting an unreachable component as reloaded.
    """
    sign_in(client, "dana@nvidia-demo.example", "admin")
    substrate.hot_reload_status = 202
    substrate.hot_reload_body = {"state": "config.downgrade_held", "detail": "rules[2]"}
    result = client.post("/admin/classes/incident/reload").json()
    assert result["performed"] is False
    assert result["ok"] is False
    assert "HELD AT THE GATE" in result["detail"]
    assert "✓" not in result["chip"]
    states = {s["step"]: s["state"] for s in result["steps"]}
    assert states["flowing"] == "skipped"


def test_a_null_issuer_is_never_shown_as_verified(client: TestClient, substrate) -> None:
    """`issuer` and `auth_mode` are null until the OIDC work lands."""
    approval_id = substrate.propose("eff-issuer")
    sign_in(client, "ruiz@nvidia-demo.example", "operator")
    client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "ruiz@nvidia-demo.example"},
    )
    page = client.get("/approval-detail").text
    assert "not recorded" in page
    assert "issuer" in page.lower()


def test_substrate_health_is_a_third_state_too(blind: TestClient) -> None:
    """Reachable and healthy are different questions."""
    page = blind.get("/admin").text
    assert "Substrate health" in page
    assert "cannot report its" in page


# ================================== accepted / refused / HELD is three-valued
def test_a_siren_held_swap_is_not_reported_as_accepted(
    client: TestClient, substrate
) -> None:
    """siren reported "the substrate accepted the swap" for a HELD swap.

    Same bug shape as unknown-vs-empty: a three-valued outcome collapsed
    into two. The operator is told a class is flowing while the swap waits
    at the gate for that same operator.
    """
    sign_in(client, "dana@nvidia-demo.example", "admin")
    substrate.hot_reload_status = 202
    substrate.hot_reload_body = {"state": "held", "approval_id": "apr-swap-1"}
    result = client.post("/admin/classes/incident/reload").json()
    assert result["performed"] is False
    assert "HELD AT THE GATE" in result["detail"]
    assert "apr-swap-1" in result["detail"], "a held swap must name its approval"
    assert all(s["state"] != "ok" for s in result["steps"] if s["step"] == "flowing")


def test_the_assistant_never_calls_a_held_swap_flowing(
    client: TestClient, substrate
) -> None:
    substrate.hot_reload_status = 202
    substrate.hot_reload_body = {"state": "held", "approval_id": "apr-swap-2"}
    sign_in(client, "dana@nvidia-demo.example", "admin")
    answer = client.post(
        "/nemoclerk/message",
        json={"message": "reload the incident class", "feature_area": "siren"},
    ).json()
    chip = next(c for c in answer["chips"] if c["name"] == "reload_signal_class")
    assert chip["ok"] is False
    assert "DID NOT RUN" in chip["summary"] or "HELD" in chip["summary"].upper()
    text = answer["text"].lower()
    # the word may appear in a NEGATION ("nothing is flowing"); what must
    # never appear is the affirmative claim
    for claim in ("is flowing", "registered and flowing", "class is now flowing"):
        assert claim not in text.replace("nothing is flowing", ""), text[:160]
