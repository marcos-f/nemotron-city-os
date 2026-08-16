"""test://signet/oidc-login — a real issuer, and what it takes to be believed.

The demo's central claim is that an irreversible effect requires an
IDENTIFIED HUMAN. Until now the identity was asserted: ``MOCK LOGIN`` was
stamped on every screenshot eight pixels from "Approve as dana". These tests
are the difference between asserting and identifying.

Every one of them is about a REFUSAL, because that is where the value is. A
login flow that accepts a good token is easy; the property worth testing is
that it accepts NOTHING ELSE — not a token signed by the wrong key, not one
from the wrong issuer, not one minted for a different audience, not an
expired one, not a callback whose state was never issued, and not a replayed
token carrying somebody else's nonce.

The one failure that is allowed to end somewhere friendly is a provider we
cannot REACH, and only because being unable to ask is a different fact from
being told no. That path lands on the labelled mock button, within a
bounded timeout, and the badge it produces still says MOCK.

No test here touches the network.
"""

from __future__ import annotations

import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from helm.app import create_app
from helm.config import BootRefused, Settings, load_settings
from helm.oidc import (
    OidcClient,
    ProviderUnavailable,
    TokenInvalid,
)
from helm.signet import SESSION_COOKIE, Identity
from tests.conftest import sign_in

ISSUER = "https://git.nemotron.example.com"
CLIENT_ID = "bcf53bea29d99130abf87f045fa87a8d8d48ace5"
REDIRECT = "http://127.0.0.1:8610/auth/callback"


# ------------------------------------------------------------------ fixtures
def _rsa_key(kid: str) -> tuple[Any, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, jwk


@pytest.fixture(scope="module")
def keys() -> dict[str, Any]:
    """One key the provider publishes, and one it does not."""
    good_private, good_jwk = _rsa_key("signing-key-1")
    evil_private, _ = _rsa_key("signing-key-1")  # same kid, different key
    return {
        "good_private": good_private,
        "good_jwk": good_jwk,
        "evil_private": evil_private,
    }


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeProvider:
    """A contract-faithful stand-in for git.nemotron.example.com's OIDC endpoints.

    It answers discovery and JWKS, and hands back whatever ID token the test
    told it to mint. ``unreachable`` makes every call raise the way a dead
    network does — immediately, so a hang in the code under test shows up as
    a hang in the suite rather than as a pass.
    """

    def __init__(self, keys: dict[str, Any], *, unreachable: bool = False) -> None:
        self.keys = keys
        self.unreachable = unreachable
        self.id_token = ""
        self.token_status = 200
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(f"{method} {url}")
        if self.unreachable:
            raise ConnectionError("network is unreachable")
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResponse(200, {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                "token_endpoint": f"{ISSUER}/oauth/token",
                "userinfo_endpoint": f"{ISSUER}/oauth/userinfo",
                "jwks_uri": f"{ISSUER}/oauth/discovery/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
                "code_challenge_methods_supported": ["plain", "S256"],
            })
        if url.endswith("/oauth/discovery/keys"):
            return FakeResponse(200, {"keys": [self.keys["good_jwk"]]})
        if url.endswith("/oauth/token"):
            if self.token_status != 200:
                return FakeResponse(self.token_status, {"error": "invalid_grant"})
            return FakeResponse(200, {
                "access_token": "at-not-an-identity",
                "token_type": "bearer",
                "id_token": self.id_token,
            })
        return FakeResponse(404, {"error": "not_found"})


def mint(
    keys: dict[str, Any],
    *,
    nonce: str,
    issuer: str = ISSUER,
    audience: str = CLIENT_ID,
    expires_in: int = 300,
    sign_with: str = "good_private",
    subject: str = "42",
    alg: str = "RS256",
    **extra: Any,
) -> str:
    now = int(time.time())
    claims = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        "nonce": nonce,
        "email": "ruiz@nvidia-demo.example",
        "email_verified": True,
        "name": "Ruiz",
        "preferred_username": "ruiz",
    }
    claims.update(extra)
    return jwt.encode(
        claims, keys[sign_with], algorithm=alg, headers={"kid": "signing-key-1"}
    )


