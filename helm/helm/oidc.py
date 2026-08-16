"""Real OIDC for signet — authorization code + PKCE, and a verified ID token.

signet always modelled an OIDC-shaped identity: a subject, an issuer, claims,
a role bound to the subject. Only the issuer was stubbed, so every approval
record in the demo said ``MOCK LOGIN`` eight pixels from "Approve as dana".
The product claim is that an irreversible effect requires an IDENTIFIED
HUMAN; an asserted identity does not identify anybody.

This module is the substitution. It does the whole round trip against a real
provider and, crucially, VERIFIES what comes back:

* the ID token signature, against the provider's published JWKS
* ``iss`` against the discovered issuer
* ``aud`` against our own client id
* ``exp``/``iat``, with a small leeway
* ``nonce`` against the one this browser was sent out with
* the signing algorithm against the provider's advertised set, with the
  ``none`` algorithm and every symmetric algorithm refused outright

Any of those failing is a REFUSAL, never a downgrade — there is no mock
identity for it to fall back into. An unreachable provider is a different
outcome from a bad token: the first surfaces as a stated reason on the
login page (a real other provider if one is configured, or an honest "no
sign-in available" if none is), never as a fabricated session.

Offline
-------
The scripted demo runs with no internet. Every network call here has an
explicit short timeout and no retries, and ``HELM_OFFLINE=1`` skips the
network entirely. An unreachable provider surfaces as
:class:`ProviderUnavailable` within ``oidc_timeout`` seconds, and the login
page says so in words — it never hangs, and it never presents the mock
identity as a real one.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger("helm.oidc")

#: Asymmetric signatures only. ``none`` and the HS* family are refused
#: before a key is ever looked up: with a symmetric algorithm, a JWKS entry
#: an attacker can read becomes a key they can sign with.
ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "PS256"})

#: An authorization request that has not come back within this many seconds
#: is dead. It bounds the state/nonce table as well as the user's patience.
LOGIN_TTL_SECONDS = 600

#: Clock skew we will tolerate on ``exp``/``iat``.
LEEWAY_SECONDS = 60

DEFAULT_SCOPES = ("openid", "profile", "email", "read_user")


class OidcError(RuntimeError):
    """Base for everything this module refuses."""


class ProviderUnavailable(OidcError):
    """The provider could not be reached, or is not configured.

    This is the ONLY failure that may end at the labelled mock path, and
    only because being unable to ask is different from being told no.
    """


class TokenInvalid(OidcError):
    """The provider answered, and the answer did not verify.

    Never falls back. A token that fails verification is evidence of a
    problem, not a reason to sign someone in under another name.
    """


# --------------------------------------------------------------- utilities
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def issuer_host(issuer: str) -> str:
    """``https://git.nemotron.example.com`` -> ``git.nemotron.example.com``. For the badge."""
    return issuer.split("://", 1)[-1].split("/", 1)[0] if issuer else ""


