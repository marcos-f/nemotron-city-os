"""test://signet/federation-steward — a franchise, not a superuser.

warrant's break-glass needs a quorum of two votes from admins of OTHER
datasets across at least two DISTINCT datasets. On a small federation no
such quorum can form, so `federation-steward` names the people who may
supply one.

Everything worth testing here is a NEGATIVE:

* it grants nothing — not admin, not approval, not any dataset authority
* nobody holds it by default, and that is not a gap to be filled
* a dataset admin cannot hand it out
* editing a console role does not move it, and signing in does not grant it

The empty case is the load-bearing one. If a fresh install shipped a
steward, every fresh install would have a break-glass quorum, and
`WARRANT-BREAKGLASS-NO-QUORUM` would be unreachable — which is the exact
silent lowering of the bar this design refuses.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helm.signet import (
    DEFAULT_GLOBAL_ROLE_HOLDERS,
    DEFAULT_ROLES,
    GLOBAL_ROLES,
    Identity,
    Signet,
)
from tests.conftest import sign_in

STEWARD = "federation-steward"


# ------------------------------------------------------- nobody holds it
def test_no_holder_is_seeded() -> None:
    """The empty roster is the shipped state, and it is deliberate."""
    assert DEFAULT_GLOBAL_ROLE_HOLDERS == ()
    for subject, row in DEFAULT_ROLES.items():
        assert row.get("global_role") == "", f"{subject} ships holding a global role"


def test_an_empty_roster_is_an_answer_not_a_gap(admin: TestClient) -> None:
    body = admin.get("/admin/global-roles").json()
    assert body["global_roles"] == list(GLOBAL_ROLES)
    assert body[STEWARD]["holders"] == []
    assert body[STEWARD]["count"] == 0
    assert "NO-QUORUM" in body[STEWARD]["note"]


def test_the_page_says_what_an_empty_roster_means(admin: TestClient) -> None:
    page = admin.get("/admin/stewards")
    assert page.status_code == 200
    assert "NOBODY HOLDS THIS ROLE" in page.text
    assert "WARRANT-BREAKGLASS-NO-QUORUM" in page.text


# ---------------------------------------------------------- it grants nothing
def test_the_franchise_grants_no_console_authority() -> None:
    steward = Identity(subject="kim@nvidia-demo.example", role="viewer", global_role=STEWARD)
    assert steward.is_federation_steward is True
    # A viewer who is a steward is still, in every respect, a viewer.
    assert steward.is_admin is False
    assert steward.may_approve is False
    assert steward.role == "viewer"


def test_a_console_admin_is_not_a_steward_by_implication() -> None:
    """The two axes do not leak into each other in either direction."""
    boss = Identity(subject="dana@nvidia-demo.example", role="admin")
    assert boss.is_admin is True
    assert boss.is_federation_steward is False
    assert boss.global_role == ""


def test_a_steward_viewer_still_cannot_approve(client: TestClient, substrate) -> None:
    sign_in(client, "kim@nvidia-demo.example", "viewer")
    signet = client.app.state.signet
    signet.set_global_role("kim@nvidia-demo.example", STEWARD, "test")
    assert signet.global_role_for("kim@nvidia-demo.example") == STEWARD

    approval_id = substrate.propose("eff-steward-viewer")
    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "kim@nvidia-demo.example",
            "caller_role": "human",
        },
    )
    assert response.status_code == 403


def test_a_steward_viewer_still_cannot_reach_admin(client: TestClient) -> None:
    sign_in(client, "kim@nvidia-demo.example", "viewer")
    client.app.state.signet.set_global_role("kim@nvidia-demo.example", STEWARD, "test")
    assert client.get("/admin").status_code == 403
    assert client.get("/admin/stewards").status_code == 403


# ------------------------------------------------------------ who may assign
def test_a_non_admin_cannot_hand_out_the_franchise(operator: TestClient) -> None:
    """The check the whole design rests on.

    Someone who could grant the franchise could manufacture the quorum that
    exists to check them.
    """
    response = operator.post(
        "/admin/global-roles",
        json={"subject": "kim@nvidia-demo.example", "global_role": STEWARD},
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["required"] == "admin"
    assert "dataset admin" in detail["reason"]


def test_a_viewer_cannot_hand_out_the_franchise(viewer: TestClient) -> None:
    response = viewer.post(
        "/admin/global-roles",
        json={"subject": "kim@nvidia-demo.example", "global_role": STEWARD},
    )
    assert response.status_code == 403


def test_an_admin_assigns_it_and_it_is_ledgered(admin: TestClient, substrate) -> None:
    response = admin.post(
        "/admin/global-roles",
        json={"subject": "ruiz@nvidia-demo.example", "global_role": STEWARD},
    )
    assert response.status_code == 200
    change = response.json()
    assert change["subject"] == "ruiz@nvidia-demo.example"
    assert change["old_global_role"] == ""
    assert change["new_global_role"] == STEWARD
    assert change["changed_by"] == "dana@nvidia-demo.example"
    assert change["ledgered"] is True

    signal = next(
        e for e in substrate.entries
        if e["body"].get("class") == "helm.admin.global_role_changed"
    )
    assert "ruiz@nvidia-demo.example" in signal["body"]["payload_ref"]
    assert STEWARD in signal["body"]["payload_ref"]

    holders = admin.get("/admin/global-roles").json()[STEWARD]["holders"]
    assert [h["subject"] for h in holders] == ["ruiz@nvidia-demo.example"]


def test_the_grant_records_whether_the_granter_was_verified(
    client: TestClient, substrate
) -> None:
    """A franchise handed out by an unverified identity must not read like
    one handed out by a verified identity — the same rule as a null issuer.
    """
    sign_in(client, "dana@nvidia-demo.example", "admin", provider="mock")
    unverified = client.post(
        "/admin/global-roles",
        json={"subject": "kim@nvidia-demo.example", "global_role": STEWARD},
    ).json()
    assert unverified["changed_by_verified"] is False
    assert unverified["changed_by_auth_mode"] == "unverified"
    assert unverified["changed_by_issuer"] == ""

    sign_in(
        client, "dana@nvidia-demo.example", "admin",
        provider="git.nemotron.example.com", issuer="https://git.nemotron.example.com",
    )
    verified = client.post(
        "/admin/global-roles",
        json={"subject": "ruiz@nvidia-demo.example", "global_role": STEWARD},
    ).json()
    assert verified["changed_by_verified"] is True
    assert verified["changed_by_issuer"] == "https://git.nemotron.example.com"

    # The two grants are distinguishable in the chain, not just in the reply.
    refs = [
        e["body"]["payload_ref"]
        for e in substrate.entries
        if e["body"].get("class") == "helm.admin.global_role_changed"
    ]
    assert any("auth_mode=unverified" in r for r in refs)
    assert any("issuer=https://git.nemotron.example.com" in r for r in refs)


def test_an_unknown_global_role_is_a_named_422(admin: TestClient) -> None:
    response = admin.post(
        "/admin/global-roles",
        json={"subject": "ruiz@nvidia-demo.example", "global_role": "superuser"},
    )
    assert response.status_code == 422
    assert "federation-steward" in response.json()["detail"]


def test_an_unknown_subject_is_a_404(admin: TestClient) -> None:
    response = admin.post(
        "/admin/global-roles",
        json={"subject": "nobody@nowhere.example", "global_role": STEWARD},
    )
    assert response.status_code == 404


def test_it_can_be_revoked(admin: TestClient) -> None:
    admin.post(
        "/admin/global-roles",
        json={"subject": "ruiz@nvidia-demo.example", "global_role": STEWARD},
    )
    cleared = admin.post(
        "/admin/global-roles",
        json={"subject": "ruiz@nvidia-demo.example", "global_role": ""},
    ).json()
    assert cleared["old_global_role"] == STEWARD
    assert cleared["new_global_role"] == ""
    assert admin.get("/admin/global-roles").json()[STEWARD]["holders"] == []


# -------------------------------------------------- the two axes stay apart
def test_a_console_role_edit_does_not_move_the_franchise(tmp_path) -> None:
    signet = Signet(tmp_path / "data", "secret")
    signet.ensure_subject("ruiz@nvidia-demo.example", "github", "ruiz")
    signet.set_global_role("ruiz@nvidia-demo.example", STEWARD, "dana")

    signet.set_role("ruiz@nvidia-demo.example", "viewer", "dana")
    assert signet.global_role_for("ruiz@nvidia-demo.example") == STEWARD

    signet.set_role("ruiz@nvidia-demo.example", "admin", "dana")
    assert signet.global_role_for("ruiz@nvidia-demo.example") == STEWARD


def test_signing_in_neither_grants_nor_drops_the_franchise(tmp_path) -> None:
    signet = Signet(tmp_path / "data", "secret")
    signet.ensure_subject("ruiz@nvidia-demo.example", "github", "ruiz")
    assert signet.global_role_for("ruiz@nvidia-demo.example") == ""

    signet.set_global_role("ruiz@nvidia-demo.example", STEWARD, "dana")
    # A later sign-in updates last_seen and provider; it must not touch this.
    signet.ensure_subject("ruiz@nvidia-demo.example", "git.nemotron.example.com", "Ruiz")
    assert signet.global_role_for("ruiz@nvidia-demo.example") == STEWARD


def test_a_row_written_before_the_field_existed_reads_as_holding_nothing(
    tmp_path,
) -> None:
    """Never absent, never unknown. Every row answers the same question."""
    signet = Signet(tmp_path / "data", "secret")
    signet.roles.write(
        {"legacy@nvidia-demo.example": {"provider": "github", "role": "operator"}}
    )
    row = next(
        r for r in signet.role_table() if r["subject"] == "legacy@nvidia-demo.example"
    )
    assert row["global_role"] == ""
    assert signet.global_role_for("legacy@nvidia-demo.example") == ""


def test_a_garbage_value_in_the_store_reads_as_holding_nothing(tmp_path) -> None:
    """Fails closed. A franchise nobody granted is a franchise nobody has."""
    signet = Signet(tmp_path / "data", "secret")
    signet.roles.write(
        {"x@nvidia-demo.example": {"role": "viewer", "global_role": "root"}}
    )
    assert signet.global_role_for("x@nvidia-demo.example") == ""


def test_the_session_reads_the_franchise_live_from_the_table(tmp_path) -> None:
    """Revocation takes effect on the next request, not on cookie expiry."""
    signet = Signet(tmp_path / "data", "secret")
    token = signet.issue("ruiz@nvidia-demo.example", "github", "ruiz")
    assert signet.identity_from_token(token).global_role == ""

    signet.set_global_role("ruiz@nvidia-demo.example", STEWARD, "dana")
    assert signet.identity_from_token(token).global_role == STEWARD

    signet.set_global_role("ruiz@nvidia-demo.example", "", "dana")
    assert signet.identity_from_token(token).is_federation_steward is False


# ------------------------------------------------------------------ the read
def test_warrant_can_read_the_roster_without_being_an_admin(operator: TestClient) -> None:
    """warrant READS this and never writes it, so the read is not admin-only."""
    body = operator.get("/admin/global-roles").json()
    assert body[STEWARD]["holders"] == []
    assert body[STEWARD]["assignable_by"] == "helm admin only; never by a dataset admin"


def test_the_roster_is_not_public(client: TestClient) -> None:
    assert client.get("/admin/global-roles").status_code == 401


def test_the_roster_page_names_people_and_warns_below_quorum(admin: TestClient) -> None:
    admin.post(
        "/admin/global-roles",
        json={"subject": "ruiz@nvidia-demo.example", "global_role": STEWARD},
    )
    page = admin.get("/admin/stewards").text
    assert "ruiz@nvidia-demo.example" in page
    # One steward cannot supply a two-dataset quorum on their own.
    assert "NO QUORUM FROM STEWARDS ALONE" in page

    admin.post(
        "/admin/global-roles",
        json={"subject": "kim@nvidia-demo.example", "global_role": STEWARD},
    )
    page = admin.get("/admin/stewards").text
    assert "kim@nvidia-demo.example" in page
    assert "NO QUORUM FROM STEWARDS ALONE" not in page


@pytest.mark.parametrize("path", ["/admin/stewards", "/profile"])
def test_the_franchise_is_named_in_the_ui_never_implicit(
    admin: TestClient, path: str
) -> None:
    admin.post(
        "/admin/global-roles",
        json={"subject": "dana@nvidia-demo.example", "global_role": STEWARD},
    )
    assert STEWARD in admin.get(path).text
