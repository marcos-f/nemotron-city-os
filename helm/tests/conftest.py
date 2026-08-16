"""Test fixtures: a real helm app over a fake — but contract-faithful — federation.

No test in this suite touches the network. The model ladder is disabled by
default (``HELM_OFFLINE=1`` with no cache), so NemoClerk runs its
deterministic composer and the tool grounding is what is under test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeFederation  # noqa: E402

import helm.clients as clients_module  # noqa: E402
from helm.app import create_app  # noqa: E402
from helm.config import Settings  # noqa: E402
from helm.signet import SESSION_COOKIE  # noqa: E402


@pytest.fixture
def substrate(monkeypatch: pytest.MonkeyPatch) -> FakeFederation:
    fake = FakeFederation()
    # The fake substrate authenticates its caller exactly as :8600 does, so
    # the suite must hold a token like any other client. It goes into the
    # ENVIRONMENT rather than being injected, because that is the resolution
    # path a deployed helm uses — a test that bypassed it would not be
    # testing it.
    monkeypatch.setenv("THROUGHLINE_CALLER_TOKEN", fake.caller_token)
    monkeypatch.setattr(clients_module.httpx, "request", fake.request)
    return fake


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        data_dir=str(tmp_path / "data"),
        cache_dir=str(tmp_path / "cache"),
        offline=True,
        agent_model_url="",
        fleet_model_url="",
        nvidia_api_key="",
        session_secret="test-secret",
        sibling_timeout=0.5,
    )


@pytest.fixture
def app(substrate: FakeFederation, settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def sign_in(
    client: TestClient,
    subject: str,
    role: str = "",
    provider: str = "github",
    issuer: str = "",
):
    """Bind a session cookie for ``subject``, optionally forcing its role.

    ``issuer`` is what separates a session an authority vouched for from one
    this console merely asserted, so a test that cares about the difference
    says so here rather than reaching into the cookie itself.
    """
    signet = client.app.state.signet
    if role:
        table = signet.roles.read()
        table[subject] = {
            "provider": provider,
            "role": role,
            "display": subject.split("@")[0],
            "last_seen": "",
        }
        signet.roles.write(table)
    token = signet.issue(subject, provider, subject.split("@")[0], issuer=issuer)
    client.cookies.set(SESSION_COOKIE, token)
    return client


@pytest.fixture
def admin(client: TestClient) -> TestClient:
    return sign_in(client, "dana@nvidia-demo.example", "admin")


@pytest.fixture
def operator(client: TestClient) -> TestClient:
    return sign_in(client, "ruiz@nvidia-demo.example", "operator")


@pytest.fixture
def viewer(client: TestClient) -> TestClient:
    return sign_in(client, "kim@nvidia-demo.example", "viewer")
