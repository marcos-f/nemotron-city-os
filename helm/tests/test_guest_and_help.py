"""helm#8/#9 — a genuine read-only guest console, and a navigable 404/help.

Six reviewers could not evaluate the product at all: every console tab 303'd
to /login for a first-time anonymous visitor, so "everything I could confirm
came from the read-only JSON API, not the product itself." Separately, a
support reviewer found /help /support /contact /about /status-page /faq all
404 with a bare ``{"detail":"Not Found"}`` and no way back into the app.

This file is the regression coverage for both fixes:

- a guest can reach the read-only console with no credential and sees the
  SAME real data the public JSON API already serves;
- a guest cannot reach any write/approve affordance, and no guest request
  can create an approval decision;
- /help exists, names component states, and never leaks a sibling address;
- an unmatched route asked for by a browser (Accept: text/html) is
  navigable, while a JSON API caller's 404 shape is unchanged.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

LOOPBACK_ADDRESS = re.compile(r"127\.0\.0\.1:860\d\b")


@pytest.fixture
def held_effect(substrate) -> tuple[str, str]:
    effect_id = "eff-guest-view-check"
    substrate.propose(effect_id, description="load-shed battery_9")
    return effect_id, f"apr-{effect_id}"


# --------------------------------------------------------------- item 2


def test_guest_console_reachable_with_no_credential(client: TestClient) -> None:
    """No 303 to /login, no cookie — the whole point of the fix."""
    response = client.get("/guest", follow_redirects=False)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "read-only" in response.text.lower()


def test_landing_page_offers_guest_path_with_no_redirect(client: TestClient) -> None:
    """GET / used to 303 to /login with an empty body for an anonymous
    visitor. It must now answer 200 and offer the guest path in the body.
    """
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert 'href="/guest"' in response.text
    assert 'href="/login"' in response.text


def test_guest_console_renders_the_same_real_data_the_json_api_serves(
    client: TestClient, substrate, held_effect
) -> None:
    """No invented/sample data: the guest page must show the SAME held
    effect the public /approvals and /ledger endpoints already answer.
    """
    effect_id, approval_id = held_effect
    api_approvals = client.get("/approvals").json()
    assert api_approvals["known"] is True
    assert any(a["id"] == approval_id for a in api_approvals["approvals"])

    page = client.get("/guest").text
    assert approval_id in page
    assert "load-shed battery_9" in page
    api_ledger = client.get("/ledger").json()
    assert api_ledger["rows"], "the fixture produced no ledger rows to compare against"
    assert api_ledger["rows"][0]["hash_prefix"] in page


def test_guest_console_shows_a_held_effect_as_held_not_actionable(
    client: TestClient, substrate, held_effect
) -> None:
    effect_id, approval_id = held_effect
    page = client.get("/guest").text
    assert "HELD" in page
    assert "sign in" in page.lower()


def test_guest_console_carries_no_write_or_decide_affordance(
    client: TestClient, substrate, held_effect
) -> None:
    """A guest must be able to SEE a held effect and understand it is held,
    and must not be able to act on it — no form, no decide button, no
    approve/reject control anywhere on the page.
    """
    page = client.get("/guest").text
    lowered = page.lower()
    assert "<form" not in lowered
    assert "review &amp; decide" not in lowered
    assert "onclick=" not in lowered
    assert 'href="/approvals/' not in page  # no link that could reach the decide endpoint
    assert re.search(r"<button[^>]*>\s*approve", lowered) is None
    assert re.search(r"<button[^>]*>\s*reject", lowered) is None
    assert re.search(r'<a[^>]*>\s*approve', lowered) is None
    assert re.search(r'<a[^>]*>\s*reject', lowered) is None


def test_guest_console_never_implies_an_identified_human(client: TestClient) -> None:
    page = client.get("/guest").text
    assert "sign out" not in page.lower()
    assert "profile" not in page.lower()
    boot_body = page
    assert '"authenticated": true' not in boot_body.lower()


def test_no_guest_request_can_create_an_approval_decision(
    client: TestClient, substrate, held_effect
) -> None:
    """The one path that must never open: a cookieless caller POSTing a
    decision. Refused, and the queue is unchanged.
    """
    effect_id, approval_id = held_effect
    before = client.get(f"/approvals/{approval_id}").json()
    assert before["state"] == "pending"

    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "guest-attempt"},
    )
    assert response.status_code != 200, response.text

    after = client.get(f"/approvals/{approval_id}").json()
    assert after["state"] == "pending", "an unauthenticated request decided a held effect"


def test_guest_console_withholds_sibling_addresses(client: TestClient, substrate) -> None:
    page = client.get("/guest").text
    assert not LOOPBACK_ADDRESS.search(page), "guest console leaked a sibling address"


# --------------------------------------------------------------- item 6


def test_help_route_exists(client: TestClient) -> None:
    response = client.get("/help")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_help_names_component_states_not_addresses(client: TestClient, substrate) -> None:
    page = client.get("/help").text
    for name in ("throughline", "docket", "breaker", "siren", "blindspot"):
        assert name in page
    assert not LOOPBACK_ADDRESS.search(page), "help page leaked a sibling address"


def test_help_offers_a_way_back_in(client: TestClient) -> None:
    page = client.get("/help").text
    assert 'href="/guest"' in page
    assert 'href="/login"' in page
    assert 'href="/"' in page


@pytest.mark.parametrize(
    "path", ["/support", "/contact", "/about", "/status-page", "/faq"]
)
def test_unknown_browser_routes_404_navigably(client: TestClient, path: str) -> None:
    """These all 404'd with a bare JSON detail and no way back — the
    reviewer's exact complaint. A browser (Accept: text/html) must now get
    a page with links, not a dead end.
    """
    response = client.get(path, headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert 'href="/"' in response.text
    assert 'href="/guest"' in response.text
    assert 'href="/help"' in response.text


def test_unknown_route_404_json_shape_is_unchanged_for_api_callers(
    client: TestClient,
) -> None:
    """The navigable 404 must not change the JSON API's contract: a caller
    that did not ask for HTML still gets the plain ``{"detail": ...}`` body
    every other 404 in this API already returns.
    """
    response = client.get("/support")
    assert response.status_code == 404
    body = response.json()
    assert body == {"detail": "Not Found"}
