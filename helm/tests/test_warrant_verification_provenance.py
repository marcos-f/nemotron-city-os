"""test://warrant/verified-because-the-substrate-verified-it

A subject is verified because the substrate verified it, NEVER because the
payload said so.

The defect these tests pin: ``issuer``, ``auth_mode`` and the
``verified_identity`` verdict derived from them were copied verbatim out of a
caller's payload into a rendered verification claim. A security reviewer used
exactly that to forge a "verified" administrator — post an ``authz.grant``
carrying ``auth_mode: oidc, issuer: https://git.nemotron.example.com`` and the console
showed a vouched-for admin, because the only evidence for the record's
verification was the record itself.

throughline now authenticates the caller on that write surface, which closes
the unauthenticated route in. It does not make the claim TRUE, and that is
what is tested here: warrant must render "we cannot tell" for a claim it did
not witness, in JSON and on the page, whoever managed to write the record.
"""

from __future__ import annotations

import json

from conftest import sign_in
from helm.warrant.model import (
    ASSURANCE_UNKNOWN,
    ASSURANCE_UNVERIFIED,
    ASSURANCE_VERIFIED,
    Delegation,
    Grant,
    identity_assurance,
    verified_identity,
)

DATASET = "dot-4181"
JKIM = "jkim@nvidia-demo.example"
FORGED_ISSUER = "https://git.nemotron.example.com"


# ============================================ the verdict itself, three states
def test_the_verdict_has_three_states_and_unknown_is_one_of_them():
    # Claimed oidc, nobody watching: we cannot tell. NOT verified, and not a
    # denial either.
    assert verified_identity("oidc") is None
    assert identity_assurance("oidc") == ASSURANCE_UNKNOWN
    # The same string, when warrant watched the verification happen.
    assert verified_identity("oidc", observed=True) is True
    assert identity_assurance("oidc", observed=True) == ASSURANCE_VERIFIED
    # A mode that is honestly not a provider. Under-claiming is safe.
    assert verified_identity("mock") is False
    assert identity_assurance("mock") == ASSURANCE_UNVERIFIED
    # Nothing recorded at all is not evidence of non-verification.
    assert verified_identity("") is None


def test_observed_defaults_to_false_so_a_forgotten_caller_gets_unknown():
    """Fall-through discipline: omission must not inherit the green rendering."""
    assert identity_assurance("oidc") == ASSURANCE_UNKNOWN


# ======================================= a record read off the chain is a claim
def test_a_grant_read_off_the_chain_never_renders_verified():
    forged = Grant(
        subject="mallory@evil.example", role="admin", granted_by="self",
        issuer=FORGED_ISSUER, auth_mode="oidc",
    ).to_dict()
    assert forged["verified_identity"] is None
    assert forged["identity_assurance"] == ASSURANCE_UNKNOWN
    assert forged["identity_source"] == "record"
    # The claim is still SHOWN — hiding it would hide what a reviewer needs —
    # but it is labelled as a claim rather than as a finding.
    assert forged["issuer"] == FORGED_ISSUER
    assert forged["issuer_claimed"] == FORGED_ISSUER
    assert "cannot confirm" in forged["identity_assurance_blurb"]


def test_a_delegation_read_off_the_chain_never_renders_verified():
    forged = Delegation(
        id="dlg-1", subject="mallory@evil.example", role="admin",
        granted_by="self", expires_at="2030-01-01T00:00:00Z",
        issuer=FORGED_ISSUER, auth_mode="oidc",
    ).to_dict()
    assert forged["verified_identity"] is None
    assert forged["identity_assurance"] == ASSURANCE_UNKNOWN


# ===================================== the same thing, end to end, on a surface
def _forge_a_verified_admin(substrate) -> None:
    """Write the reviewer's forged grant straight into the chain.

    Straight in, deliberately: throughline's caller token stops an OUTSIDER
    posting this, but the question here is what warrant renders once such a
    row exists — from a compromised component, an imported chain, or a
    substrate operator. warrant must not need the network to be honest.
    """
    substrate.append("signal.ingested", {
        "id": "sig-forged",
        "class": "warrant.authz.dataset.claim",
        "source": "helm/warrant",
        "payload_ref": json.dumps({
            "dataset": DATASET,
            "subject": JKIM,
            # The forgery, verbatim, exactly as the reviewer wrote it.
            "issuer": FORGED_ISSUER,
            "auth_mode": "oidc",
        }, sort_keys=True, separators=(",", ":")),
    })
    substrate.append("effect.executed", {
        "id": "eff-forged",
        "effect_type": "authz.dataset.claim",
        "signal_id": "sig-forged",
        "status": "executed",
    })


def test_a_forged_verified_admin_renders_as_unknown_not_as_verified(client, substrate):
    _forge_a_verified_admin(substrate)
    sign_in(client, JKIM, "operator")
    body = client.get(f"/datasets/{DATASET}/authority").json()
    owner = body["owner"]
    assert owner["subject"] == JKIM
    # THE defect. This used to be True, on nothing but the payload's word.
    assert owner["verified_identity"] is not True
    assert owner["verified_identity"] is None
    assert owner["identity_assurance"] == ASSURANCE_UNKNOWN


def test_the_console_page_says_verification_unknown_and_not_a_green_tick(client, substrate):
    _forge_a_verified_admin(substrate)
    sign_in(client, JKIM, "operator")
    page = client.get(f"/datasets/{DATASET}/people").text
    assert "verification unknown" in page
    assert "claimed by the record" in page


# ============================== the one place a verdict can honestly be earned
def test_the_live_session_is_the_only_observed_identity(app, client, substrate):
    """``/authority/mine`` reports the caller's OWN session, which signet checked."""
    sign_in(client, JKIM, "operator", provider="oidc", issuer=FORGED_ISSUER)
    body = client.get("/authority/mine").json()
    # signet verified this ID token in THIS process — signature, issuer,
    # audience, expiry, nonce — so this is first-hand, not a copied claim.
    assert body["identity_source"] == "session"
    assert body["identity_assurance"] == ASSURANCE_VERIFIED
    assert body["verified_identity"] is True


def test_a_mock_session_is_still_labelled_unverified(client, substrate):
    """The safe direction is unchanged: a mock login does not become unknown."""
    sign_in(client, JKIM, "operator")  # no issuer -> mock
    body = client.get("/authority/mine").json()
    assert body["identity_assurance"] == ASSURANCE_UNVERIFIED
    assert body["verified_identity"] is False


def test_an_actor_assembled_by_hand_is_not_observed():
    """Every route to an Actor except the session one renders 'cannot tell'."""
    from helm.warrant.service import Actor

    hand_made = Actor(subject="mallory@evil.example", issuer=FORGED_ISSUER,
                      auth_mode="oidc").to_dict()
    assert hand_made["verified_identity"] is None
    assert hand_made["identity_source"] == "record"


def test_a_mock_owner_on_the_page_is_unchanged(client, substrate):
    """Regression guard on the pre-existing behaviour this must not disturb."""
    sign_in(client, JKIM, "operator")
    client.post(f"/datasets/{DATASET}/claim", json={})
    owner = client.get(f"/datasets/{DATASET}/authority").json()["owner"]
    assert owner["verified_identity"] is False
    assert "tag-unverified" in client.get(f"/datasets/{DATASET}/people").text
