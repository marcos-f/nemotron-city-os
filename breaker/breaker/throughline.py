"""Clients for the throughline substrate — one real, one mock, one contract.

breaker never executes its own irreversible effect. It hands the effect to
throughline's gate and then **checks that the gate held it**: a substrate that
came back with anything other than a held effect is a gate violation, and
breaker refuses to dispatch rather than assuming the best.

The mock exists so breaker can be built and tested with no substrate running.
It implements the same hold semantics the real gate implements — held with no
deadline, released only by a decision carrying a subject — because a mock that
is easier to satisfy than the real thing is how integration surprises are
manufactured. `mode` is reported everywhere so no screen can claim a real gate
where the mock ran.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

import httpx

DEFAULT_URL = "http://127.0.0.1:8600"

# --------------------------------------------------------------- caller token

#: throughline authenticates the CALLER — the process on the other end of the
#: socket — on the half of its write surface that changes who may do what.
#: breaker touches exactly one of those acts: ``POST /approvals/{id}/decide``.
#: Its proposals, signals and probes are ordinary fleet traffic and carry no
#: header at all, which is what keeps this deployable without a flag day.
CALLER_TOKEN_HEADER = "X-Throughline-Caller-Token"

#: The substrate's own name for the refusal. Reused, not reinvented, so the
#: same string is greppable on both sides of the socket.
REFUSAL_CALLER_TOKEN = "caller-token-required"

CALLER_TOKEN_FILENAME = "caller-token"

#: The one sentence this failure is allowed to be.
NO_CALLER_TOKEN = "throughline requires a caller token and none was configured"


def read_caller_token(environ: Optional[dict] = None) -> tuple[str, str]:
    """``(token, source)``. ``THROUGHLINE_CALLER_TOKEN``, else the 0600 file.

    Reading the file IS the authentication: it is mode 0600 inside the
    substrate's own data directory, so being able to read it means being the
    user the substrate runs as.

    An absent file is not an error. breaker must still construct — and must
    still hold, propose and probe — with no substrate anywhere near it.
    """
    env = dict(os.environ if environ is None else environ)
    from_env = (env.get("THROUGHLINE_CALLER_TOKEN") or "").strip()
    if from_env:
        return from_env, "environment"
    path = Path(env.get("THROUGHLINE_DATA_DIR", "data")) / CALLER_TOKEN_FILENAME
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return "", f"absent ({path}: {exc.strerror})"
    return (token, str(path)) if token else ("", f"absent ({path} is empty)")

#: Statuses throughline reports for an effect that is being held for a human.
HELD_STATUSES = frozenset({"queued", "waiting_at_gate", "proposed"})


class GateViolation(RuntimeError):
    """An irreversible effect was not held. Nothing dispatches after this."""


class SubstrateUnreachable(RuntimeError):
    """The substrate could not be reached. breaker holds rather than acts."""


class SubstrateRefused(SubstrateUnreachable):
    """The substrate WAS reached, and refused. That is not the same thing.

    A gate that answers 403 "caller_role is required" is working perfectly:
    it received the request, applied its policy and declined. Reporting that
    as "unreachable" tells an operator to go looking for a dead process when
    the process is alive and the *request* is wrong — the same class of
    illegibility as a bare 500 for a dead dependency, pointing the other way.

    It subclasses ``SubstrateUnreachable`` deliberately: every existing
    ``except SubstrateUnreachable`` still catches it, so breaker keeps
    holding on a refusal exactly as it did before. What changes is only what
    the caller is told.
    """

    def __init__(self, message: str, *, status: int, body: Any = None,
                 method: str = "", path: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.method = method
        self.path = path

    @property
    def refusal(self) -> dict[str, Any]:
        """The substrate's own refusal record, when it published one."""
        return self.body if isinstance(self.body, dict) else {}

    @property
    def refusal_reason(self) -> str:
        return str(self.refusal.get("refusal_reason") or "")


class CallerTokenMissing(SubstrateRefused):
    """WE COULD NOT AUTHENTICATE. THE GATE DID NOT REFUSE THE EFFECT.

    The distinction this class exists to preserve, and which two review rounds
    were spent on: a 403 because the caller presented no valid token means the
    act never reached the rule that would have judged it. The gate did not
    decline the decision — it never saw it. An operator told "refused"
    concludes their approval was considered and denied; it was not, and the
    fix is a different one (configure a token) from the fix for a refusal
    (be a different principal, or accept the answer).

    It subclasses ``SubstrateRefused`` — and so, transitively,
    ``SubstrateUnreachable`` — deliberately: every existing fail-closed
    ``except`` still catches it and breaker still holds. What changes is only
    what the caller is told.
    """

    def __init__(self, message: str, *, status: int = 401, body: Any = None,
                 method: str = "", path: str = "", token_source: str = "") -> None:
        super().__init__(message, status=status, body=body, method=method, path=path)
        self.token_source = token_source


