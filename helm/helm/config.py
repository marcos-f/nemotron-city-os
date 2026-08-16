"""Configuration — environment only, no hidden defaults that lie.

Three rules carry acceptance weight here:

* Nothing that can MINT AN IDENTITY without a real credential may exist in
  staging or production. That is a boot REFUSAL which names every offending
  capability, not a check against one flag by name
  (``test://signet/auth-flag-env``).
* A protected environment with no identity provider configured is also a
  refusal. A console nobody can sign in to that still draws an approve
  button is not a safe degraded state.
* There is no mock/dev one-click sign-in as a product capability. A fixed
  identity may be constructed directly by tests; it is never reachable
  through an HTTP route.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

PROTECTED_ENVIRONMENTS = frozenset({"staging", "stage", "production", "prod"})


class BootRefused(RuntimeError):
    """Raised instead of booting when configuration would weaken the gate."""


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    host: str = "127.0.0.1"
    port: int = 8610

    throughline_url: str = "http://127.0.0.1:8600"
    docket_url: str = "http://127.0.0.1:8601"
    breaker_url: str = "http://127.0.0.1:8602"
    siren_url: str = "http://127.0.0.1:8603"
    blindspot_url: str = "http://127.0.0.1:8604"

    auth_disabled: bool = False
    github_client_id: str = ""
    github_client_secret: str = ""
    public_url: str = "http://127.0.0.1:8610"
    session_secret: str = "helm-dev-secret-not-for-production"

    # signet's real issuer. The client id and secret come from the
    # environment or the fleet secret store
    # (``.secrets/nemocity-signet.yaml``) and are never defaulted: an empty
    # client id means "no real provider configured here", which the login
    # page states in words rather than papering over with the mock.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid profile email read_user"
    oidc_redirect_path: str = "/auth/callback"
    # git.nemotron.example.com presents an internally-signed certificate. Name its CA
    # here (or via SSL_CERT_FILE / REQUESTS_CA_BUNDLE, which the loader also
    # reads). There is deliberately no setting that turns verification off:
    # a subject verified over an unverified channel is a weaker claim than
    # it looks, and a weaker claim that looks strong is the failure mode
    # this whole layer exists to remove.
    oidc_ca_bundle: str = ""
    # Short by design. The scripted demo runs offline, and a login page that
    # blocks on a dead provider fails worse than one that says so in 4s.
    oidc_timeout: float = 4.0

    # NemoClerk model ladder; the first reachable tier wins, and the active
    # tier is named in the rail header. Both top rungs are LOCAL: the demo
    # needs no internet.
    #   1. NVIDIA Nemotron on NVIDIA GB10 Grace Blackwell (DGX Spark), vLLM.
    #      Measured 0.6s for an 11-token turn with /no_think. An NVIDIA model
    #      on NVIDIA silicon, locally, with no internet — the only rung that
    #      makes the hardware claim without hedging.
    #   2. RTX 5090 llama.cpp qwen3.6-27b, ~82 tok/s. The only rung with a
    #      tool-call parser, so it can also CHOOSE tools; helm's grounding
    #      does not depend on that (the router runs the tools either way).
    #   The deepseek rung on :18000 is gone: the GB10's 121GiB cannot host
    #   both, and the operator traded it for the Nemotron above.
    agent_model_url: str = "http://dgx-spark.nemotron.example.com:8000/v1"
    agent_model_name: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    fallback_model_url: str = "http://rtx-workstation.nemotron.example.com:9997/v1"
    fallback_model_name: str = "qwen3.6-27b"
    fleet_model_url: str = ""
    fleet_model_name: str = "qwen2.5-32b-instruct"
    hosted_model_url: str = "https://integrate.api.nvidia.com/v1"
    hosted_model_name: str = "nvidia/nemotron-3-super-120b-a12b"
    nvidia_api_key: str = ""
    # The GB10 deployment runs --enforce-eager at ~5 tok/s with
    # --max-num-seqs 2. Answers stay short and calls stay serialized.
    model_max_tokens: int = 220
    model_timeout: float = 90.0
    model_probe_timeout: float = 3.0

    data_dir: str = "data"
    cache_dir: str = "data/model-cache"
    offline: bool = False

    sibling_timeout: float = 2.5

    blockers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_protected_env(self) -> bool:
        return self.env.strip().lower() in PROTECTED_ENVIRONMENTS

    @property
    def oidc_configured(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)

    @property
    def oidc_redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}{self.oidc_redirect_path}"

    @property
    def oidc_scope_tuple(self) -> tuple[str, ...]:
        return tuple(s for s in self.oidc_scopes.replace(",", " ").split() if s)

    @property
    def sibling_urls(self) -> dict[str, str]:
        return {
            "throughline": self.throughline_url,
            "docket": self.docket_url,
            "breaker": self.breaker_url,
            "siren": self.siren_url,
            "blindspot": self.blindspot_url,
        }


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Build settings from the environment and REFUSE unsafe combinations."""
    env = dict(os.environ if environ is None else environ)

    def get(name: str, default: str = "") -> str:
        return env.get(name, default).strip()

    def flag(name: str) -> bool:
        return env.get(name, "0").strip().lower() in {"1", "true", "yes", "on"}

    settings = Settings(
        env=get("HELM_ENV", "dev") or "dev",
        host=get("HELM_HOST", "127.0.0.1"),
        port=int(get("HELM_PORT", "8610") or 8610),
        throughline_url=get("THROUGHLINE_URL", "http://127.0.0.1:8600"),
        docket_url=get("DOCKET_URL", "http://127.0.0.1:8601"),
        breaker_url=get("BREAKER_URL", "http://127.0.0.1:8602"),
        siren_url=get("SIREN_URL", "http://127.0.0.1:8603"),
        blindspot_url=get("BLINDSPOT_URL", "http://127.0.0.1:8604"),
        auth_disabled=flag("AUTH_DISABLED"),
        github_client_id=get("GITHUB_CLIENT_ID"),
        github_client_secret=get("GITHUB_CLIENT_SECRET"),
        public_url=get("HELM_PUBLIC_URL", "http://127.0.0.1:8610"),
        session_secret=get("HELM_SESSION_SECRET", "helm-dev-secret-not-for-production"),
        # The fleet store's own `apply-env.sh` exports the nemocity-signet
        # bundle as lower-case `oidc_*`; the explicit HELM_ names win when
        # both are present, so a deployment can override the fleet default
        # without editing the store.
        oidc_issuer=get("HELM_OIDC_ISSUER") or get("oidc_issuer"),
        oidc_client_id=get("HELM_OIDC_CLIENT_ID") or get("oidc_client_id"),
        oidc_client_secret=get("HELM_OIDC_CLIENT_SECRET") or get("oidc_client_secret"),
        oidc_scopes=(
            get("HELM_OIDC_SCOPES")
            or get("oidc_scopes")
            or "openid profile email read_user"
        ),
        oidc_redirect_path=get("HELM_OIDC_REDIRECT_PATH", "/auth/callback"),
        oidc_timeout=float(get("HELM_OIDC_TIMEOUT", "4") or 4),
        oidc_ca_bundle=(
            get("HELM_OIDC_CA_BUNDLE")
            or get("SSL_CERT_FILE")
            or get("REQUESTS_CA_BUNDLE")
        ),
        agent_model_url=get(
            "NEMOCLERK_BASE_URL",
            get("AGENT_MODEL_URL", "http://dgx-spark.nemotron.example.com:8000/v1"),
        ),
        agent_model_name=get(
            "NEMOCLERK_MODEL",
            get("AGENT_MODEL_NAME", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"),
        ),
        fallback_model_url=get(
            "NEMOCLERK_FALLBACK_BASE_URL", "http://rtx-workstation.nemotron.example.com:9997/v1"
        ),
        fallback_model_name=get("NEMOCLERK_FALLBACK_MODEL", "qwen3.6-27b"),
        fleet_model_url=get("FLEET_MODEL_URL"),
        fleet_model_name=get("FLEET_MODEL_NAME", "qwen2.5-32b-instruct"),
        hosted_model_url=get("HOSTED_MODEL_URL", "https://integrate.api.nvidia.com/v1"),
        hosted_model_name=get(
            "HOSTED_MODEL_NAME", "nvidia/nemotron-3-super-120b-a12b"
        ),
        nvidia_api_key=get("NVIDIA_API_KEY"),
        model_max_tokens=int(get("NEMOCLERK_MAX_TOKENS", "220") or 220),
        model_timeout=float(get("NEMOCLERK_TIMEOUT", "90") or 90),
        model_probe_timeout=float(get("NEMOCLERK_PROBE_TIMEOUT", "3") or 3),
        data_dir=get("HELM_DATA_DIR", "data"),
        cache_dir=get("HELM_CACHE_DIR", "data/model-cache"),
        offline=flag("HELM_OFFLINE"),
        sibling_timeout=float(get("HELM_SIBLING_TIMEOUT", "2.5") or 2.5),
    )
    enforce_auth_flag_policy(settings)
    return settings


#: Every setting that can MINT AN IDENTITY without a real credential,
#: named so the policy can be asserted as a LIST rather than as behaviour.
IDENTITY_MINTING_FLAGS: tuple[tuple[str, str], ...] = (
    ("AUTH_DISABLED", "serves the console with authentication switched off"),
)

#: The default that ships in this file. Left in place it is not a secret,
#: it is a published one: anyone with the source can forge a session cookie.
DEFAULT_SESSION_SECRET = "helm-dev-secret-not-for-production"


def identity_minting_capabilities(settings: Settings) -> list[str]:
    """Everything in THIS configuration that can produce a principal.

    The refusal used to be attached to ``AUTH_DISABLED`` by name, which
    made it a list of one that three later additions walked straight
    around. It is attached to the CAPABILITY now — "can this setting mint
    an identity without a real credential?" — so the next flag someone adds
    is refused by default instead of being missed. Each entry is a
    ``(name, what it does)`` pair, because a refusal you cannot act on is
    just an outage.
    """
    found: list[tuple[str, str]] = []
    if settings.auth_disabled:
        found.append(
            ("AUTH_DISABLED", "serves the console with authentication switched off")
        )
    if settings.session_secret == DEFAULT_SESSION_SECRET:
        found.append(
            (
                "HELM_SESSION_SECRET",
                "is still the built-in development value, so anyone holding "
                "this source can forge a session cookie for any subject",
            )
        )
    return [f"{name} — {what}" for name, what in found]


def enforce_auth_flag_policy(settings: Settings) -> None:
    """Nothing that can mint an identity may exist outside dev and test.

    ``test://signet/auth-flag-env``. A console whose signet identity is not
    load-bearing writes approval records that cannot be falsified, which is
    worse than writing none: they look exactly as trustworthy as true ones.
    """
    if not settings.is_protected_env:
        return
    offenders = identity_minting_capabilities(settings)
    if offenders:
        raise BootRefused(
            f"REFUSED: helm will not boot in HELM_ENV={settings.env!r} while "
            "anything can mint an identity without a real credential. "
            + "; ".join(offenders)
            + ". These are honoured in dev and test only."
        )
    if not settings.oidc_configured and not settings.github_client_id:
        raise BootRefused(
            f"REFUSED: helm will not boot in HELM_ENV={settings.env!r} with no "
            "identity provider configured. Set HELM_OIDC_ISSUER and "
            "HELM_OIDC_CLIENT_ID (the secret store bundle nemocity-signet "
            "carries both). Refusing to start is the honest outcome: the "
            "alternative is a console nobody can sign in to that still "
            "presents an approve button."
        )
