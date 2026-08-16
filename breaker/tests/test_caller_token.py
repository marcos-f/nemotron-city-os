"""breaker authenticates to the substrate, and says so when it cannot.

throughline authenticates the CALLER on the half of its write surface that
changes who may do what. breaker touches exactly one of those acts —
``POST /approvals/{id}/decide`` — and its proposals, signals and probes are
ordinary fleet traffic that must keep working with no header at all.

The distinction these tests pin, which two review rounds were spent on:

    A refusal because we could not authenticate is NOT a refusal by the gate.

The gate did not decline the release. It never saw it. An operator told
"refused" concludes their decision was considered and denied — and goes to
argue with a policy, when the actual fix is to configure a token.
"""

from __future__ import annotations

import httpx
import pytest

from breaker.throughline import (
    CALLER_TOKEN_HEADER,
    NO_CALLER_TOKEN,
    REFUSAL_CALLER_TOKEN,
    CallerTokenMissing,
    HttpThroughlineClient,
    MockThroughlineClient,
    SubstrateRefused,
    SubstrateUnreachable,
    read_caller_token,
)

SUBJECT = "oidc|ruiz@nvidia-demo.example"


# ================================================================= resolution
def test_the_environment_wins(tmp_path, monkeypatch):
    (tmp_path / "caller-token").write_text("from-the-file\n")
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("THROUGHLINE_CALLER_TOKEN", "from-the-env")
    assert read_caller_token() == ("from-the-env", "environment")


def test_the_substrates_own_file_is_the_fallback(tmp_path, monkeypatch):
    (tmp_path / "caller-token").write_text("minted-by-the-substrate\n")
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path))
    token, source = read_caller_token()
    assert token == "minted-by-the-substrate"
    assert source.endswith("caller-token")


def test_an_absent_file_is_not_a_crash(tmp_path, monkeypatch):
    """breaker must construct, hold, propose and probe with no substrate."""
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path / "nothing-here"))
    token, source = read_caller_token()
    assert token == ""
    assert source.startswith("absent")
    assert HttpThroughlineClient("http://gate.invalid").caller_token == ""


def test_the_token_is_read_once_per_client(tmp_path, monkeypatch):
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path))
    (tmp_path / "caller-token").write_text("first\n")
    client = HttpThroughlineClient("http://gate.invalid")
    (tmp_path / "caller-token").write_text("second\n")
    # A token that shifts mid-session makes an auth failure unexplainable.
    assert client.caller_token == "first"


# ========================================= what actually goes on the wire
def test_only_the_decide_carries_the_header(monkeypatch, caller_token):
    seen: list[tuple[str, dict]] = []

    def capture(method, url, json=None, timeout=None, headers=None):
        seen.append((url, dict(headers or {})))
        return httpx.Response(200, json={"approval": {}, "effect": {}})

    monkeypatch.setattr(httpx, "request", capture)
    client = HttpThroughlineClient("http://gate.invalid")
    client.post_signal({"id": "sig-1"})
    client.post_effect({"id": "eff-1"})
    client.decide("apr-1", "approve", SUBJECT)

    headers = dict(seen)
    # Ordinary fleet traffic is untouched. That is what makes the substrate's
    # change deployable without a flag day, and breaker must not widen it.
    assert CALLER_TOKEN_HEADER not in headers["http://gate.invalid/signals"]
    assert CALLER_TOKEN_HEADER not in headers["http://gate.invalid/effects"]
    assert headers["http://gate.invalid/approvals/apr-1/decide"][
        CALLER_TOKEN_HEADER] == caller_token


# ============================ the honest failure, by name, not as a bare 403
def test_without_a_token_the_decide_is_never_sent(monkeypatch, tmp_path):
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path / "absent"))
    sent: list[str] = []

    def capture(method, url, json=None, timeout=None, headers=None):
        sent.append(url)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "request", capture)
    client = HttpThroughlineClient("http://gate.invalid")

    with pytest.raises(CallerTokenMissing) as caught:
        client.decide("apr-1", "approve", SUBJECT)

    assert NO_CALLER_TOKEN in str(caught.value)
    assert caught.value.status == 401       # not 403: we did not authenticate
    assert caught.value.refusal_reason == REFUSAL_CALLER_TOKEN
    assert caught.value.refusal["attempted"] is False
    assert sent == []                        # never left the process