@pytest.fixture
def provider(keys: dict[str, Any]) -> FakeProvider:
    return FakeProvider(keys)


@pytest.fixture
def oidc(provider: FakeProvider) -> OidcClient:
    return OidcClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="a-secret-that-never-leaves-this-process",
        redirect_uri=REDIRECT,
        timeout=1.0,
        transport=provider,
    )


def begin(oidc: OidcClient) -> Any:
    _url, pending = oidc.begin()
    return pending


# ------------------------------------------------------------ the happy path
def test_a_verified_token_yields_the_subject_and_the_issuer(oidc, provider, keys):
    pending = begin(oidc)
    provider.id_token = mint(keys, nonce=pending.nonce)
    claims = oidc.complete(code="the-code", state=pending.state)

    assert claims.subject == "42"
    assert claims.issuer == ISSUER
    assert claims.issuer_host == "git.nemotron.example.com"
    assert claims.email == "ruiz@nvidia-demo.example"
    # Nothing that could be replayed leaves the module.
    assert "nonce" not in claims.claims
    assert "at-not-an-identity" not in json.dumps(claims.to_dict())


def test_the_authorization_url_carries_pkce_s256_and_a_nonce(oidc):
    url, pending = oidc.begin()
    assert url.startswith(f"{ISSUER}/oauth/authorize?")
    assert "code_challenge_method=S256" in url
    assert "code_challenge=" in url
    assert f"state={pending.state}" in url
    assert f"nonce={pending.nonce}" in url
    # The verifier itself must never travel with the front-channel request.
    assert pending.code_verifier not in url


# ------------------------------------------------- refusals: the token itself
def test_a_bad_signature_is_refused(oidc, provider, keys):
    """Signed with a key of the same kid that the provider does not publish."""
    pending = begin(oidc)
    provider.id_token = mint(keys, nonce=pending.nonce, sign_with="evil_private")
    with pytest.raises(TokenInvalid) as excinfo:
        oidc.complete(code="c", state=pending.state)
    assert "REFUSED" in str(excinfo.value)


def test_the_wrong_issuer_is_refused(oidc, provider, keys):
    pending = begin(oidc)
    provider.id_token = mint(keys, nonce=pending.nonce, issuer="https://evil.example")
    with pytest.raises(TokenInvalid):
        oidc.complete(code="c", state=pending.state)


def test_the_wrong_audience_is_refused(oidc, provider, keys):
    """A token minted for a DIFFERENT client of the same issuer."""
    pending = begin(oidc)
    provider.id_token = mint(keys, nonce=pending.nonce, audience="some-other-client")
    with pytest.raises(TokenInvalid):
        oidc.complete(code="c", state=pending.state)


def test_an_expired_token_is_refused(oidc, provider, keys):
    pending = begin(oidc)
    provider.id_token = mint(keys, nonce=pending.nonce, expires_in=-3600)
    with pytest.raises(TokenInvalid):
        oidc.complete(code="c", state=pending.state)


def test_the_none_algorithm_is_refused_before_any_key_lookup(oidc, provider, keys):
    """The classic. An unsigned token must not be a valid token."""
    pending = begin(oidc)
    provider.id_token = jwt.encode(
        {
            "iss": ISSUER, "sub": "42", "aud": CLIENT_ID,
            "iat": int(time.time()), "exp": int(time.time()) + 300,
            "nonce": pending.nonce,
        },
        key="",
        algorithm="none",
        headers={"kid": "signing-key-1"},
    )
    with pytest.raises(TokenInvalid) as excinfo:
        oidc.complete(code="c", state=pending.state)
    assert "alg" in str(excinfo.value)


