"""test://signet/subject-required, test://signet/auth-flag-env,
test://helm/rbac-admin, test://helm/human-can-agent-cannot.

The gate is a ROLE gate. The same effect, the same transport: a human with a
signet role succeeds, an agent principal is refused.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import json

from helm.config import BootRefused, Settings, enforce_auth_flag_policy, load_settings
from helm.signet import SESSION_COOKIE
from tests.conftest import sign_in


# ------------------------------------------------- test://signet/subject-required
def test_subject_required(client: TestClient, substrate) -> None:
    """A decision without a subject is a 422 from the schema, before any policy."""
    approval_id = substrate.propose("eff-sub")
    response = client.post(f"/approvals/{approval_id}/decide", json={"decision": "approve"})
    assert response.status_code == 422
    body = response.json()
    assert any(
        err.get("loc", [])[-1] == "decided_by" for err in body["detail"]
    ), body


def test_empty_subject_is_also_rejected(client: TestClient, substrate) -> None:
    approval_id = substrate.propose("eff-empty")
    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": ""},
    )
    assert response.status_code == 422


def test_subject_lands_in_the_approval_record(operator: TestClient, substrate) -> None:
    """The SESSION subject lands in the record — an attestation may agree."""
    approval_id = substrate.propose("eff-record")
    response = operator.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "ruiz@nvidia-demo.example",  # matches the session
            "caller_role": "human",
            "rationale": "divergence confirmed",
        },
    )
    assert response.status_code == 200
    assert response.json()["approver_source"] == "session"
    record = operator.get(f"/approvals/{approval_id}").json()
    assert record["decided_by"] == "ruiz@nvidia-demo.example"
    assert record["state"] == "approved"
    assert record["rationale"] == "divergence confirmed"


# --------------------------------------------------------------- the role gate
def test_operator_may_approve_an_irreversible_effect(operator: TestClient, substrate) -> None:
    approval_id = substrate.propose("eff-op")
    response = operator.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "ruiz@nvidia-demo.example"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_viewer_may_not_decide(viewer: TestClient, substrate) -> None:
    approval_id = substrate.propose("eff-view")
    response = viewer.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "kim@nvidia-demo.example"},
    )
    assert response.status_code == 403
    assert "viewer" in response.json()["detail"]["reason"]
    assert substrate.approvals[approval_id]["state"] == "pending"


def test_human_can_agent_cannot(client: TestClient, substrate) -> None:
    """test://helm/human-can-agent-cannot — one effect, two callers.

    The agent is refused and the refusal is ledgered; the SAME effect is then
    approved by a human subject. Role, not transport.
    """
    approval_id = substrate.propose("eff-both")

    refused = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "agent:nemoclerk(deepseek-v4-flash-0731@dgx-spark)",
            "caller_role": "agent",
        },
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["refused"] is True
    assert substrate.approvals[approval_id]["state"] == "pending"
    refusals = [e for e in substrate.entries if e["type"] == "approval.refused"]
    assert len(refusals) == 1
    assert refusals[0]["body"]["decided_by"].startswith("agent:nemoclerk")

    sign_in(client, "dana@nvidia-demo.example", "admin")
    allowed = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "dana@nvidia-demo.example",
            "caller_role": "human",
        },
    )
    assert allowed.status_code == 200
    assert substrate.approvals[approval_id]["state"] == "approved"
    assert substrate.approvals[approval_id]["decided_by"] == "dana@nvidia-demo.example"


def test_unknown_approval_is_404(operator: TestClient) -> None:
    response = operator.post(
        "/approvals/apr-nope/decide",
        json={"decision": "approve", "decided_by": "ruiz@nvidia-demo.example"},
    )
    assert response.status_code == 404


# ------------------------------------------------ test://signet/auth-flag-env
@pytest.mark.parametrize("env", ["dev", "development", "test", "local"])
def test_auth_disabled_is_honoured_in_dev_and_test(env: str) -> None:
    settings = Settings(env=env, auth_disabled=True)
    enforce_auth_flag_policy(settings)  # must not raise
    assert settings.auth_disabled is True


@pytest.mark.parametrize("env", ["staging", "stage", "production", "prod"])
def test_auth_disabled_refuses_boot_in_protected_envs(env: str) -> None:
    settings = Settings(env=env, auth_disabled=True)
    with pytest.raises(BootRefused) as excinfo:
        enforce_auth_flag_policy(settings)
    message = str(excinfo.value)
    assert "AUTH_DISABLED" in message, "the refusal must NAME the flag"
    assert "REFUSED" in message
    assert env in message


def test_load_settings_refuses_the_unsafe_combination() -> None:
    with pytest.raises(BootRefused):
        load_settings({"HELM_ENV": "production", "AUTH_DISABLED": "1"})


#: A production environment with nothing in it that can mint an identity.
PRODUCTION_ENV = {
    "HELM_ENV": "production",
    "HELM_SESSION_SECRET": "a-real-secret-from-the-fleet-store",
    "HELM_OIDC_ISSUER": "https://git.nemotron.example.com",
    "HELM_OIDC_CLIENT_ID": "0123456789abcdef",
}


def test_production_boots_when_nothing_can_mint_an_identity() -> None:
    settings = load_settings(dict(PRODUCTION_ENV))
    assert settings.auth_disabled is False
    assert settings.oidc_configured is True


def test_auth_flag_state_is_reported_to_admin(admin: TestClient) -> None:
    body = admin.get("/admin/providers").json()
    assert body["auth_flag"]["flag"] == "AUTH_DISABLED"
    assert body["auth_flag"]["honoured_in"] == ["dev", "test"]


# ------------------------------------------------------ test://helm/rbac-admin
def test_rbac_admin_page_access(client: TestClient) -> None:
    """/admin is 200 for admin and 403 for operator, viewer and agent."""
    sign_in(client, "dana@nvidia-demo.example", "admin")
    assert client.get("/admin").status_code == 200

    for subject, role in (
        ("ruiz@nvidia-demo.example", "operator"),
        ("kim@nvidia-demo.example", "viewer"),
        ("agent:nemoclerk", "agent"),
    ):
        sign_in(client, subject, role)
        response = client.get("/admin")
        assert response.status_code == 403, f"{role} reached /admin"
        assert "not permitted" in response.text
        # 403 is never a dead end.
        assert "Back to console" in response.text


def test_rbac_admin_api_access(client: TestClient) -> None:
    sign_in(client, "dana@nvidia-demo.example", "admin")
    assert client.get("/admin/roles").status_code == 200
    for subject, role in (
        ("ruiz@nvidia-demo.example", "operator"),
        ("kim@nvidia-demo.example", "viewer"),
        ("agent:nemoclerk", "agent"),
    ):
        sign_in(client, subject, role)
        assert client.get("/admin/roles").status_code == 403
        assert client.get("/admin/gate-policy").status_code == 403
        assert client.get("/admin/providers").status_code == 403


def test_role_edit_produces_a_ledger_entry(admin: TestClient, substrate) -> None:
    before = len(substrate.entries)
    result = admin.post(
        "/admin/roles", json={"subject": "kim@nvidia-demo.example", "role": "operator"}
    ).json()
    assert result["old_role"] == "viewer"
    assert result["new_role"] == "operator"
    assert result["changed_by"] == "dana@nvidia-demo.example"
    assert result["ledgered"] is True
    assert len(substrate.entries) == before + 1
    entry = substrate.entries[-1]
    assert entry["type"] == "signal.ingested"
    assert entry["body"]["class"] == "helm.admin.role_changed"
    assert "kim@nvidia-demo.example" in entry["body"]["payload_ref"]
    assert "viewer->operator" in entry["body"]["payload_ref"]
    # and the change took effect
    assert admin.app.state.signet.role_for("kim@nvidia-demo.example") == "operator"


def test_role_edit_rejects_an_unknown_role(admin: TestClient) -> None:
    response = admin.post(
        "/admin/roles", json={"subject": "kim@nvidia-demo.example", "role": "superuser"}
    )
    assert response.status_code == 422


def test_sign_out_clears_the_session(client: TestClient) -> None:
    sign_in(client, "dana@nvidia-demo.example", "admin")
    client.post(
        "/nemoclerk/message", json={"message": "what is waiting?", "feature_area": "helm"}
    )
    store = client.app.state.clerk.sessions
    assert ("dana@nvidia-demo.example", "helm") in store.keys()

    response = client.get("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert ("dana@nvidia-demo.example", "helm") not in store.keys()

    client.cookies.delete(SESSION_COOKIE)
    assert client.get("/console", follow_redirects=False).status_code == 303


def test_admin_is_reachable_only_from_the_profile_menu(admin: TestClient) -> None:
    """Admin is NOT a tab; it lives in the ProfileMenu, and it always has an exit."""
    console = admin.get("/console").text
    tab_row = console.split('<nav class="tabs"')[1].split("</nav>")[0]
    assert "/admin" not in tab_row
    assert 'href="/admin"' in console  # in the user menu
    admin_page = admin.get("/admin").text
    assert "Back to console" in admin_page
    assert "Return to console" in admin_page


def test_the_gate_disable_control_is_inert(admin: TestClient) -> None:
    policy = admin.get("/admin/gate-policy").json()
    assert policy["disable_gate"]["available"] is False
    assert "refused at config load" in policy["disable_gate"]["reason"]
    page = admin.get("/admin").text
    assert "disable gate" in page
    assert "<button disabled" in page



# ============================================================================
# The approver is the SESSION SUBJECT — never a name the caller supplied.
#
# For a while it was the caller's string, forwarded untouched. An
# unauthenticated POST could write any name it liked into an approval
# record and the chain would hash it faithfully: integrity present,
# AUTHENTICITY ABSENT, which is the more dangerous failure because the
# record looks exactly as trustworthy as a true one.
# ============================================================================
def test_a_cookieless_decide_is_refused(client: TestClient, substrate) -> None:
    approval_id = substrate.propose("eff-anon")
    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "attacker@example.com"},
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["refusal_kind"] == "anonymous"
    assert substrate.approvals[approval_id]["state"] == "pending"
    assert substrate.approvals[approval_id]["decided_by"] is None


def test_a_cookieless_decide_never_reaches_the_substrate(
    client: TestClient, substrate
) -> None:
    """The refusal happens in helm; throughline is never asked."""
    approval_id = substrate.propose("eff-anon-2")
    before = len(substrate.entries)
    client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "attacker@example.com"},
    )
    decided = [e for e in substrate.entries if e["type"] == "approval.decided"]
    assert decided == []
    # ...but the REFUSAL is on the chain.
    assert len(substrate.entries) > before


def test_a_mismatched_attestation_is_refused_and_ledgered(
    client: TestClient, substrate
) -> None:
    approval_id = substrate.propose("eff-impersonate")
    sign_in(client, "ruiz@nvidia-demo.example", "operator")
    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "dana@nvidia-demo.example"},
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["refusal_kind"] == "impersonation"
    assert detail["claimed"] == "dana@nvidia-demo.example"
    assert detail["actual"] == "ruiz@nvidia-demo.example"
    assert substrate.approvals[approval_id]["state"] == "pending"

    # BOTH principals are on the chain: what was claimed and who claimed it.
    note = substrate.entries[-1]
    assert note["body"]["class"] == "helm.refusal"
    payload = note["body"]["payload_ref"]
    assert "dana@nvidia-demo.example" in payload
    assert "ruiz@nvidia-demo.example" in payload
    assert "impersonation" in payload


def test_the_ledgered_approver_is_always_the_session_subject(
    client: TestClient, substrate
) -> None:
    """Whatever the body says, the record says who was signed in."""
    approval_id = substrate.propose("eff-bound")
    sign_in(client, "ruiz@nvidia-demo.example", "operator")
    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "ruiz@nvidia-demo.example"},
    )
    assert response.status_code == 200
    assert substrate.approvals[approval_id]["decided_by"] == "ruiz@nvidia-demo.example"
    decided = [e for e in substrate.entries if e["type"] == "approval.decided"]
    assert decided[-1]["body"]["decided_by"] == "ruiz@nvidia-demo.example"


def test_no_ledger_entry_ever_names_an_unauthenticated_approver(
    client: TestClient, substrate
) -> None:
    approval_id = substrate.propose("eff-sweep")
    for body in (
        {"decision": "approve", "decided_by": "attacker@example.com"},
        {"decision": "approve", "decided_by": "root"},
        {"decision": "reject", "decided_by": "dana@nvidia-demo.example"},
    ):
        client.post(f"/approvals/{approval_id}/decide", json=body)
    chain = json.dumps(
        [e for e in substrate.entries if e["type"] == "approval.decided"]
    )
    assert "attacker@example.com" not in chain
    assert "root" not in chain


# --------------------------------------------- every refusal reaches the chain
def test_a_viewer_refusal_is_ledgered(viewer: TestClient, substrate) -> None:
    """The showcase refusal was on the chain; a real one was not."""
    approval_id = substrate.propose("eff-viewer-ledger")
    before = len(substrate.entries)
    response = viewer.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "kim@nvidia-demo.example"},
    )
    assert response.status_code == 403
    assert len(substrate.entries) == before + 1
    body = substrate.entries[-1]["body"]
    assert body["class"] == "helm.refusal"
    assert "kim@nvidia-demo.example" in body["payload_ref"]
    assert "role" in body["payload_ref"]


# ------------------------------- the policy belongs to the CAPABILITY, not a flag
@pytest.mark.parametrize("flag", ["AUTH_DISABLED"])
def test_every_identity_minting_flag_refuses_production(flag: str) -> None:
    """The guard was attached to one flag and routed around by its twin."""
    with pytest.raises(BootRefused) as excinfo:
        load_settings(dict(PRODUCTION_ENV, **{flag: "1"}))
    assert flag in str(excinfo.value)


def test_the_default_session_secret_refuses_production() -> None:
    """Shipping the built-in secret means anyone can forge a cookie."""
    with pytest.raises(BootRefused) as excinfo:
        load_settings({k: v for k, v in PRODUCTION_ENV.items()
                       if k != "HELM_SESSION_SECRET"})
    assert "HELM_SESSION_SECRET" in str(excinfo.value)


def test_the_capability_list_is_the_policy() -> None:
    """A flag added later is refused by default rather than being missed."""
    from helm.config import IDENTITY_MINTING_FLAGS, identity_minting_capabilities

    assert {f for f, _ in IDENTITY_MINTING_FLAGS} == {"AUTH_DISABLED"}
    settings = load_settings({"HELM_ENV": "dev", "AUTH_DISABLED": "1"})
    found = identity_minting_capabilities(settings)
    assert len(found) == 2  # the flag plus the default secret
    assert all(" — " in entry or "(" in entry for entry in found), (
        "a refusal you cannot act on is just an outage"
    )


def test_auth_mock_route_does_not_exist_anywhere(tmp_path, client: TestClient) -> None:
    """There is no mock/dev one-click sign-in as a product capability.

    ``/auth/mock`` used to be fenced by env; it is deleted now, so every
    environment gets the same 404 rather than a 403 that implies the
    capability still exists somewhere.
    """
    from helm.app import create_app

    # dev/test — where the old route used to succeed with a 303.
    assert client.get("/auth/mock", follow_redirects=False).status_code == 404

    for env in ("staging", "production"):
        app = create_app(
            Settings(
                env=env,
                data_dir=str(tmp_path / env),
                offline=True,
                agent_model_url="",
                fallback_model_url="",
                nvidia_api_key="",
                session_secret="a-real-secret",
            )
        )
        with TestClient(app) as probe:
            assert probe.get("/auth/mock", follow_redirects=False).status_code == 404
