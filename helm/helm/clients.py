"""Clients for the real substrate and the sibling feeds.

No component is mocked here. A sibling that is not running is reported as
``offline`` with the reason, and the console renders its wireframe pane
labelled "component offline" — the honest state, not a fabricated one.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from helm.signet import current_attestation

log = logging.getLogger("helm.clients")

# --------------------------------------------------------------- caller token

#: throughline authenticates the CALLER — the process on the other end of the
#: socket — on the privileged half of its write surface: ``POST
#: /approvals/{id}/decide``, ``authz.*`` effects and ``warrant.*`` signals.
#: Ordinary signals, judgments and untyped reversible effects are untouched.
CALLER_TOKEN_HEADER = "X-Throughline-Caller-Token"

#: The substrate's own name for this refusal, echoed in its 403 body and in
#: its ledger. helm reuses the substrate's word rather than inventing one, so
#: the same string is greppable on both sides of the socket.
REFUSAL_CALLER_TOKEN = "caller-token-required"

#: The file the substrate mints for itself, 0600, inside its data directory.
CALLER_TOKEN_FILENAME = "caller-token"

#: Effect types and signal classes the substrate treats as privileged. Kept
#: as prefixes, matching throughline's own ``_is_privileged_*`` predicates, so
#: helm asks for a token on exactly the set the substrate demands one for —
#: no wider (which would break ordinary traffic) and no narrower (which would
#: 403).
PRIVILEGED_EFFECT_PREFIX = "authz."
PRIVILEGED_SIGNAL_PREFIX = "warrant."

#: The one sentence this failure is allowed to be. A caller that could not
#: authenticate was never refused BY THE GATE — the gate never saw the act —
#: and the two must never render as the same outcome.
NO_CALLER_TOKEN = "throughline requires a caller token and none was configured"

#: The same distinction on the other side: we HAD a token and the substrate
#: did not accept it. Still authentication, still not a gate refusal.
CALLER_TOKEN_REJECTED = "throughline rejected the caller token helm presented"


@dataclass(frozen=True)
class CallerToken:
    """The token helm can present, and where it came from.

    Resolved ONCE per client, at construction. Re-reading it per request
    would turn a 0600 file into a hot path and would let the answer change
    mid-session, which is exactly the kind of drift that makes an auth
    failure hard to explain afterwards.
    """

    value: str = ""
    source: str = "absent"
    detail: str = ""

    @property
    def present(self) -> bool:
        return bool(self.value)


def read_caller_token(environ: dict[str, str] | None = None) -> CallerToken:
    """``THROUGHLINE_CALLER_TOKEN`` first, then ``<data-dir>/caller-token``.

    The file being absent is not an error here: a federation booted without
    the substrate, or a unit test with no substrate at all, must still
    construct its clients. What an absent token does is make the PRIVILEGED
    calls fail by name — see :data:`NO_CALLER_TOKEN` — while every ordinary
    call carries on exactly as before.
    """
    env = dict(os.environ if environ is None else environ)
    from_env = (env.get("THROUGHLINE_CALLER_TOKEN") or "").strip()
    if from_env:
        return CallerToken(from_env, "environment")
    data_dir = env.get("THROUGHLINE_DATA_DIR", "data")
    path = Path(data_dir) / CALLER_TOKEN_FILENAME
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return CallerToken("", "absent", f"{path} could not be read: {exc.strerror}")
    if not token:
        return CallerToken("", "absent", f"{path} is empty")
    return CallerToken(token, str(path))


def _human_error(body: Any) -> str:
    """A sentence, not a Python repr.

    ``str(body)`` on a decoded JSON dict produced ``{'detail': "..."}`` —
    single quotes and all — which then rendered verbatim into the page at
    the irreversible-decision moment. The user should never meet our
    interpreter.
    """
    if isinstance(body, dict):
        detail = body.get("detail", body)
        if isinstance(detail, str):
            return detail[:500]
        if isinstance(detail, list):
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    where = ".".join(str(x) for x in item.get("loc", [])[1:])
                    parts.append(f"{where}: {item.get('msg', 'invalid')}".strip(": "))
                else:
                    parts.append(str(item))
            return "; ".join(parts)[:500]
        if isinstance(detail, dict):
            for key in ("reason", "message", "detail", "error"):
                if isinstance(detail.get(key), str):
                    return str(detail[key])[:500]
    if isinstance(body, str):
        return body[:500]
    return "the request was refused"


@dataclass
class Reply:
    """The result of one call to a sibling service."""

    ok: bool
    status: int = 0
    data: Any = None
    error: str = ""
    #: The substrate's own named reason, when it published one. Empty for a
    #: transport failure, and for a success.
    refusal_reason: str = ""

    @property
    def offline(self) -> bool:
        return self.status == 0 and not self.ok

    @property
    def unauthenticated(self) -> bool:
        """We could not authenticate. THIS IS NOT A REFUSAL BY THE GATE.

        Two review rounds went on exactly this distinction. A 403 because the
        caller presented no valid token means the act never reached the rule
        that would have judged it — the gate did not decline the effect, it
        never saw it. Rendering the two the same way tells an operator their
        approval was denied on the merits when in fact nobody was asked.
        """
        return self.refusal_reason == REFUSAL_CALLER_TOKEN


@dataclass
class ServiceStatus:
    name: str
    url: str
    online: bool
    detail: str = ""
    health: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return "online" if self.online else "component offline"


class SiblingClient:
    """Thin HTTP client over one sibling service on its fixed port."""

    def __init__(
        self,
        name: str,
        base_url: str,
        timeout: float = 2.5,
        caller_token: CallerToken | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Resolved once, here, not per request. An absent token is a normal
        # constructible state: the client still exists and every ordinary
        # call still works.
        self.caller_token = read_caller_token() if caller_token is None else caller_token

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        privileged: bool = False,
        act: str = "",
    ) -> Reply:
        """One call. ``privileged`` marks the acts throughline authenticates.

        A privileged call with no token is NOT SENT. There is nothing to be
        gained from making the round trip — the answer is already known — and
        not sending it keeps the failure unambiguous: we cannot say the gate
        refused something we never asked it.
        """
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}
        if privileged:
            if not self.caller_token.present:
                where = self.caller_token.detail or "no token file and no environment variable"
                log.warning(
                    "%s %s %s not attempted: %s (%s)",
                    self.name, method, path, NO_CALLER_TOKEN, where,
                )
                return Reply(
                    ok=False,
                    # 401, not 403: this is "we did not authenticate", which is
                    # a different sentence from "you may not".
                    status=401,
                    error=(
                        f"{NO_CALLER_TOKEN}. Set THROUGHLINE_CALLER_TOKEN, or run "
                        f"where the substrate's {CALLER_TOKEN_FILENAME} file is "
                        f"readable ({where})."
                    ),
                    refusal_reason=REFUSAL_CALLER_TOKEN,
                    data={
                        "refused": True,
                        "refusal_reason": REFUSAL_CALLER_TOKEN,
                        "authentication": False,
                        "act": act or f"{method} {path}",
                        "attempted": False,
                    },
                )
            headers[CALLER_TOKEN_HEADER] = self.caller_token.value
        try:
            response = httpx.request(
                method,
                url,
                json=json,
                params=params,
                headers=headers or None,
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:  # connection refused, DNS, timeout
            log.info("%s unreachable at %s: %s", self.name, url, exc)
            return Reply(ok=False, status=0, error=f"{type(exc).__name__}: {exc}")
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text
        named = ""
        if isinstance(body, dict):
            named = str(body.get("refusal_reason") or "")
        error = "" if response.is_success else _human_error(body)
        if named == REFUSAL_CALLER_TOKEN:
            # The substrate saw a token and did not accept it. Say so in our
            # own words, in front of its words, so the sentence a reader
            # meets first is the one that is true about what went wrong.
            error = (
                f"{CALLER_TOKEN_REJECTED} (source: {self.caller_token.source}). "
                f"{error}"
            ).strip()
        return Reply(
            ok=response.is_success,
            status=response.status_code,
            data=body,
            error=error,
            refusal_reason=named,
        )

    def get(self, path: str, **kw: Any) -> Reply:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Reply:
        return self.request("POST", path, **kw)

    def status(self, health_path: str = "/healthz") -> ServiceStatus:
        reply = self.get(health_path)
        return ServiceStatus(
            name=self.name,
            url=self.base_url,
            online=reply.ok,
            detail="" if reply.ok else (reply.error or "not reachable"),
            health=reply.data if isinstance(reply.data, dict) else {},
        )


class Throughline(SiblingClient):
    """The substrate. helm never keeps its own ledger."""

    def approvals(self, state: str | None = None) -> Reply:
        return self.get("/approvals", params={"state": state} if state else None)

    def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        decided_by: str,
        caller_role: str,
        rationale: str | None = None,
    ) -> Reply:
        payload: dict[str, Any] = {
            "decision": decision,
            "decided_by": decided_by,
            "caller_role": caller_role,
        }
        if rationale:
            payload["rationale"] = rationale
        # WHO approved, and WHICH AUTHORITY vouched for them. The issuer is
        # attached here — at the last boundary before the substrate — from
        # the attestation the request handler bound after verifying the
        # session cookie, so no layer in between can supply or alter one.
        # It is attached only when it belongs to the same subject that is
        # being written, and a mock session carries no issuer at all: the
        # record then says "mock" rather than naming an authority that
        # never spoke for anybody.
        attestation = current_attestation()
        if attestation.subject and attestation.subject == decided_by:
            payload["auth_mode"] = attestation.auth_mode
            if attestation.issuer:
                payload["issuer"] = attestation.issuer
        # A decide is on throughline's PRIVILEGED write surface: it is the one
        # path to execution, so the substrate authenticates the caller before
        # it will even look at the decision. Without a token this returns
        # unsent, named as an authentication failure.
        return self.post(
            f"/approvals/{approval_id}/decide",
            json=payload,
            timeout=10.0,
            privileged=True,
            act="approval.decide",
        )

    def ledger(
        self, limit: int = 25, offset: int | None = None, *, privileged: bool = False
    ) -> Reply:
        """The tail by default; one page of the chain when ``offset`` is given.

        ``limit`` is the substrate's per-page cap, not a ceiling on what may
        be known — a consumer that must see the whole chain walks it with
        ``offset``. See ``helm.warrant.projection.read_chain``.

        ``privileged``: throughline pseudonymizes ``decided_by``/``approved_by``
        on this read unless the caller presents its caller token — the same
        protection this codebase gives sibling addresses, applied by the
        substrate to decider identity. helm's own public routes (``/ledger``,
        the guest console) must keep asking WITHOUT the token, exactly like
        any other anonymous reader, so an anonymous caller never sees more
        than throughline itself would show one. Only a caller helm has
        already verified as admin may ask WITH the token, mirroring
        ``reveal_addresses`` elsewhere in this codebase.
        """
        params: dict[str, Any] = {"limit": limit}
        if offset is not None:
            params["offset"] = offset
        # A read must never go UNSENT for lack of a token — that turns "we
        # would reveal more" into "the ledger is unreadable". Ask with the
        # token only when one is actually configured; without one this is
        # the exact same public read it always was.
        return self.get(
            "/ledger", params=params, privileged=privileged and self.caller_token.present
        )

    def verify(self) -> Reply:
        return self.get("/ledger/verify")

    def walk(self, effect_id: str) -> Reply:
        return self.get(f"/effects/{effect_id}/walk")

    def config(self) -> Reply:
        return self.get("/config")

    def signal(self, payload: dict[str, Any]) -> Reply:
        """Append an auditable record by way of a signal ingest.

        helm has no ledger of its own; an administrative act that must be on
        the record is ingested as a signal so it lands in throughline's chain.

        A ``warrant.*`` signal carries the structured record an authorization
        effect is projected from — it IS part of the permission graph — so
        the substrate authenticates its caller. Every other class is ordinary
        fleet traffic and routes exactly as it always has.
        """
        signal_class = str(payload.get("class") or payload.get("signal_class") or "")
        return self.post(
            "/signals",
            json=payload,
            privileged=signal_class.startswith(PRIVILEGED_SIGNAL_PREFIX),
            act="signal.ingest",
        )

    def effect(self, payload: dict[str, Any]) -> Reply:
        """Propose an effect against the gate.

        The registry classifies it, not the caller: an irreversible effect
        comes back ``queued`` with an ``approval_id`` and does not run. warrant
        uses this for every authorization change, so a grant is gated and
        chained exactly like a load shed.

        An ``authz.*`` effect IS a change to who may do what, so it needs an
        authenticated caller. An ordinary effect does not, and is posted with
        no header at all.
        """
        effect_type = str(payload.get("effect_type") or payload.get("type") or "")
        return self.post(
            "/effects",
            json=payload,
            timeout=10.0,
            privileged=effect_type.startswith(PRIVILEGED_EFFECT_PREFIX),
            act="effect.propose",
        )


class Federation:
    """Every sibling, keyed by name, plus the substrate."""

    def __init__(self, urls: dict[str, str], timeout: float = 2.5) -> None:
        # One read of the token for the whole federation, at construction.
        # Every client shares it, so /healthz and a decide can never disagree
        # about whether helm is able to authenticate.
        self.caller_token = read_caller_token()
        self.throughline = Throughline(
            "throughline", urls["throughline"], timeout, self.caller_token
        )
        self.docket = SiblingClient("docket", urls["docket"], timeout, self.caller_token)
        self.breaker = SiblingClient("breaker", urls["breaker"], timeout, self.caller_token)
        self.siren = SiblingClient("siren", urls["siren"], timeout, self.caller_token)
        self.blindspot = SiblingClient(
            "blindspot", urls["blindspot"], timeout, self.caller_token
        )

    @property
    def all(self) -> dict[str, SiblingClient]:
        return {
            "throughline": self.throughline,
            "docket": self.docket,
            "breaker": self.breaker,
            "siren": self.siren,
            "blindspot": self.blindspot,
        }

    def statuses(self) -> dict[str, ServiceStatus]:
        return {name: client.status() for name, client in self.all.items()}