def test_a_token_with_no_subject_is_refused(oidc, provider, keys):
    pending = begin(oidc)
    provider.id_token = mint(keys, nonce=pending.nonce, subject="")
    with pytest.raises(TokenInvalid):
        oidc.complete(code="c", state=pending.state)


def test_an_access_token_alone_is_not_an_identity(oidc, provider, keys):
    pending = begin(oidc)
    provider.id_token = ""  # provider returns only an access token
    with pytest.raises(TokenInvalid) as excinfo:
        oidc.complete(code="c", state=pending.state)
    assert "id_token" in str(excinfo.value)


# ----------------------------------------------- refusals: state and nonce
def test_an_unknown_state_is_refused(oidc, provider, keys):
    begin(oidc)
    with pytest.raises(TokenInvalid) as excinfo:
        oidc.complete(code="c", state="a-state-nobody-issued")
    assert "state" in str(excinfo.value)


def test_a_state_is_single_use(oidc, provider, keys):
    pending = begin(oidc)
    provider.id_token = mint(keys, nonce=pending.nonce)
    oidc.complete(code="c", state=pending.state)
    with pytest.raises(TokenInvalid):
        oidc.complete(code="c", state=pending.state)


def test_a_mismatched_nonce_is_refused(oidc, provider, keys):
    """A perfectly valid token from the right issuer — for another login."""
    other = begin(oidc)
    pending = begin(oidc)
    provider.id_token = mint(keys, nonce=other.nonce)
    with pytest.raises(TokenInvalid) as excinfo:
        oidc.complete(code="c", state=pending.state)
    assert "nonce" in str(excinfo.value)


def test_a_missing_nonce_is_refused(oidc, provider, keys):
    pending = begin(oidc)
    provider.id_token = mint(keys, nonce="")
    with pytest.raises(TokenInvalid):
        oidc.complete(code="c", state=pending.state)


def test_a_callback_with_no_code_is_refused(oidc):
    pending = begin(oidc)
    with pytest.raises(TokenInvalid):
        oidc.complete(code="", state=pending.state)


# ------------------------------------------------------- offline, not hanging
def test_an_unreachable_provider_is_unavailable_not_invalid(keys):
    """The distinction the whole fallback rests on."""
    dead = FakeProvider(keys, unreachable=True)
    client = OidcClient(
        issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=REDIRECT,
        timeout=0.5, transport=dead,
    )
    started = time.monotonic()
    with pytest.raises(ProviderUnavailable):
        client.begin()
    assert time.monotonic() - started < 5, "an offline run must not hang"


def test_offline_mode_makes_no_outbound_call_at_all(keys, provider):
    client = OidcClient(
        issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=REDIRECT,
        offline=True, transport=provider,
    )
    with pytest.raises(ProviderUnavailable) as excinfo:
        client.begin()
    assert "HELM_OFFLINE" in str(excinfo.value)
    assert provider.calls == [], "offline mode must not touch the network"


def test_an_unconfigured_client_says_which_variable_is_missing():
    client = OidcClient(issuer="", client_id="", redirect_uri=REDIRECT)
    assert client.configured is False
    assert "HELM_OIDC_ISSUER" in client.unconfigured_reason
    assert "HELM_OIDC_CLIENT_ID" in client.unconfigured_reason


def test_a_discovery_document_naming_another_issuer_is_refused(keys):
    class Liar(FakeProvider):
        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            response = super().request(method, url, **kwargs)
            if url.endswith("openid-configuration"):
                doc = dict(response.json(), issuer="https://somewhere.else")
                return FakeResponse(200, doc)
            return response

    client = OidcClient(
        issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=REDIRECT,
        transport=Liar(keys),
    )
    with pytest.raises(TokenInvalid):
        client.discover()


