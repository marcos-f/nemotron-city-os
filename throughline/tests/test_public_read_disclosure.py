"""P1 (issue #3): the audit-read surface is anonymous ON PURPOSE, and now
says so.

A review found ``/ledger``, ``/ledger/verify`` and ``/approvals`` all
returning 200 to an anonymous caller while the write path (``POST
/approvals/{id}/decide``) and the privileged effect/signal paths correctly
refuse one. Recorded decision: this is DELIBERATE public verifiability — the
product's differentiator, not a leak — and it must say so on the surface
itself rather than leave a reviewer to assume the worst. This must NOT gate
the surface: these tests assert the disclosure is present AND that the
endpoints still answer anonymously.
"""

from __future__ import annotations


def _assert_disclosure(notice: dict) -> None:
    assert notice["public"] is True
    # Machine-readable: a consumer can branch on this without parsing prose.
    assert isinstance(notice["surface"], list) and notice["surface"]
    assert isinstance(notice["excluded_from_public_read"], list)
    assert notice["excluded_from_public_read"], "must name what is excluded"
    # Human-readable: an actual sentence, not just a code.
    assert isinstance(notice["reason"], str) and len(notice["reason"]) > 40
    assert isinstance(notice["message_ui"], str) and len(notice["message_ui"]) > 10


def test_ledger_is_anonymous_and_discloses_it(anonymous_client):
    response = anonymous_client.get("/ledger")
    assert response.status_code == 200
    _assert_disclosure(response.json()["public_verification"])


def test_ledger_verify_is_anonymous_and_discloses_it(anonymous_client):
    response = anonymous_client.get("/ledger/verify")
    assert response.status_code == 200
    _assert_disclosure(response.json()["public_verification"])


def test_approvals_is_anonymous_and_discloses_it(anonymous_client):
    response = anonymous_client.get("/approvals")
    assert response.status_code == 200
    _assert_disclosure(response.json()["public_verification"])


def test_the_disclosure_names_what_stays_authenticated(anonymous_client):
    """The excluded list must actually name the write surface it excludes,
    not just assert vaguely that something is excluded."""
    notice = anonymous_client.get("/ledger").json()["public_verification"]
    joined = " ".join(notice["excluded_from_public_read"]).lower()
    assert "decide" in joined or "warrant" in joined or "authz" in joined


def test_the_disclosure_does_not_claim_coverage_beyond_throughlines_own_surfaces(
    anonymous_client,
):
    """Regression: an earlier disclosure asserted redaction covered "every
    other" surface in the federation, including ones throughline does not
    serve and cannot see (helm's /healthz, for one). Four independent
    reviewers falsified that in minutes. throughline may only promise what
    it can verify about ITS OWN responses — the whole product's thesis is
    that it never asserts what it cannot check."""
    notice = anonymous_client.get("/ledger").json()["public_verification"]
    excluded = " ".join(notice["excluded_from_public_read"]).lower()

    # The specific overclaim that shipped and was falsified: a guarantee
    # phrased to cover "every other" surface in the federation.
    assert "every other" not in excluded, (
        "disclosure must not claim redaction coverage over surfaces "
        "throughline does not serve"
    )
    # It must instead say the guarantee is scoped to this component.
    assert "throughline" in excluded and (
        "this component" in excluded or "its own process" in excluded
    ), "disclosure must scope the redaction guarantee to throughline itself"


def test_write_path_still_refuses_an_anonymous_decision(anonymous_client):
    """The disclosure does not weaken the write surface — it only labels the
    read surface that was already open."""
    response = anonymous_client.post("/approvals/apr-does-not-exist/decide", json={
        "decision": "approve", "decided_by": "oidc|test|anon",
    })
    assert response.status_code == 403
    assert response.json()["refused"] is True
