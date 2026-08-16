"""The login page must name the identity mode that is actually in force.

Twelve first-time reviewers hit this deployment cold. Seven of them opened the
same finding: the page rendered ``MOCK LOGIN — the console then runs with a
fixed identity`` while ``/auth/mock`` returned 403 because the environment is
production and ``/auth/github`` ran a real OAuth handshake against github.com.

The cause was not a logic error in the auth code. ``mock_reason`` is set whenever
*OIDC* is unconfigured — it explains why signet is unavailable — and the template
rendered that string under a MOCK LOGIN heading regardless of whether mock login
was reachable at all.

For a product whose whole claim is that it never asserts what it cannot verify,
the front door asserting a false thing about its own identity mode is the most
expensive defect it can carry.

There is no mock/dev one-click sign-in as a product capability any more —
``/auth/mock`` does not exist and there is no ``MOCK LOGIN`` branch left to
render. The login page honestly offers only signet/OIDC when configured,
GitHub when configured, or says plainly that no session can be established.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helm.app import create_app
from helm.config import Settings


def _settings(tmp_path: Path, **over) -> Settings:
    base = dict(
        env="production",
        data_dir=str(tmp_path / "data"),
        cache_dir=str(tmp_path / "cache"),
        offline=True,
        agent_model_url="",
        fleet_model_url="",
        nvidia_api_key="",
        session_secret="test-secret",
        sibling_timeout=0.5,
    )
    base.update(over)
    return Settings(**base)


def _login(settings: Settings) -> str:
    return TestClient(create_app(settings)).get("/login").text


def test_github_only_deployment_does_not_claim_to_be_running_a_mock(
    substrate, tmp_path: Path
) -> None:
    """OIDC unset + mock refused + GitHub configured == a GitHub identity.

    This is the exact state the deployed host was in when the panel reviewed it.
    """
    body = _login(_settings(tmp_path, github_client_id="gh-client", github_client_secret="s"))

    assert "MOCK LOGIN:" not in body, (
        "the page announced MOCK LOGIN while mock login does not exist as a "
        "product capability — that is the contradiction seven reviewers reported"
    )
    assert "GITHUB IDENTITY:" in body
    # It must not overclaim in the other direction either: a personal GitHub
    # account is a real identity but it is not an enterprise IdP.
    assert "not an enterprise IdP" in body


def test_no_provider_at_all_says_no_sign_in_is_available(
    substrate, tmp_path: Path
) -> None:
    """No OIDC, no GitHub, no mock: say so rather than describing a mock."""
    body = _login(_settings(tmp_path))

    assert "NO SIGN-IN AVAILABLE:" in body
    assert "MOCK LOGIN:" not in body


def test_the_login_page_never_offers_a_mock_button_in_any_environment(
    substrate, tmp_path: Path
) -> None:
    """There is no mock/dev one-click sign-in as a product capability.

    Not fenced by env, not gated by a flag — gone. The dev/test environment
    used to be exactly where the mock button DID render; now it must not,
    anywhere.
    """
    for env in ("dev", "test", "staging", "production"):
        kwargs = {"env": env}
        if env in ("staging", "production"):
            kwargs["session_secret"] = "a-real-secret"
        body = _login(_settings(tmp_path / env, **kwargs))
        assert "/auth/mock" not in body, f"env={env} still links to /auth/mock"
        assert "MOCK LOGIN" not in body, f"env={env} still advertises MOCK LOGIN"


def test_auth_mock_route_does_not_exist(substrate, tmp_path: Path) -> None:
    """The route itself is gone, not merely refused with a 403."""
    client = TestClient(create_app(_settings(tmp_path, env="dev")))
    response = client.get("/auth/mock", follow_redirects=False)
    assert response.status_code == 404


def test_the_dead_google_button_is_removed(substrate, tmp_path: Path) -> None:
    """A decorative, unclickable 'Continue with Google' read as available.

    Reviewers called it a dead button: it looked like a provider choice but
    was an inert span with no href. Under the no-stub-UI directive it is
    removed rather than left to be mistaken for something real.
    """
    body = _login(_settings(tmp_path))
    assert "Continue with Google" not in body