# ---------------------------------------------------------------- metadata
@dataclass(frozen=True)
class ProviderMetadata:
    """The discovery document, reduced to what the flow actually uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str = ""
    signing_algorithms: tuple[str, ...] = ("RS256",)
    code_challenge_methods: tuple[str, ...] = ()
    fetched_at: float = 0.0

    @property
    def supports_pkce_s256(self) -> bool:
        return "S256" in self.code_challenge_methods

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> "ProviderMetadata":
        required = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
        missing = [key for key in required if not doc.get(key)]
        if missing:
            raise ProviderUnavailable(
                "the discovery document is missing "
                + ", ".join(missing)
                + " — this is not a usable OIDC provider"
            )
        return cls(
            issuer=str(doc["issuer"]).rstrip("/"),
            authorization_endpoint=str(doc["authorization_endpoint"]),
            token_endpoint=str(doc["token_endpoint"]),
            jwks_uri=str(doc["jwks_uri"]),
            userinfo_endpoint=str(doc.get("userinfo_endpoint", "")),
            signing_algorithms=tuple(
                doc.get("id_token_signing_alg_values_supported") or ("RS256",)
            ),
            code_challenge_methods=tuple(doc.get("code_challenge_methods_supported") or ()),
            fetched_at=time.time(),
        )


# ------------------------------------------------------------ pending login
@dataclass(frozen=True)
class PendingLogin:
    """What we must remember between the redirect out and the callback back."""

    state: str
    nonce: str
    code_verifier: str
    created_at: float
    next_path: str = "/console"


class PendingLogins:
    """Single-use state/nonce/verifier store, in memory, with a TTL.

    In memory on purpose: a login that does not complete before the process
    restarts should fail closed rather than resume against a state nobody
    can vouch for any more.
    """

    def __init__(self, ttl: float = LOGIN_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._rows: dict[str, PendingLogin] = {}

    def start(self, next_path: str = "/console") -> PendingLogin:
        pending = PendingLogin(
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(24),
            # 43-128 characters of unreserved charset, per RFC 7636 §4.1.
            code_verifier=secrets.token_urlsafe(64),
            created_at=time.time(),
            next_path=next_path,
        )
        with self._lock:
            self._expire()
            self._rows[pending.state] = pending
        return pending

    def consume(self, state: str) -> PendingLogin:
        """Take the pending login for ``state``, once. Missing is a refusal."""
        with self._lock:
            self._expire()
            row = self._rows.pop(state, None)
        if row is None:
            raise TokenInvalid(
                "REFUSED: this callback presented a state parameter that helm "
                "did not issue, or that has already been used or expired. A "
                "login is completed exactly once, in the browser that started "
                "it."
            )
        return row

    def _expire(self) -> None:
        cutoff = time.time() - self._ttl
        for key in [k for k, v in self._rows.items() if v.created_at < cutoff]:
            self._rows.pop(key, None)

    def __len__(self) -> int:  # pragma: no cover - diagnostics
        with self._lock:
            return len(self._rows)


# ------------------------------------------------------------------ claims
@dataclass(frozen=True)
class VerifiedClaims:
    """An ID token that PASSED every check, and nothing that did not."""

    subject: str
    issuer: str
    audience: str
    email: str = ""
    email_verified: bool = False
    name: str = ""
    preferred_username: str = ""
    issued_at: int = 0
    expires_at: int = 0
    token_id: str = ""
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def issuer_host(self) -> str:
        return issuer_host(self.issuer)

    @property
    def display(self) -> str:
        return self.name or self.preferred_username or self.email or self.subject

    def to_dict(self) -> dict[str, Any]:
        """Everything a record may carry. No token material, ever."""
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "audience": self.audience,
            "email": self.email,
            "email_verified": self.email_verified,
            "preferred_username": self.preferred_username,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "token_id": self.token_id,
        }


# ------------------------------------------------------------------ client
class OidcClient:
    """The authorization-code + PKCE flow against one provider.

    The client id and secret arrive from the environment or the fleet secret
    store and are never written to disk, a log line, or a template. The only
    thing that leaves this class is a :class:`VerifiedClaims`.
    """

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str = "",
        redirect_uri: str,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        timeout: float = 4.0,
        offline: bool = False,
        ca_bundle: str = "",
        metadata_ttl: float = 3600.0,
        transport: Any = httpx,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = tuple(scopes)
        self.timeout = timeout
        self.offline = offline
        # An internally-signed issuer is trusted by NAMING ITS CA, never by
        # switching verification off. There is no flag here that can do the
        # latter: a TLS-unverified channel to the identity provider would
        # make every downstream "verified subject" a smaller claim than it
        # appears, which is the exact failure this module exists to end.
        self.ca_bundle = ca_bundle
        self.metadata_ttl = metadata_ttl
        self._http = transport
        self._metadata: ProviderMetadata | None = None
        self._jwks: dict[str, Any] = {}
        self._jwks_fetched_at = 0.0
        self._lock = threading.Lock()
        self.pending = PendingLogins()

    # ------------------------------------------------------------- config
    @property
    def configured(self) -> bool:
        """Is there enough here to attempt a real login at all?"""
        return bool(self.issuer and self.client_id)

    @property
    def unconfigured_reason(self) -> str:
        if self.issuer and self.client_id:
            return ""
        missing = []
        if not self.issuer:
            missing.append("HELM_OIDC_ISSUER")
        if not self.client_id:
            missing.append("HELM_OIDC_CLIENT_ID")
        return (
            f"{' and '.join(missing)} not set, so the OIDC round trip cannot run. "
            "The client id and secret come from the environment or the fleet "
            "secret store; they are never committed."
        )

    @property
    def host(self) -> str:
        return issuer_host(self.issuer)

    # ---------------------------------------------------------- discovery
    def discover(self, *, refresh: bool = False) -> ProviderMetadata:
        """Fetch (and cache) the discovery document. Short timeout, no retry."""
        if not self.configured:
            raise ProviderUnavailable(self.unconfigured_reason)
        if self.offline:
            raise ProviderUnavailable(
                "HELM_OFFLINE=1 — helm makes no outbound call in offline mode, "
                "so the OIDC provider cannot be reached by design."
            )
        with self._lock:
            cached = self._metadata
        fresh_enough = (
            cached is not None
            and not refresh
            and (time.time() - cached.fetched_at) < self.metadata_ttl
        )
        if cached is not None and fresh_enough:
            return cached
        url = f"{self.issuer}/.well-known/openid-configuration"
        doc = self._get_json(url, what="the OIDC discovery document")
        metadata = ProviderMetadata.from_document(doc)
        if metadata.issuer != self.issuer:
            raise TokenInvalid(
                f"REFUSED: {url} declares issuer {metadata.issuer!r}, but helm "
                f"is configured for {self.issuer!r}. A discovery document that "
                "names a different issuer is not this provider's."
            )
        with self._lock:
            self._metadata = metadata
        return metadata

    @property
    def _tls(self) -> dict[str, Any]:
        """Extra transport kwargs. Only ever ADDS trust, never removes it."""
        return {"verify": self.ca_bundle} if self.ca_bundle else {}

    def _get_json(self, url: str, *, what: str) -> dict[str, Any]:
        try:
            response = self._http.request("GET", url, timeout=self.timeout, **self._tls)
        except Exception as exc:  # httpx.HTTPError and any transport's kin
            raise ProviderUnavailable(
                f"could not reach {self.host} for {what} within "
                f"{self.timeout:g}s: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise ProviderUnavailable(
                f"{self.host} answered {response.status_code} for {what}"
            )
        try:
            doc = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(f"{what} from {self.host} is not JSON") from exc
        if not isinstance(doc, dict):
            raise ProviderUnavailable(f"{what} from {self.host} is not an object")
        return doc

    # --------------------------------------------------------------- jwks
    def jwks(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            cached, age = self._jwks, time.time() - self._jwks_fetched_at
        if cached and not refresh and age < self.metadata_ttl:
            return cached
        metadata = self.discover()
        doc = self._get_json(metadata.jwks_uri, what="the signing keys (JWKS)")
        keys = doc.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ProviderUnavailable(f"{self.host} published an empty JWKS")
        with self._lock:
            self._jwks = doc
            self._jwks_fetched_at = time.time()
        return doc

    def _key_for(self, kid: str, alg: str) -> Any:
        """The public key for ``kid``, refreshing the JWKS once if unknown."""
        import jwt  # imported here so the module loads without the extra

        for refresh in (False, True):
            doc = self.jwks(refresh=refresh)
            for entry in doc.get("keys", []):
                if kid and entry.get("kid") != kid:
                    continue
                entry_alg = entry.get("alg") or alg
                if entry_alg not in ALLOWED_ALGORITHMS:
                    continue
                return jwt.PyJWK(entry, algorithm=entry_alg).key
            if refresh:
                break
        raise TokenInvalid(
            f"REFUSED: the ID token is signed with key {kid!r}, which is not in "
            f"{self.host}'s published JWKS. helm re-fetched the key set once "
            "before refusing, so this is not a rotation lag."
        )

    # ------------------------------------------------------------- begin
    def begin(self, next_path: str = "/console") -> tuple[str, PendingLogin]:
        """The URL to send the browser to, and what to remember about it.

        PKCE is not optional here. Even with a confidential client, S256
        binds the code to THIS browser's exchange, so a code intercepted in
        a redirect cannot be redeemed by anyone else.
        """
        metadata = self.discover()
        if not metadata.supports_pkce_s256:
            raise ProviderUnavailable(
                f"REFUSED: {self.host} does not advertise the S256 code "
                "challenge method. helm does not fall back to a plain "
                "challenge, which would defeat the point of PKCE."
            )
        pending = self.pending.start(next_path)
        challenge = _b64url(hashlib.sha256(pending.code_verifier.encode("ascii")).digest())
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.scopes),
                "state": pending.state,
                "nonce": pending.nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{metadata.authorization_endpoint}?{query}", pending

    # ---------------------------------------------------------- callback
    def complete(self, *, code: str, state: str) -> VerifiedClaims:
        """Exchange the code and return claims that VERIFIED, or raise."""
        pending = self.pending.consume(state)
        if not code:
            raise TokenInvalid(
                "REFUSED: the callback carried no authorization code. Nothing "
                "was exchanged and no session was issued."
            )
        metadata = self.discover()
        id_token = self._exchange(metadata, code=code, verifier=pending.code_verifier)
        return self.verify_id_token(id_token, nonce=pending.nonce)

    def _exchange(self, metadata: ProviderMetadata, *, code: str, verifier: str) -> str:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        try:
            response = self._http.request(
                "POST",
                metadata.token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                **self._tls,
            )
        except Exception as exc:
            raise ProviderUnavailable(
                f"could not reach {self.host}'s token endpoint within "
                f"{self.timeout:g}s: {type(exc).__name__}"
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or not isinstance(body, dict):
            # The provider's own error CODE is safe to surface; its body may
            # contain token material, so it does not travel.
            raise TokenInvalid(
                f"REFUSED: {self.host} rejected the code exchange with HTTP "
                f"{response.status_code}"
                + (f" ({body.get('error')})" if isinstance(body, dict) and body.get("error") else "")
            )
        id_token = body.get("id_token")
        if not id_token or not isinstance(id_token, str):
            raise TokenInvalid(
                f"REFUSED: {self.host} returned no id_token. An access token "
                "alone says what a caller may do, never who they are — signet "
                "needs the identity assertion."
            )
        return id_token

    # ------------------------------------------------------------ verify
    def verify_id_token(self, id_token: str, *, nonce: str) -> VerifiedClaims:
        """Signature, issuer, audience, expiry and nonce. All of them."""
        import jwt

        metadata = self.discover()
        try:
            header = jwt.get_unverified_header(id_token)
        except Exception as exc:
            raise TokenInvalid(f"REFUSED: the ID token is not a well-formed JWT ({exc})") from exc

        alg = str(header.get("alg", ""))
        if alg not in ALLOWED_ALGORITHMS:
            raise TokenInvalid(
                f"REFUSED: the ID token declares alg={alg!r}. helm accepts only "
                f"{sorted(ALLOWED_ALGORITHMS)} — 'none' and the symmetric "
                "families are refused before a key is even looked up."
            )
        if alg not in metadata.signing_algorithms:
            raise TokenInvalid(
                f"REFUSED: the ID token declares alg={alg!r}, which "
                f"{self.host} does not advertise "
                f"({', '.join(metadata.signing_algorithms)})."
            )

        key = self._key_for(str(header.get("kid", "")), alg)
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[alg],
                audience=self.client_id,
                issuer=metadata.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except Exception as exc:
            # jwt raises a distinct class per failure; the class name is the
            # honest reason and carries no token material.
            raise TokenInvalid(
                f"REFUSED: the ID token from {self.host} did not verify — "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        presented = str(claims.get("nonce", ""))
        if not presented or not secrets.compare_digest(presented, nonce):
            raise TokenInvalid(
                "REFUSED: the ID token's nonce does not match the one helm sent "
                "with this authorization request. That is what a replayed or "
                "injected token looks like, so the session is not issued."
            )

        subject = str(claims.get("sub", ""))
        if not subject:
            raise TokenInvalid("REFUSED: the ID token carries no subject.")

        return VerifiedClaims(
            subject=subject,
            issuer=str(claims.get("iss", "")),
            audience=self.client_id,
            email=str(claims.get("email", "")),
            email_verified=bool(claims.get("email_verified", False)),
            name=str(claims.get("name", "")),
            preferred_username=str(
                claims.get("preferred_username") or claims.get("nickname") or ""
            ),
            issued_at=int(claims.get("iat", 0) or 0),
            expires_at=int(claims.get("exp", 0) or 0),
            token_id=str(claims.get("jti", "")),
            claims={k: v for k, v in claims.items() if k not in {"nonce"}},
        )