# ------------------------------------------------------ mapping onto signet
def test_the_verified_subject_keeps_its_existing_role(app, keys, provider):
    """roles.json is untouched: the change is WHERE the subject comes from.

    ruiz is an operator in the shipped role table. Signing in for real must
    find that row rather than create a second one and demote them.
    """
    signet = app.state.signet
    signet.ensure_subject("ruiz@nvidia-demo.example", "github", "ruiz")
    signet.set_role("ruiz@nvidia-demo.example", "operator", "test")

    client = OidcClient(
        issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=REDIRECT, transport=provider
    )
    _url, pending = client.begin()
    provider.id_token = mint(keys, nonce=pending.nonce)
    claims = client.complete(code="c", state=pending.state)

    _token, identity = signet.issue_for_claims(claims)
    assert identity.subject == "ruiz@nvidia-demo.example"
    assert identity.role == "operator"
    assert identity.issuer == ISSUER
    assert identity.mock is False


def test_an_unknown_subject_is_a_viewer_never_promoted(app, keys, provider):
    client = OidcClient(
        issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=REDIRECT, transport=provider
    )
    _url, pending = client.begin()
    provider.id_token = mint(
        keys, nonce=pending.nonce, subject="9001",
        email="stranger@example.com", preferred_username="stranger",
    )
    claims = client.complete(code="c", state=pending.state)
    _token, identity = app.state.signet.issue_for_claims(claims)
    assert identity.role == "viewer"


def test_an_unverified_email_does_not_become_the_subject(app, keys, provider):
    """Anyone can put an address in a profile. Only a verified one is a name."""
    client = OidcClient(
        issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=REDIRECT, transport=provider
    )
    _url, pending = client.begin()
    provider.id_token = mint(
        keys, nonce=pending.nonce, email="dana@nvidia-demo.example",
        email_verified=False, preferred_username="impostor",
    )
    claims = client.complete(code="c", state=pending.state)
    _token, identity = app.state.signet.issue_for_claims(claims)
    assert identity.subject != "dana@nvidia-demo.example"
    assert identity.subject == "git.nemotron.example.com/impostor"
    assert identity.role == "viewer"


# ------------------------------------------------------------- the badge
def test_the_badge_never_claims_real_auth_when_mocked():
    mock = Identity(subject="dana@nvidia-demo.example", provider="mock", mock=True)
    assert mock.auth_badge == "MOCK LOGIN"
    assert mock.auth_mode == "mock"

    real = Identity(
        subject="ruiz@nvidia-demo.example", provider="git.nemotron.example.com", issuer=ISSUER
    )
    assert real.auth_badge == "SIGNET · git.nemotron.example.com"
    assert real.auth_mode == "oidc"

    # An identity that somehow carried an issuer AND the mock flag is still
    # mock: the honest reading of a contradiction is the weaker one.
    contradiction = Identity(subject="x", issuer=ISSUER, mock=True)
    assert contradiction.auth_badge == "MOCK LOGIN"


def test_a_null_issuer_is_never_rendered_as_verified():
    """The failure this build was refused over: implying verification.

    A real sign-in with no signed assertion behind it is not mock and is
    not verified. It has its own word, and the word is never blank.
    """
    unverified = Identity(subject="dana@nvidia-demo.example", provider="github")
    assert unverified.mock is False
    assert unverified.issuer == ""
    assert unverified.auth_mode == "unverified"
    assert unverified.auth_badge == "UNVERIFIED IDENTITY"
    assert "UNVERIFIED" in unverified.auth_detail
    assert unverified.to_dict()["verified"] is False

    # Every state has words. None of them is the empty string.
    for identity in (
        unverified,
        Identity(subject="x", mock=True),
        Identity(subject="", authenticated=False),
        Identity(subject="x", issuer=ISSUER),
    ):
        assert identity.auth_badge.strip()
        assert identity.auth_detail.strip()


