"""test://helm/caller-token — helm authenticates to the substrate, and says so
when it cannot.

throughline authenticates the CALLER on the half of its write surface that
changes who may do what: ``POST /approvals/{id}/decide``, ``authz.*`` effects
and ``warrant.*`` signals. helm has to present the token — and, when it has
none, has to fail in a way nobody can mistake for the gate declining the
effect.

That distinction is the point of this file. Two review rounds went on it. A
403 because we could not authenticate means the act NEVER REACHED the rule
that would have judged it: the gate did not refuse the effect, it never saw
it. An operator told "refused" reasonably concludes their approval was
considered and denied. It was not.
"""

from __future__ import annotations

import pytest

import helm.clients as clients_module
from helm.clients import (
    CALLER_TOKEN_HEADER,
    NO_CALLER_TOKEN,
    REFUSAL_CALLER_TOKEN,
    CallerToken,
    Federation,
    read_caller_token,
)

from conftest import sign_in  # noqa: E402


URLS = {
    "throughline": "http://127.0.0.1:8600",
    "docket": "http://127.0.0.1:8601",
    "breaker": "http://127.0.0.1:8602",
    "siren": "http://127.0.0.1:8603",
    "blindspot": "http://127.0.0.1:8604",
}


# ================================================= resolving it, once, safely
def test_the_environment_wins_and_is_read_once(tmp_path, monkeypatch):
    (tmp_path / "caller-token").write_text("from-the-file\n")
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("THROUGHLINE_CALLER_TOKEN", "from-the-env")
    token = read_caller_token()
    assert token.value == "from-the-env"
    assert token.source == "environment"


def test_the_substrates_own_file_is_the_fallback(tmp_path, monkeypatch):
    (tmp_path / "caller-token").write_text("minted-by-the-substrate\n")
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path))
    token = read_caller_token()
    assert token.value == "minted-by-the-substrate"
    assert token.source.endswith("caller-token")


def test_an_absent_token_file_is_not_a_crash(tmp_path, monkeypatch):
    """A federation booted without the substrate must still construct."""
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path / "nothing-here"))
    token = read_caller_token()
    assert token.present is False
    assert token.source == "absent"
    assert "could not be read" in token.detail
    # And a client built on it exists, rather than raising at import time.
    assert Federation(URLS).throughline.caller_token.present is False


def test_the_token_is_read_once_per_client(tmp_path, monkeypatch):
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path))
    (tmp_path / "caller-token").write_text("first\n")
    federation = Federation(URLS)
    # Moving the file underneath a live client must not silently change what
    # it presents mid-session; an auth failure that shifts is unexplainable.
    (tmp_path / "caller-token").write_text("second\n")
    assert federation.throughline.caller_token.value == "first"
    # And every sibling shares the one reading.
    assert federation.docket.caller_token is federation.throughline.caller_token


# =============================================== what actually goes on the wire
def test_a_decide_carries_the_header_and_an_ordinary_signal_does_not(substrate, monkeypatch):
    seen: list[tuple[str, dict]] = []
    real = substrate.request

    def spy(method, url, **kw):
        seen.append((url, dict(kw.get("headers") or {})))
        return real(method, url, **kw)

    monkeypatch.setattr(clients_module.httpx, "request", spy)
    federation = Federation(URLS)
    approval_id = substrate.propose("eff-1", "irreversible", "shed load", "sig-1")

    federation.throughline.signal(
        {"id": "sig-2", "class": "grid.telemetry", "source": "t"}
    )
    federation.throughline.decide(
        approval_id, decision="approve", decided_by="dana@x", caller_role="human"
    )

    signal_headers = next(h for u, h in seen if u.endswith("/signals"))
    decide_headers = next(h for u, h in seen if u.endswith("/decide"))
    # Ordinary fleet traffic is untouched — this is what makes the substrate's
    # change deployable without a flag day, and helm must not widen it.
    assert CALLER_TOKEN_HEADER not in signal_headers
    assert decide_headers[CALLER_TOKEN_HEADER] == substrate.caller_token


def test_a_warrant_signal_and_an_authz_effect_are_privileged(substrate, monkeypatch):
    seen: list[tuple[str, dict]] = []
    real = substrate.request

    def spy(method, url, **kw):
        seen.append((url, dict(kw.get("headers") or {})))
        return real(method, url, **kw)

    monkeypatch.setattr(clients_module.httpx, "request", spy)
    federation = Federation(URLS)
    federation.throughline.signal({"id": "s", "class": "warrant.grant", "source": "helm"})
    federation.throughline.effect({"id": "e", "effect_type": "authz.grant"})
    federation.throughline.effect({"id": "e2", "effect_type": "microgrid_dispatch"})

    headers = {u: h for u, h in seen}
    assert CALLER_TOKEN_HEADER in headers["http://127.0.0.1:8600/signals"]
    effects = [h for u, h in seen if u.endswith("/effects")]
    assert CALLER_TOKEN_HEADER in effects[0]      # authz.grant
    assert CALLER_TOKEN_HEADER not in effects[1]  # an ordinary dispatch


