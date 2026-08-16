"""P0 (issue #9): GET /approvals — and every other anonymous-reachable
route — named the DECIDER to a caller who presented no credential at all.

A review found ``decided_by`` values like ``oidc|github|dana``,
``dana@nvidia-demo.example`` and ``attacker@example.com`` in a completely
anonymous ``GET /approvals`` response (~241KB, 356+ records). The panel's
finding drew a distinction this suite holds onto: the hash chain and its
verification are LEGITIMATELY public — a third party must be able to check
chain integrity with no account, and that is this product's differentiator,
not a bug. WHO decided is a different fact from WHETHER the chain is intact.
Publishing the decider's identity is not required for verifiability and is
not defensible for a public-sector deployment.

The fix (``throughline/redact.py::scrub_identity``) pseudonymizes
``decided_by`` (and its mirror ``approved_by``) on every unauthenticated
response, plus any bare email address wherever it appears, with a keyed
HMAC so the token is stable (two entries sharing a decider still visibly
share one pseudonym) and non-reversible without the substrate's own caller
token. It reuses the same middleware, and the same route enumeration, that
``tests/test_no_local_leak.py`` built to sweep the whole live route table
rather than a hand-picked sample — see ``_anonymous_get_routes`` there.
"""

from __future__ import annotations

from pathlib import Path

from test_no_local_leak import _anonymous_get_routes

REPO_ROOT = Path(__file__).resolve().parents[1]
GOOD_CONFIG = REPO_ROOT / "config" / "effects.yaml"

#: The exact values the review found leaking, plus a plain name a human
#: reviewer would recognise on sight.
KNOWN_DECIDER_EMAIL = "dana@nvidia-demo.example"
KNOWN_ATTACKER_EMAIL = "attacker@example.com"
KNOWN_OIDC_SUBJECT = "oidc|github|dana"

IRREVERSIBLE = {
    "reversibility": "irreversible",
    "status": "proposed",
    "effect_type": "payment.release",
    "description": "release funds",
}


def _queue(client, effect_id: str) -> str:
    signal_id = f"sig-{effect_id}"
    client.post("/signals", json={
        "id": signal_id, "class": "demo.signal", "source": "test",
        "real_or_synthetic": "real",
    })
    client.post("/effects", json={
        **IRREVERSIBLE, "id": effect_id, "signal_id": signal_id,
    })
    approvals = client.get("/approvals").json()["approvals"]
    return next(a["id"] for a in approvals if a["effect_id"] == effect_id)


def _decide_and_refuse(client) -> None:
    """Put a mix of decider identities on the chain: one attested APPROVE
    (so an ``approval.decided`` and an ``effect.executing``/``approved_by``
    entry both land), one unattested attempt from a different subject (so an
    ``approval.refused`` entry — whose ``reason`` text quotes the subject —
    also lands), matching the exact shapes the review's finding named."""
    approved_id = _queue(client, "eff-redact-approved")
    decided = client.post(f"/approvals/{approved_id}/decide", json={
        "decision": "approve",
        "decided_by": KNOWN_OIDC_SUBJECT,
        "caller_role": "human",
        "issuer": "https://git.nemotron.example.com",
        "auth_mode": "oidc",
    })
    assert decided.status_code == 200, decided.text

    refused_id = _queue(client, "eff-redact-refused")
    refused = client.post(f"/approvals/{refused_id}/decide", json={
        "decision": "approve",
        "decided_by": KNOWN_ATTACKER_EMAIL,
        "caller_role": "human",
    })
    assert refused.status_code == 403, refused.text

    also_decided_id = _queue(client, "eff-redact-email")
    decided2 = client.post(f"/approvals/{also_decided_id}/decide", json={
        "decision": "reject",
        "decided_by": KNOWN_DECIDER_EMAIL,
        "caller_role": "human",
    })
    assert decided2.status_code == 200, decided2.text