def test_an_approval_with_a_null_issuer_says_unverified_in_words(client, substrate):
    """Including records written before the issuer field existed."""
    sign_in(client, "dana@nvidia-demo.example", "admin", provider="mock")
    approval_id = substrate.propose("eff-null-issuer")
    client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "dana@nvidia-demo.example",
            "caller_role": "human",
        },
    )
    signet_block = client.get(f"/approvals/{approval_id}").json()["signet"]
    assert signet_block["verified"] is False
    assert signet_block["issuer"] == ""
    assert "UNVERIFIED" in signet_block["label"]
    assert signet_block["label"].strip()

    # And it is present on the LIST too, where a blank column is easiest
    # to read as agreement with the verified row above it.
    listed = next(
        a for a in client.get("/approvals").json()["approvals"] if a["id"] == approval_id
    )
    assert listed["signet"]["verified"] is False


def test_an_approval_with_an_issuer_names_it(client, substrate):
    sign_in(
        client, "ruiz@nvidia-demo.example", "operator",
        provider="git.nemotron.example.com", issuer=ISSUER,
    )
    approval_id = substrate.propose("eff-named-issuer")
    client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "ruiz@nvidia-demo.example",
            "caller_role": "human",
        },
    )
    signet_block = client.get(f"/approvals/{approval_id}").json()["signet"]
    assert signet_block["verified"] is True
    assert signet_block["issuer"] == ISSUER
    assert signet_block["label"] == "verified by git.nemotron.example.com"


def test_a_session_round_trips_its_issuer(app):
    signet = app.state.signet
    token = signet.issue("ruiz@nvidia-demo.example", "git.nemotron.example.com", "Ruiz", issuer=ISSUER)
    identity = signet.identity_from_token(token)
    assert identity.issuer == ISSUER
    assert identity.auth_badge == "SIGNET · git.nemotron.example.com"

    mock_token = signet.issue("dana@nvidia-demo.example", "mock", "dana", mock=True)
    assert signet.identity_from_token(mock_token).auth_badge == "MOCK LOGIN"


# -------------------------------------------- boot policy: the CAPABILITY
IDENTITY_MINTING_ENV = [
    {"AUTH_DISABLED": "1"},
    {},  # the default session secret is itself an identity-minting capability
]


@pytest.mark.parametrize("env", ["staging", "stage", "production", "prod"])
@pytest.mark.parametrize("extra", IDENTITY_MINTING_ENV)
def test_a_protected_env_refuses_every_way_of_minting_an_identity(env, extra):
    environ = {
        "HELM_ENV": env,
        "HELM_OIDC_ISSUER": ISSUER,
        "HELM_OIDC_CLIENT_ID": CLIENT_ID,
    }
    if extra:
        environ["HELM_SESSION_SECRET"] = "a-real-secret"
    environ.update(extra)
    with pytest.raises(BootRefused) as excinfo:
        load_settings(environ)
    message = str(excinfo.value)
    assert "REFUSED" in message and env in message
    for name in extra or ["HELM_SESSION_SECRET"]:
        assert name in message, "the refusal must NAME the capability"


def test_a_protected_env_with_no_provider_at_all_refuses():
    """Not a console that cannot be signed into. No console."""
    with pytest.raises(BootRefused) as excinfo:
        load_settings({
            "HELM_ENV": "production",
            "HELM_SESSION_SECRET": "a-real-secret",
        })
    assert "no identity provider" in str(excinfo.value)


def test_the_mock_route_does_not_exist(tmp_path, substrate):
    """There is no mock/dev one-click sign-in as a product capability."""
    staging = Settings(
        env="staging",
        data_dir=str(tmp_path / "data"),
        cache_dir=str(tmp_path / "cache"),
        offline=True,
        agent_model_url="",
        fleet_model_url="",
        nvidia_api_key="",
        session_secret="a-real-secret",
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        sibling_timeout=0.5,
    )
    client = TestClient(create_app(staging), follow_redirects=False)
    response = client.get("/auth/mock")
    assert response.status_code == 404
    assert SESSION_COOKIE not in response.cookies