#: Caller roles throughline recognises. breaker relays a role; it does not
#: invent one, and it will not send a role the gate has never heard of.
CALLER_ROLES = ("human", "agent")

#: breaker's decide surface exists to relay a HUMAN's decision — the release
#: of an irreversible load shed. That is the default, and it is a default
#: rather than a hard-coding so an agent caller can declare itself honestly
#: and be refused by the gate on the record.
DEFAULT_CALLER_ROLE = "human"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ThroughlineClient(Protocol):
    mode: str

    def post_signal(self, signal: dict[str, Any]) -> dict[str, Any]: ...
    def post_judgment(self, judgment: dict[str, Any]) -> dict[str, Any]: ...
    def post_effect(self, effect: dict[str, Any]) -> dict[str, Any]: ...
    def get_effect(self, effect_id: str) -> dict[str, Any]: ...
    def approvals(self) -> list[dict[str, Any]]: ...
    def decide(self, approval_id: str, decision: str, decided_by: str,
               rationale: Optional[str] = None,
               caller_role: str = DEFAULT_CALLER_ROLE,
               auth_mode: str = "", issuer: str = "") -> dict[str, Any]: ...
    def walk(self, effect_id: str) -> dict[str, Any]: ...
    def health(self) -> dict[str, Any]: ...


def assert_held(effect: dict[str, Any], mode: str) -> dict[str, Any]:
    """The check that makes the hold breaker's claim rather than a hope."""
    status = effect.get("status")
    reversibility = effect.get("reversibility") or effect.get("reversibility_class")
    if reversibility != "irreversible":
        raise GateViolation(
            f"[{mode}] dispatch effect {effect.get('id')} came back as "
            f"{reversibility!r}; a load-shed dispatch is irreversible"
        )
    if status not in HELD_STATUSES:
        raise GateViolation(
            f"[{mode}] dispatch effect {effect.get('id')} was not held: "
            f"status={status!r}. breaker will not dispatch an effect the gate "
            f"did not hold"
        )
    return effect