# ================================== the honest failure, by name, not as a 403
def test_without_a_token_the_decide_is_never_even_sent(substrate, monkeypatch, tmp_path):
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path / "absent"))
    sent: list[str] = []
    real = substrate.request

    def spy(method, url, **kw):
        sent.append(url)
        return real(method, url, **kw)

    monkeypatch.setattr(clients_module.httpx, "request", spy)
    approval_id = substrate.propose("eff-2", "irreversible", "shed load", "sig-2")

    reply = Federation(URLS).throughline.decide(
        approval_id, decision="approve", decided_by="dana@x", caller_role="human"
    )

    assert reply.ok is False
    assert reply.unauthenticated is True
    assert reply.refusal_reason == REFUSAL_CALLER_TOKEN
    assert NO_CALLER_TOKEN in reply.error
    # Not sent at all. We cannot report that the gate refused something we
    # never asked it about.
    assert not any(u.endswith("/decide") for u in sent)
    # 401, not 403: "we did not authenticate" is a different sentence.
    assert reply.status == 401


def test_a_rejected_token_is_named_as_an_authentication_failure(substrate, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_CALLER_TOKEN", "the-wrong-token")
    approval_id = substrate.propose("eff-3", "irreversible", "shed load", "sig-3")
    reply = Federation(URLS).throughline.decide(
        approval_id, decision="approve", decided_by="dana@x", caller_role="human"
    )
    assert reply.ok is False
    assert reply.status == 403          # the substrate's own status, preserved
    assert reply.unauthenticated is True
    assert "rejected the caller token" in reply.error


@pytest.mark.parametrize("token,expected_phrase", [
    (None, NO_CALLER_TOKEN),
    ("the-wrong-token", "rejected the caller token"),
])
def test_the_console_says_authentication_and_never_says_refused(
    substrate, settings, monkeypatch, tmp_path, token, expected_phrase
):
    """THE point of this file, at the surface an operator actually reads."""
    if token is None:
        monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
        monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path / "absent"))
    else:
        monkeypatch.setenv("THROUGHLINE_CALLER_TOKEN", token)

    from fastapi.testclient import TestClient

    from helm.app import create_app

    approval_id = substrate.propose("eff-4", "irreversible", "shed load", "sig-4")
    client = TestClient(create_app(settings))
    sign_in(client, "dana@nvidia-demo.example", role="operator")

    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "dana@nvidia-demo.example",
              "rationale": "confirmed"},
    )
    outcome = response.json()["detail"]

    assert response.status_code in (401, 403)
    assert outcome["refusal_kind"] == "authentication"
    # `refused` means THE GATE declined. It did not — it was never asked.
    assert outcome["refused"] is False
    assert outcome["unauthenticated"] is True
    assert outcome["ledgered"] is False
    assert expected_phrase in outcome["reason"]
    assert "authentication problem" in outcome["reason"]
    # Routed through the existing three-state machinery rather than a new path.
    assert outcome["action_result"]["performed"] is False
    assert "DID NOT RUN" in outcome["summary"]
    steps = {s["step"]: s["state"] for s in outcome["action_result"]["steps"]}
    assert steps == {"authenticate-caller": "refused", "decide": "skipped"}

    # And the effect really is still held: nothing decided it.
    assert substrate.approvals[approval_id]["state"] == "pending"


def test_with_a_token_the_same_decide_succeeds_and_is_ledgered(substrate, client):
    """The other half of the before/after: nothing is broken by the header."""
    approval_id = substrate.propose("eff-5", "irreversible", "shed load", "sig-5")
    sign_in(client, "dana@nvidia-demo.example", role="operator")
    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve", "decided_by": "dana@nvidia-demo.example",
              "rationale": "confirmed"},
    )
    assert response.status_code == 200
    assert substrate.approvals[approval_id]["state"] == "approved"
    kinds = [e["type"] for e in substrate.entries]
    assert "approval.decided" in kinds
    assert "write.refused" not in kinds


def test_a_client_with_no_token_still_reads_everything(substrate, monkeypatch, tmp_path):
    """An absent token must not take the READ surface down with it."""
    monkeypatch.delenv("THROUGHLINE_CALLER_TOKEN", raising=False)
    monkeypatch.setenv("THROUGHLINE_DATA_DIR", str(tmp_path / "absent"))
    federation = Federation(URLS)
    assert federation.throughline.status().online is True
    assert federation.throughline.ledger().ok is True
    assert federation.throughline.verify().ok is True
    assert federation.throughline.approvals().ok is True
    # And ordinary fleet traffic still writes.
    assert federation.throughline.signal(
        {"id": "sig-9", "class": "grid.telemetry", "source": "t"}
    ).ok is True


def test_an_injected_token_overrides_the_environment():
    """So a caller that has resolved a token some other way can pass it in."""
    client = clients_module.SiblingClient(
        "throughline", URLS["throughline"], caller_token=CallerToken("given", "caller")
    )
    assert client.caller_token.value == "given"