# ------------------------------------------- the console, end to end offline
def test_the_login_page_says_no_sign_in_is_available_when_offline(client):
    """The scripted demo's path with no provider configured and no mock left.

    It must never claim a real provider, and it must not fall back to
    describing a mock identity that no longer exists as a product route.
    """
    page = client.get("/login")
    assert page.status_code == 200
    assert "NO SIGN-IN AVAILABLE" in page.text
    assert "MOCK LOGIN" not in page.text
    assert "/auth/mock" not in page.text
    assert "SIGNET ·" not in page.text


def test_beginning_a_login_with_no_provider_lands_back_on_login(client):
    response = client.get("/auth/oidc", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?error=")
    assert SESSION_COOKIE not in response.cookies


def test_a_forged_callback_issues_no_session(client):
    response = client.get(
        "/auth/callback?code=stolen&state=never-issued", follow_redirects=False
    )
    assert response.status_code == 400
    assert SESSION_COOKIE not in response.cookies
    assert client.get("/auth/session").json()["identity"]["authenticated"] is False


# --------------------------- the point of all of it: the approval record
def test_the_ledgered_approval_carries_subject_and_issuer(client, substrate):
    """WHO approved, and WHICH AUTHORITY vouched for them, in the chain.

    This is the line that turns "an irreversible effect requires an
    identified human" from a claim the console makes about itself into a
    record a third party can check against git.nemotron.example.com.
    """
    sign_in(
        client,
        "ruiz@nvidia-demo.example",
        "operator",
        provider="git.nemotron.example.com",
        issuer=ISSUER,
    )
    approval_id = substrate.propose("eff-attested")
    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "ruiz@nvidia-demo.example",
            "caller_role": "human",
            "rationale": "verified against the divergence",
        },
    )
    assert response.status_code == 200

    record = client.get(f"/approvals/{approval_id}").json()
    assert record["decided_by"] == "ruiz@nvidia-demo.example"
    assert record["issuer"] == ISSUER
    assert record["auth_mode"] == "oidc"

    entry = next(
        e for e in substrate.entries if e["type"] == "approval.decided"
        and e["body"]["approval_id"] == approval_id
    )
    assert entry["body"]["decided_by"] == "ruiz@nvidia-demo.example"
    assert entry["body"]["issuer"] == ISSUER
    assert entry["body"]["auth_mode"] == "oidc"


def test_a_mock_approval_is_ledgered_as_unattested(client, substrate):
    """The mock path still works — and the chain says exactly what it was."""
    sign_in(client, "dana@nvidia-demo.example", "admin", provider="mock")
    approval_id = substrate.propose("eff-unattested")
    response = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "dana@nvidia-demo.example",
            "caller_role": "human",
        },
    )
    assert response.status_code == 200

    entry = next(
        e for e in substrate.entries if e["type"] == "approval.decided"
        and e["body"]["approval_id"] == approval_id
    )
    assert entry["body"]["decided_by"] == "dana@nvidia-demo.example"
    assert entry["body"]["issuer"] is None
    assert entry["body"]["auth_mode"] != "oidc"


def test_an_issuer_is_never_borrowed_from_another_subject(client, substrate):
    """The attestation is attached only to the subject it belongs to.

    A signed-in operator naming somebody else in the body must not have
    their issuer stamped onto that other name.
    """
    sign_in(
        client,
        "ruiz@nvidia-demo.example",
        "operator",
        provider="git.nemotron.example.com",
        issuer=ISSUER,
    )
    approval_id = substrate.propose("eff-borrowed")
    client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "someone.else@nvidia-demo.example",
            "caller_role": "human",
        },
    )
    entry = next(
        (e for e in substrate.entries if e["type"] == "approval.decided"
         and e["body"]["approval_id"] == approval_id),
        None,
    )
    if entry is not None:
        assert entry["body"]["issuer"] is None


def test_healthz_names_the_mode_it_can_actually_reach(client):
    """No OIDC, no GitHub, and no mock capability left: the honest mode is "none"."""
    auth = client.get("/healthz").json()["auth"]
    assert auth["mode"] == "none"
    assert auth["oidc_configured"] is False
    assert "mock_available" not in auth
    assert "mock_login" not in auth
