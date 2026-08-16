"""Shared fixtures. Every test gets its own data dir — no shared state."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from throughline.app import create_app
from throughline.service import Settings, Substrate

REPO_ROOT = Path(__file__).resolve().parents[1]
GOOD_CONFIG = REPO_ROOT / "config" / "effects.yaml"
FIXTURES = Path(__file__).parent / "fixtures"
BAD_CONFIG = FIXTURES / "unsafe-auto-execute.yaml"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # The fixtures directory is allowlisted for reload so the UC-006 refusal
    # beats still load their bad configs over HTTP. Anywhere else — tmp_path,
    # /etc, an attacker's home — is outside the allowlist and refused.
    return Settings(
        data_dir=tmp_path / "data",
        config_path=GOOD_CONFIG,
        port=8600,
        extra_config_dirs=[FIXTURES],
    )


@pytest.fixture
def substrate(settings: Settings) -> Substrate:
    return Substrate(settings)


@pytest.fixture
def client(substrate: Substrate):
    """An AUTHENTICATED caller — what helm is once it is wired.

    The privileged half of the write surface (a decide, an ``authz.*``
    effect, a ``warrant.*`` signal) requires the caller token, so the
    default client presents it. ``anonymous_client`` is the one that does
    not, and the security tests use it to stand in for a stranger with curl.
    """
    token = substrate.settings.resolved_caller_token
    app = create_app(substrate=substrate)
    with TestClient(app, headers={"X-Throughline-Caller-Token": token}) as test_client:
        test_client.substrate = substrate
        yield test_client


@pytest.fixture
def anonymous_client(substrate: Substrate):
    """A caller off the network with no credential of any kind."""
    with TestClient(create_app(substrate=substrate)) as test_client:
        test_client.substrate = substrate
        yield test_client