def test_no_anonymous_response_contains_decider_identity(anonymous_client, client):
    """Sweep the WHOLE enumerated route table — not a sample — the way
    ``test_no_anonymous_reachable_route_leaks_a_path_or_internal_hostport``
    does for filesystem paths, and assert none of it names a decider."""
    _decide_and_refuse(client)

    effect = client.post("/effects", json={
        "id": "eff-redact-walk-probe",
        "effect_type": "notify.operator",
        "description": "walk probe",
    }).json()
    dataset_id = client.get("/datasets").json()["datasets"][0]["id"]
    concrete = {
        "/effects/{id}": f"/effects/{effect['id']}",
        "/effects/{id}/walk": f"/effects/{effect['id']}/walk",
        "/datasets/{id}": f"/datasets/{dataset_id}",
    }

    app_paths = _anonymous_get_routes(anonymous_client.app)
    assert app_paths, "route enumeration returned nothing — fixture is broken"

    for template in app_paths:
        url = concrete.get(template, template)
        body = anonymous_client.get(url, params={"limit": 1000}).text
        assert KNOWN_DECIDER_EMAIL not in body, (
            f"{url} leaked a decider email address"
        )
        assert KNOWN_ATTACKER_EMAIL not in body, (
            f"{url} leaked a decider email address"
        )
        assert KNOWN_OIDC_SUBJECT not in body, (
            f"{url} leaked an oidc subject"
        )
        assert "dana@" not in body, f"{url} leaked a decider email address"
        # A github-handle-shaped oidc subject is covered by the exact-value
        # check above (KNOWN_OIDC_SUBJECT); a bare "@example.com" scan below
        # catches any email this suite did not enumerate by name.
        assert "@example.com" not in body, f"{url} leaked an email address"


def test_authenticated_caller_still_sees_real_decider_identity(client):
    """The redaction is scoped to UNAUTHENTICATED callers, exactly like the
    path/topology scrub it sits beside — an operator or console with the
    caller token still needs the real subject to do anything useful with an
    approval history."""
    _decide_and_refuse(client)

    approvals = client.get("/approvals").json()["approvals"]
    subjects = {a["decided_by"] for a in approvals if a["decided_by"]}
    assert KNOWN_OIDC_SUBJECT in subjects
    assert KNOWN_DECIDER_EMAIL in subjects

    entries = client.get("/ledger", params={"limit": 1000}).json()["entries"]
    decided_bodies = [e["body"] for e in entries if e["type"] == "approval.decided"]
    assert any(b.get("decided_by") == KNOWN_OIDC_SUBJECT for b in decided_bodies)
    refused_bodies = [e["body"] for e in entries if e["type"] == "approval.refused"]
    assert any(b.get("decided_by") == KNOWN_ATTACKER_EMAIL for b in refused_bodies)


def test_anonymous_approvals_pseudonym_is_present_and_stable(anonymous_client, client):
    """Redaction is pseudonymization, not blanking: the field still carries a
    value, the same decider hashes to the same token twice, and it is
    obviously not the real subject."""
    _decide_and_refuse(client)

    approvals = anonymous_client.get("/approvals").json()["approvals"]
    by_effect = {a["effect_id"]: a for a in approvals}
    approved = by_effect["eff-redact-approved"]
    rejected = by_effect["eff-redact-email"]

    assert approved["decided_by"], "pseudonymization must not blank the field"
    assert approved["decided_by"] != KNOWN_OIDC_SUBJECT
    assert rejected["decided_by"] != KNOWN_DECIDER_EMAIL

    # Same decider ("oidc|github|dana") appears on both the approval record
    # and the mirrored ledger entry; both must pseudonymize to one token.
    entries = anonymous_client.get("/ledger", params={"limit": 1000}).json()["entries"]
    decided_entry = next(
        e for e in entries
        if e["type"] == "approval.decided" and e["body"].get("approval_id") == approved["id"]
    )
    assert decided_entry["body"]["decided_by"] == approved["decided_by"]


def test_chain_verification_still_succeeds_against_redacted_anonymous_view(
    anonymous_client, client
):
    """The chain and its verification stay public and correct: an anonymous
    caller who checks /ledger/verify after decider identity has been
    pseudonymized still gets a clean bill of health for the same chain an
    authenticated caller sees — pseudonymizing identity on the way out never
    touches the ledger file or the digests computed over it."""
    _decide_and_refuse(client)

    anon_verify = anonymous_client.get("/ledger/verify").json()
    auth_verify = client.get("/ledger/verify").json()
    assert anon_verify["valid"] is True
    assert auth_verify["valid"] is True
    assert anon_verify["chain_length"] == auth_verify["chain_length"]

    # The per-entry "verified" tick /ledger renders is also computed against
    # the real, un-redacted body before the pseudonymization pass runs, so it
    # stays true for every entry even though the body shown is pseudonymized.
    entries = anonymous_client.get("/ledger", params={"limit": 1000}).json()["entries"]
    assert entries, "ledger is unexpectedly empty"
    assert all(e.get("verified") for e in entries if "_raw" not in e)