def test_a_rejected_token_is_still_an_authentication_failure(monkeypatch, caller_token):
    def refuse(method, url, json=None, timeout=None, headers=None):
        return httpx.Response(403, json={
            "detail": "caller token required",
            "refused": True,
            "refusal_reason": REFUSAL_CALLER_TOKEN,
        })

    monkeypatch.setattr(httpx, "request", refuse)
    client = HttpThroughlineClient("http://gate.invalid")
    with pytest.raises(CallerTokenMissing) as caught:
        client.decide("apr-1", "approve", SUBJECT)
    assert caught.value.status == 403        # the substrate's own status kept
    assert "AUTHENTICATION failure" in str(caught.value)


def test_it_is_still_caught_by_every_fail_closed_except(monkeypatch, tmp_path):
    """The hold is unchanged. Only what the caller is TOLD changes."""
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path / "absent"))
    client = HttpThroughlineClient("http://gate.invalid")
    with pytest.raises(SubstrateUnreachable):
        client.decide("apr-1", "approve", SUBJECT)
    with pytest.raises(SubstrateRefused):
        client.decide("apr-1", "approve", SUBJECT)


def test_an_ordinary_gate_refusal_is_not_relabelled(monkeypatch, caller_token):
    """The other direction: a REAL refusal must keep saying it is one."""
    def refuse(method, url, json=None, timeout=None, headers=None):
        return httpx.Response(403, json={
            "detail": "caller_role is required",
            "refused": True,
            "refusal_reason": "caller-role-required",
        })

    monkeypatch.setattr(httpx, "request", refuse)
    client = HttpThroughlineClient("http://gate.invalid")
    with pytest.raises(SubstrateRefused) as caught:
        client.decide("apr-1", "approve", SUBJECT)
    assert not isinstance(caught.value, CallerTokenMissing)
    assert caught.value.refusal_reason == "caller-role-required"


# ================================================ the mock holds the same line
def test_the_mock_refuses_a_tokenless_decide_exactly_as_the_gate_does(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path / "absent"))
    mock = MockThroughlineClient()
    effect = mock.post_effect({"id": "eff-1", "reversibility": "irreversible"})
    with pytest.raises(CallerTokenMissing) as caught:
        mock.decide(effect["approval_id"], "approve", SUBJECT)
    assert caught.value.refusal_reason == REFUSAL_CALLER_TOKEN
    # And the effect is STILL HELD. Nothing was released by the failure.
    assert mock.approvals()[0]["state"] == "pending"


def test_the_mock_with_a_token_decides_as_before(caller_token):
    mock = MockThroughlineClient()
    effect = mock.post_effect({"id": "eff-1", "reversibility": "irreversible"})
    result = mock.decide(effect["approval_id"], "approve", SUBJECT)
    assert result["approval"]["state"] == "approved"
    assert result["effect"]["status"] == "executed"


# ============================================== and at the HTTP surface breaker
# ============================================== actually presents to an operator
def test_the_api_says_authentication_and_never_says_the_gate_refused(
    monkeypatch, tmp_path, service
):
    from fastapi.testclient import TestClient

    from breaker.app import create_app

    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path / "absent"))
    # Rebuild the gate client so it resolves the (absent) token now.
    service.watch.client = MockThroughlineClient()

    with TestClient(create_app(service=service)) as client:
        proposal = client.post("/fixture/run").json()["proposals"][0]
        response = client.post(
            f"/proposals/{proposal['id']}/decide",
            json={"decision": "approve", "decided_by": SUBJECT},
        )

    body = response.json()
    assert body["error"] == "caller_token_missing"
    assert body["authenticated"] is False
    assert body["fail_closed"] is True
    assert body["refusal_reason"] == REFUSAL_CALLER_TOKEN
    # NOT "substrate_refused". The gate did not refuse; it was never asked.
    assert body["error"] != "substrate_refused"
    assert "AUTHENTICATION problem" in body["invariant"]
    assert "not a refusal of the effect" in body["invariant"]
    assert "THROUGHLINE_CALLER_TOKEN" in body["remedy"]