class HttpThroughlineClient:
    """The real client: throughline on :8600, per its published contract."""

    mode = "real"

    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 10.0,
                 caller_token: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        # NOTE: this timeout is the HTTP read timeout for one request. It has
        # nothing to do with how long an approval may wait — the gate holds
        # forever, and breaker never polls with a deadline. See
        # tests/test_proposal_flow.py::test_no_timeout_path_exists.
        self._timeout = timeout
        # Resolved ONCE, here. Re-reading a 0600 file per request would put it
        # on a hot path and would let the answer change mid-session, which is
        # exactly what makes an auth failure impossible to explain afterwards.
        if caller_token is None:
            self.caller_token, self.caller_token_source = read_caller_token()
        else:
            self.caller_token, self.caller_token_source = caller_token, "given"

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 privileged: bool = False, act: str = "") -> Any:
        headers: dict[str, str] = {}
        if privileged:
            if not self.caller_token:
                # NOT SENT. There is nothing to learn from the round trip, and
                # not making it keeps the failure unambiguous: we cannot say
                # the gate refused something we never asked it.
                raise CallerTokenMissing(
                    f"{NO_CALLER_TOKEN} ({self.caller_token_source}); breaker "
                    f"did not send {method} {path}, so the gate never saw it "
                    f"and nothing was decided. Set THROUGHLINE_CALLER_TOKEN, "
                    f"or run where the substrate's {CALLER_TOKEN_FILENAME} "
                    f"file is readable.",
                    body={
                        "refused": True,
                        "refusal_reason": REFUSAL_CALLER_TOKEN,
                        "authenticated": False,
                        "attempted": False,
                        "act": act or f"{method} {path}",
                    },
                    method=method, path=path,
                    token_source=self.caller_token_source,
                )
            headers[CALLER_TOKEN_HEADER] = self.caller_token
        try:
            response = httpx.request(
                method, f"{self.base_url}{path}", json=body,
                headers=headers or None, timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SubstrateUnreachable(f"throughline at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
            named = body.get("refusal_reason") if isinstance(body, dict) else None
            if named == REFUSAL_CALLER_TOKEN:
                # We HAD a token and it was not accepted. Still authentication,
                # still not a decision about the effect.
                raise CallerTokenMissing(
                    f"throughline rejected breaker's caller token "
                    f"(source: {self.caller_token_source}) on {method} {path}; "
                    f"this is an AUTHENTICATION failure between breaker and the "
                    f"substrate, not a refusal of the effect — the gate never "
                    f"judged it and the effect is still held",
                    status=response.status_code, body=body,
                    method=method, path=path,
                    token_source=self.caller_token_source,
                )
            if response.status_code < 500:
                # Reached, and refused. The gate is alive and applied its
                # policy; saying "unreachable" would send an operator hunting
                # a dead process that is running fine.
                raise SubstrateRefused(
                    f"throughline {method} {path} -> {response.status_code}: "
                    f"{response.text}",
                    status=response.status_code, body=body,
                    method=method, path=path,
                )
            raise SubstrateUnreachable(
                f"throughline {method} {path} -> {response.status_code}: {response.text}"
            )
        return response.json()

    def post_signal(self, signal): return self._request("POST", "/signals", signal)
    def post_judgment(self, judgment): return self._request("POST", "/judgments", judgment)
    def post_effect(self, effect): return self._request("POST", "/effects", effect)
    def get_effect(self, effect_id): return self._request("GET", f"/effects/{effect_id}")
    def walk(self, effect_id): return self._request("GET", f"/effects/{effect_id}/walk")
    def health(self): return self._request("GET", "/healthz")

    def approvals(self):
        return self._request("GET", "/approvals")["approvals"]

    def decide(self, approval_id, decision, decided_by, rationale=None,
               caller_role=DEFAULT_CALLER_ROLE, auth_mode="", issuer=""):
        # caller_role is REQUIRED by the gate and omitting it is a refusal,
        # not a quiet pass — throughline made the safe path mandatory after a
        # security review, and breaker was still sending the old shape.
        #
        # A decide is also the one act breaker performs on throughline's
        # PRIVILEGED write surface, so it carries the caller token. Without
        # one it is not sent at all, and the failure says it is an
        # authentication problem rather than passing a bare 403 up as though
        # the gate had considered and declined the release.
        #
        # ``auth_mode``/``issuer`` are the ATTESTATION releasing an
        # irreversible effect now requires: who authenticated the approver.
        # They are RELAYED, exactly like caller_role — breaker does not
        # possess an identity provider and must never invent one. Sending a
        # fabricated `auth_mode: oidc` would be forging the very claim the
        # gate is asking for. So when breaker was told nothing, it SENDS
        # nothing, and the gate refuses the release on the ledger — which is
        # the correct outcome, and a visible one.
        payload = {
            "decision": decision, "decided_by": decided_by, "rationale": rationale,
            "caller_role": caller_role,
        }
        if auth_mode:
            payload["auth_mode"] = auth_mode
        if issuer:
            payload["issuer"] = issuer
        return self._request("POST", f"/approvals/{approval_id}/decide", payload,
                             privileged=True, act="approval.decide")


class MockThroughlineClient:
    """A stand-in that holds as hard as the real gate does.

    Labelled ``mock`` in every record it returns and in /healthz, so a screen
    reading breaker cannot claim a real substrate.
    """

    mode = "mock"

    def __init__(self, caller_token: Optional[str] = None) -> None:
        # Resolved the same way the real client resolves it, and enforced on
        # the same act. A mock that let a token-less decide through would be
        # easier to satisfy than the real gate — this module's own docstring
        # says that is how integration surprises are manufactured, and it is
        # precisely how the caller-role defect reached main: every mocked test
        # passed while :8600 had started refusing the shape breaker sent.
        if caller_token is None:
            self.caller_token, self.caller_token_source = read_caller_token()
        else:
            self.caller_token, self.caller_token_source = caller_token, "given"
        self._lock = threading.Lock()
        self.signals: dict[str, dict] = {}
        self.judgments: dict[str, dict] = {}
        self.effects: dict[str, dict] = {}
        self._approvals: dict[str, dict] = {}
        self.ledger: list[dict] = []

    def _append(self, kind: str, body: dict) -> None:
        self.ledger.append({"seq": len(self.ledger) + 1, "type": kind,
                            "ts": _now(), "body": body})

    def post_signal(self, signal):
        with self._lock:
            record = {**signal, "id": signal.get("id") or f"sig-{uuid.uuid4().hex[:10]}"}
            self.signals[record["id"]] = record
            self._append("signal.ingested", record)
            return record

    def post_judgment(self, judgment):
        with self._lock:
            record = {**judgment, "id": judgment.get("id") or f"jud-{uuid.uuid4().hex[:10]}"}
            self.judgments[record["id"]] = record
            self._append("judgment.recorded", record)
            return record

    def post_effect(self, effect):
        with self._lock:
            reversibility = effect.get("reversibility") or "irreversible"
            record = {
                **effect,
                "id": effect.get("id") or f"eff-{uuid.uuid4().hex[:10]}",
                "reversibility": reversibility,
                "reversibility_class": reversibility,
                "mode": "mock",
            }
            if reversibility == "irreversible":
                approval_id = f"apr-{uuid.uuid4().hex[:10]}"
                # Held. There is no deadline here and no scheduled release:
                # the only transition out of this state is decide().
                self._approvals[approval_id] = {
                    "id": approval_id, "effect_id": record["id"], "state": "pending",
                    "status": "pending", "reversibility_class": reversibility,
                    "queued_at": _now(), "decided_by": None,
                }
                record["status"] = "queued"
                record["approval_id"] = approval_id
            else:
                record["status"] = "executed"
            self.effects[record["id"]] = record
            self._append(f"effect.{record['status']}", record)
            return record

    def get_effect(self, effect_id):
        return self.effects[effect_id]

    def approvals(self):
        return list(self._approvals.values())

    def _require_caller_token(self, approval_id: str) -> None:
        """The mock's half of throughline's privileged-write authentication.

        Kept as its own method so the enforcement is one line inside
        ``decide`` — the real client's rule, applied here, without burying it
        in the refusal ladder it sits above.
        """
        if self.caller_token:
            return
        raise CallerTokenMissing(
            f"[mock] {NO_CALLER_TOKEN} ({self.caller_token_source}); the "
            "decision was not sent and the effect is still held. This is an "
            "authentication failure, NOT the gate refusing the effect.",
            body={"refused": True, "refusal_reason": REFUSAL_CALLER_TOKEN,
                  "approval_id": approval_id, "attempted": False, "mode": "mock"},
            method="POST", path=f"/approvals/{approval_id}/decide",
            token_source=self.caller_token_source,
        )

    def decide(self, approval_id, decision, decided_by, rationale=None,
               caller_role=DEFAULT_CALLER_ROLE, auth_mode="", issuer=""):
        self._require_caller_token(approval_id)
        if not decided_by:
            raise ValueError("decided_by (the approver's subject) is required")
        # The mock enforces the gate's caller_role allowlist too. It is the
        # module docstring's own rule — a mock easier to satisfy than the real
        # thing is how integration surprises are manufactured — and this is
        # precisely how this defect reached main: every mocked test passed
        # while the real gate had started refusing the shape breaker sent.
        role = (caller_role or "").strip().lower()
        if not role:
            raise SubstrateRefused(
                "[mock] caller_role is required on every decision; an approval "
                "that does not say who is deciding is not an approval",
                status=403,
                body={"refused": True, "refusal_reason": "caller-role-required",
                      "approval_id": approval_id, "mode": "mock"},
                method="POST", path=f"/approvals/{approval_id}/decide",
            )
        if role not in CALLER_ROLES:
            raise SubstrateRefused(
                f"[mock] caller_role {caller_role!r} is not one of {CALLER_ROLES}; "
                f"an unrecognised role is refused, never treated as human",
                status=403,
                body={"refused": True, "refusal_reason": "caller-role-unrecognised",
                      "approval_id": approval_id, "mode": "mock"},
                method="POST", path=f"/approvals/{approval_id}/decide",
            )
        with self._lock:
            approval = self._approvals[approval_id]
            if approval["state"] != "pending":
                raise RuntimeError(f"{approval_id} was already {approval['state']}")
            approval["state"] = approval["status"] = (
                "approved" if decision == "approve" else "rejected")
            approval["decided_by"] = decided_by
            # The mock records the role the real gate demands, so a decision
            # that would be refused 403 upstream is not silently accepted here.
            approval["caller_role"] = caller_role
            # The attestation the real gate records beside the decision: who
            # authenticated the approver. Recorded here too, unchanged and
            # un-invented, so a mocked run and a real one produce the same
            # shape and a missing attestation is as visible in the mock as it
            # is on the real ledger.
            approval["auth_mode"] = auth_mode or None
            approval["issuer"] = issuer or None
            approval["rationale"] = rationale
            approval["decided_at"] = _now()
            self._append("approval.decided", dict(approval))

            effect = self.effects[approval["effect_id"]]
            effect["status"] = "executed" if decision == "approve" else "rejected"
            self._append(f"effect.{effect['status']}", effect)
            return {"approval": approval, "effect": effect}

    def walk(self, effect_id):
        effect = self.effects[effect_id]
        hops = [{"kind": "effect", "id": effect["id"], "verified": True}]
        judgment_id = effect.get("judgment_id")
        if judgment_id in self.judgments:
            hops.append({"kind": "judgment", "id": judgment_id, "verified": True})
        signal_id = effect.get("signal_id") or self.judgments.get(judgment_id, {}).get("signal_id")
        if signal_id in self.signals:
            hops.append({"kind": "signal", "id": signal_id, "verified": True})
        return {
            "effect_id": effect_id, "hops": hops, "hop_count": len(hops),
            "complete": len(hops) == 3, "all_hops_verified": True, "mode": "mock",
        }

    def health(self):
        return {"component": "throughline", "mode": "mock",
                "note": "MOCK substrate — no real gate ran.",
                "approvals_pending": sum(
                    1 for a in self._approvals.values() if a["state"] == "pending")}
