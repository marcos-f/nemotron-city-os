"""The effect gate.

An effect's fate is decided by its **reversibility class**, which comes from
the effect registry (the loaded config), not from what the caller claims:

* registered effect type  -> the registry's class wins, always;
* unregistered effect type -> ``irreversible`` (fail closed — an effect the
  registry has never heard of does not get to assert its own safety);
* no effect type at all   -> the declared reversibility, defaulting to
  ``irreversible``.

Reversible effects with ``auto_execute`` execute immediately. Everything else
is queued for a human. ``decide`` is the only way a queued effect ever runs:
no flag and no environment variable skips it, and the one API path that could
once route around it — ``POST /config/reload``, by reclassifying an
irreversible effect type as reversible — no longer can. A reload whose
candidate registry would *downgrade* any effect type to ``reversible`` is
itself proposed as an irreversible effect and held at this gate; the running
registry is untouched until a human releases it. That claim used to be
written here as an absolute and was false: a security review reclassified
``grid.load_shed`` with two unauthenticated curls. It is now narrowed to what
the code actually enforces, and the code enforces it.

One caller-controlled path deliberately survives, stated plainly rather than
hidden: a proposal with **no** ``effect_type`` that claims
``reversibility: reversible`` is auto-executed on the caller's word (docket
routes permits this way). The registry has nothing to check it against, so
the chain records where the class came from — ``class_source`` on the
``effect.proposed`` entry is ``registry``, ``caller-claim`` or
``unregistered-fail-closed``. An unregistered *typed* effect is still
irreversible, always.

Releasing a queued IRREVERSIBLE effect takes more than a well-formed body.
The decision must also carry an ATTESTATION — an ``auth_mode`` on the
accepted allowlist (``oidc`` by default, matching helm/signet's own rule for
a verified identity) naming an ``issuer``. A decision that carries none is
refused as ``unattested-decision-may-not-release-irreversible``, the refusal
is ledgered, and the effect stays held. throughline cannot VERIFY an
attestation and does not pretend to; it can decline to act without one, and
that is now what it does. Recording ``auth_mode: unattested`` next to an
executed payment preserved the audit trail and none of the restraint.

Every conditional on this path is an allowlist: the safe branch is the
default and a caller has to be recognised to leave it. ``if x ==
"unsafe_value": refuse`` is fail-open whenever ``x`` is caller-supplied and
optional, and this module no longer contains that shape.

Ordering is the other invariant: the ledger entry is written **before** the
effect executes. If the ledger cannot be written the effect does not run.
"""

from __future__ import annotations

import json
import os
from typing import Any, Awaitable, Callable, Optional

from .approvals import ApprovalQueue
from .config import ConfigStore, EffectRule
from .ledger import Ledger
from .models import Decision, Effect, EffectProposal
from .relay import RelayGateMirror

Executor = Callable[[Effect], Any | Awaitable[Any]]

#: The complete set of caller roles. Anything else — absent, empty,
#: misspelled, or invented — is refused. Allowlist, not denylist.
CALLER_ROLES = ("human", "agent")

#: Subject shapes that name a non-human principal. A caller presenting one of
#: these while claiming ``caller_role: human`` is lying about which it is.
AGENT_SUBJECT_PREFIXES = ("agent:", "agent|", "agent/", "bot:", "svc:", "service:")

#: Auth modes throughline accepts as an ATTESTATION that some authority
#: authenticated the approver. This is an ALLOWLIST and it is deliberately
#: one entry long: it matches helm/signet's own rule for a verified identity
#: (``auth_mode == "oidc"`` with an issuer). A denylist here would silently
#: promote whatever mode anybody adds next.
DEFAULT_ATTESTED_AUTH_MODES = ("oidc",)

#: Env override, ``,``-separated. An operator running the console in mock
#: login can widen the allowlist EXPLICITLY (``mock``) and the widening is
#: named in /healthz and in every refusal payload, so a reader can tell a
#: chain released by verified identities from one released by a demo switch.
ATTESTED_AUTH_MODES_ENV = "THROUGHLINE_ATTESTED_AUTH_MODES"


def attested_auth_modes(raw: Optional[str] = None) -> tuple[str, ...]:
    """Resolve the attested-auth-mode allowlist from the environment.

    It can only ever name modes. An empty entry is discarded, so the one
    thing this switch CANNOT do is re-open the proven hole: a decision that
    omits ``auth_mode`` altogether has no mode to allowlist and is refused
    whatever this is set to.
    """
    value = raw if raw is not None else os.environ.get("THROUGHLINE_ATTESTED_AUTH_MODES", "")
    modes = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    return modes or DEFAULT_ATTESTED_AUTH_MODES


#: Named refusal reasons. Each one is a ledger row an operator can grep for.
REFUSAL_ROLE_MISSING = "caller-role-required"
REFUSAL_ROLE_UNRECOGNISED = "caller-role-unrecognised"
REFUSAL_AGENT_APPROVES_IRREVERSIBLE = "agent-may-not-approve-irreversible"
REFUSAL_AGENT_SUBJECT_CLAIMS_HUMAN = "agent-subject-may-not-claim-the-human-role"
REFUSAL_UNATTESTED_IRREVERSIBLE = "unattested-decision-may-not-release-irreversible"
REFUSAL_ATTESTATION_NAMES_NO_ISSUER = "attestation-names-no-issuer"
REFUSAL_EFFECT_NAMES_NO_SIGNAL = "effect-names-no-signal"
REFUSAL_EFFECT_NAMES_UNKNOWN_SIGNAL = "effect-names-unresolvable-signal"
REFUSAL_EFFECT_TYPE_REQUIRED = "effect-type-required"
REFUSAL_EFFECT_DESCRIPTION_REQUIRED = "effect-description-required"

#: Effect type used to hold a registry reversibility downgrade at the gate.
CONFIG_DOWNGRADE_EFFECT_TYPE = "config.reversibility_downgrade"

#: The effect type the registry holds at the gate for an admin promotion.
AUTHZ_ADMIN_EFFECT_TYPE = "authz.grant.admin"

#: Authorization effect types that CONFER a role on somebody. These are the
#: ones whose payload has to be read, because the type string alone does not
#: say what is being handed over.
#:
#: ``authz.dataset.claim`` is deliberately absent: onboarding confers
#: administration on the subject registering a dataset and auto-executes by
#: design (RULE-006), because a bootstrap that needs a pre-existing approver
#: is not one. That exemption is a live residual risk, named in the README.
AUTHZ_CONFERRING_EFFECT_TYPES = ("authz.grant", "authz.delegate")

#: Roles whose conferral widens who may widen access. Allowlist by inversion:
#: the set of ESCALATING roles is closed and small, and anything in it is
#: held, so a role invented tomorrow is only ever treated as ordinary if it
#: is genuinely ordinary. ``RANK`` in helm/warrant/model.py is the source.
ESCALATING_ROLES = ("admin", "administrator", "owner", "federation-steward")

#: How many PENDING approvals may share the same (effect_type, description)
#: signature before the gate refuses another one. A review found a sibling
#: feed generating a continuous, unthrottled backlog of near-identical
#: "SYNTHETIC" load-shed proposals — 346 deep, oldest ~10h — sitting in the
#: live approval queue. throughline cannot reach into another component's
#: repository and turn its feed off, but it is the gate every proposal must
#: pass through, so the backlog is capped here, on the way IN: past the cap,
#: a near-duplicate is refused (ledgered, never silently dropped) rather
#: than queued forever. Existing ledger history is untouched — refusing a
#: NEW duplicate is not rewriting the ones already recorded, and rewriting
#: the chain is the one unforgivable act for this product.
DEFAULT_MAX_PENDING_DUPLICATES = 3

#: Env override. An operator running a legitimate high-volume feed can widen
#: this explicitly; the widening is a deliberate, visible choice (see
#: ``Gate.max_pending_duplicates`` echoed on the refusal), not a silent
#: default.
MAX_PENDING_DUPLICATES_ENV = "THROUGHLINE_MAX_PENDING_DUPLICATES"

#: Named policy for the ledgered refusal below.
POLICY_PENDING_DUPLICATE_CAP = "pending-duplicate-cap"


def resolve_max_pending_duplicates(raw: Optional[str] = None) -> int:
    """Resolve the duplicate-pending cap from the environment.

    A non-numeric or non-positive override is ignored in favour of the
    default — a caller-controlled env var must not be able to raise this to
    zero-effect (accepting 0 or a negative number would silently disable the
    cap it exists to enforce).
    """
    value = raw if raw is not None else os.environ.get(MAX_PENDING_DUPLICATES_ENV, "")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PENDING_DUPLICATES
    return parsed if parsed > 0 else DEFAULT_MAX_PENDING_DUPLICATES


class PendingBacklogCapped(RuntimeError):
    """Raised when a new proposal would grow a near-duplicate backlog past
    the cap. The proposal is refused and ledgered; nothing already queued or
    already decided is touched."""

    def __init__(self, effect_id: str, cap: int, duplicate_count: int) -> None:
        self.effect_id = effect_id
        self.cap = cap
        self.duplicate_count = duplicate_count
        super().__init__(
            f"{effect_id} refused: {duplicate_count} pending approvals already "
            f"share its effect_type and description (cap {cap})"
        )


def _role_in(payload: Any) -> Optional[str]:
    """The ``role`` a structured authz record confers, normalised."""
    if not isinstance(payload, dict):
        return None
    role = payload.get("role")
    if not isinstance(role, str):
        return None
    return role.strip().lower() or None


def looks_like_an_agent_subject(subject: Optional[str]) -> bool:
    """True when ``subject`` names a machine principal by its own convention."""
    if not subject:
        return False
    lowered = subject.strip().lower()
    return any(lowered.startswith(prefix) for prefix in AGENT_SUBJECT_PREFIXES)


class EffectNotFound(KeyError):
    pass


class UntraceableEffectRefused(RuntimeError):
    """Raised when a proposal names no resolvable cause.

    ``every consequence traceable to its cause`` is not a slogan the gate
    can honour by minting an identity for an effect that names none. A
    proposal missing ``signal_id`` (or naming one that was never ingested),
    ``effect_type`` or ``description`` is refused outright — ledgered by
    name, exactly like the anonymous-decision refusal — and never admitted
    to the queue. Nothing is invented in its place.
    """

    def __init__(self, reason: str, message: str, effect_id: Optional[str] = None) -> None:
        self.reason = reason
        self.effect_id = effect_id
        super().__init__(message)


class RoleRefused(PermissionError):
    """The caller's declared role does not permit this decision.

    Raised for a missing role, an unrecognised role, an agent approving an
    irreversible effect, an agent principal claiming to be human, and a
    decision that would release an irreversible effect without a recognised
    attestation. Every one of them is ledgered as ``approval.refused``
    before this is raised, and every one leaves the effect held.

    The name is now narrower than the class: it covers every reason the
    caller may not make this decision. It is kept because consoles and
    sibling services import it.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


async def _noop_executor(effect: Effect) -> dict[str, Any]:
    """Default executor: the demo has no real world to touch."""
    return {"effect_id": effect.id, "executed": True, "executor": "noop"}


class Gate:
    def __init__(
        self,
        ledger: Ledger,
        config_store: ConfigStore,
        queue: ApprovalQueue,
        mirror: Optional[RelayGateMirror] = None,
        executor: Optional[Executor] = None,
        attested_modes: Optional[tuple[str, ...]] = None,
        max_pending_duplicates: Optional[int] = None,
    ) -> None:
        self.ledger = ledger
        self.config_store = config_store
        self.queue = queue
        self.mirror = mirror or RelayGateMirror(ledger=ledger)
        self.executor = executor or _noop_executor
        #: Auth modes accepted as an attestation. Resolved once, at wiring
        #: time, so the policy cannot change under a running gate.
        self.attested_modes = attested_modes or attested_auth_modes()
        #: The near-duplicate pending cap. Resolved once, at wiring time, for
        #: the same reason: the policy in force cannot change under a
        #: running gate just because the environment did.
        self.max_pending_duplicates = (
            max_pending_duplicates
            if max_pending_duplicates is not None
            else resolve_max_pending_duplicates()
        )
        self._effects: dict[str, Effect] = {}
        self._restore()

    # --------------------------------------------------------------- restore

    def _restore(self) -> None:
        """Rebuild effect state from the ledger — it is the source of truth."""
        for entry in self.ledger.read():
            if not str(entry.get("type", "")).startswith("effect."):
                continue
            body = entry.get("body") or {}
            effect_id = body.get("id")
            if not effect_id:
                continue
            try:
                self._effects[effect_id] = Effect.model_validate(body)
            except Exception:
                continue

    # -------------------------------------------------------------- classify

    def rule_for(self, effect_type: Optional[str]) -> Optional[EffectRule]:
        if not effect_type:
            return None
        return self.config_store.current.by_effect_type.get(effect_type)

    def _signal_was_ingested(self, signal_id: str) -> bool:
        """True only if a ``signal.ingested`` entry names exactly this id.

        Same ledger scan ``conferred_role`` uses to resolve a signal's
        payload — the cause has to be a real, recorded event, not merely a
        string the caller typed.
        """
        for entry in self.ledger.read():
            if entry.get("type") != "signal.ingested":
                continue
            if (entry.get("body") or {}).get("id") == signal_id:
                return True
        return False

    def conferred_role(self, proposal: EffectProposal) -> Optional[str]:
        """The role this proposal would hand somebody, or ``None``.

        Two places carry it, and both are read, because a caller who omits
        one must not thereby escape the check:

        * ``payload`` on the proposal itself, when the caller inlines the
          structured record;
        * the ``signal.ingested`` entry the proposal names as its cause —
          warrant writes the record there, JSON-encoded in ``payload_ref``,
          and points the effect at it.

        Anything unreadable — no signal, malformed JSON, a payload that is
        not an object — returns ``None`` and the type-based classification
        applies. That is not a hole: for a conferring authz type the caller
        would have to hide the role from the very record warrant projects
        from, and a grant whose role nobody can read confers nothing.
        """
        role = _role_in(getattr(proposal, "payload", None))
        if role:
            return role
        if not proposal.signal_id:
            return None
        for entry in reversed(self.ledger.read()):
            if entry.get("type") != "signal.ingested":
                continue
            body = entry.get("body") or {}
            if body.get("id") != proposal.signal_id:
                continue
            raw = body.get("payload_ref")
            if not isinstance(raw, str):
                return None
            try:
                return _role_in(json.loads(raw))
            except (ValueError, TypeError):
                return None
        return None

    def classify(self, proposal: EffectProposal) -> tuple[str, bool, Optional[EffectRule]]:
        """Return ``(reversibility_class, auto_execute, rule)``."""
        klass, auto_execute, rule, _ = self.classify_with_source(proposal)
        return klass, auto_execute, rule

    def classify_with_source(
        self, proposal: EffectProposal
    ) -> tuple[str, bool, Optional[EffectRule], str]:
        """As :meth:`classify`, plus *where the class came from*.

        The source is recorded in the ledger so an auditor can tell a class
        the registry asserted from one the caller merely claimed.
        """
        # What a grant CONFERS outranks the row its type string matches. A
        # security review proved the alternative: ``authz.grant`` is a
        # reversible, auto-executing row, so a grant carrying ``role: admin``
        # never reached the gate at all — while warrant, reading the same
        # payload, rendered it in the chain as ``authz.grant.admin``, the
        # type that IS held. The registry row and the label disagreed and the
        # honest-looking one was the lie. This closes that: the payload
        # decides, the type string does not.
        conferred = self.conferred_role(proposal)
        if (
            (proposal.effect_type or "") in AUTHZ_CONFERRING_EFFECT_TYPES
            and conferred in ESCALATING_ROLES
        ):
            admin_rule = self.rule_for(AUTHZ_ADMIN_EFFECT_TYPE)
            return "irreversible", False, admin_rule, "payload-confers-admin"

        rule = self.rule_for(proposal.effect_type)
        if rule is not None:
            return rule.reversibility_class, rule.auto_execute, rule, "registry"
        if proposal.effect_type:
            # A type the registry has never heard of does not get to assert
            # its own safety. Fail closed, and never auto-execute.
            return "irreversible", False, None, "unregistered-fail-closed"
        # Untyped: only an explicit, spelled-out "reversible" claim is
        # permissive, and the chain records that it was a claim.
        if proposal.reversibility == "reversible":
            return "reversible", True, None, "caller-claim"
        return "irreversible", False, None, "untyped-fail-closed"

    # ---------------------------------------------------------------- submit

    def _refuse_untraceable(self, proposal: EffectProposal, reason: str, message: str) -> None:
        """Ledger the refusal by name, then raise. Nothing is queued or minted.

        Same shape as ``_refuse`` below: the ledger entry is written before
        the exception leaves this method, so a refusal is never silent even
        though nothing is admitted for it to attach to.
        """
        self.ledger.append("effect.refused", {
            "id": proposal.id,
            "effect_type": proposal.effect_type,
            "signal_id": proposal.signal_id,
            "judgment_id": proposal.judgment_id,
            "description": proposal.description,
            "policy": reason,
            "reason": message,
        })
        raise UntraceableEffectRefused(reason, message, effect_id=proposal.id)

    async def submit(self, proposal: EffectProposal) -> Effect:
        """Type, ledger, then gate. Never the other way round.

        Two checks stand before either: a caller-supplied ``id`` is now the
        ONLY identity an effect gets (the uuid-minting fallback is gone —
        this is unconditional, whether or not the effect will be queued),
        and any effect that would be QUEUED for a human must additionally
        name an ingested signal, an effect_type and a description. The
        pending-approval queue is exactly where the P0 review found 206 of
        452 entries with all three null, so the requirement is scoped
        there. An auto-executing reversible effect (docket's untyped route
        claim, or a registry-reversible type such as ``notify.operator``)
        never sits in that queue, and its existing, deliberately-documented
        shape (see the module docstring) is unchanged by this.
        """
        klass, auto_execute, rule, class_source = self.classify_with_source(proposal)

        if not proposal.id:
            self._refuse_untraceable(
                proposal, "effect-id-required",
                "no id was supplied and the gate no longer mints one; a "
                "caller-supplied id is the only identity an effect gets.",
            )

        will_be_queued = not (klass == "reversible" and auto_execute)
        if will_be_queued:
            if not proposal.signal_id:
                self._refuse_untraceable(
                    proposal, REFUSAL_EFFECT_NAMES_NO_SIGNAL,
                    "the effect names no signal_id; every consequence has to "
                    "be traceable to its cause, and this one names none.",
                )
            if not self._signal_was_ingested(proposal.signal_id):
                self._refuse_untraceable(
                    proposal, REFUSAL_EFFECT_NAMES_UNKNOWN_SIGNAL,
                    f"signal_id {proposal.signal_id!r} does not name a signal "
                    "throughline ever ingested; a cause that was never "
                    "recorded cannot be walked back to.",
                )
            if not proposal.effect_type:
                self._refuse_untraceable(
                    proposal, REFUSAL_EFFECT_TYPE_REQUIRED,
                    "effect_type is required on a queued effect.",
                )
            if not proposal.description:
                self._refuse_untraceable(
                    proposal, REFUSAL_EFFECT_DESCRIPTION_REQUIRED,
                    "description is required on a queued effect.",
                )

        effect = Effect(
            id=proposal.id,
            reversibility=klass,
            status="proposed",
            signal_id=proposal.signal_id,
            judgment_id=proposal.judgment_id,
            description=proposal.description,
            effect_type=proposal.effect_type,
            reversibility_class=klass,
        )
        self.ledger.append(
            "effect.proposed",
            {**effect.model_dump(mode="json"), "rule": rule.id if rule else None,
             "claimed_reversibility": proposal.reversibility,
             "class_source": class_source},
        )
        self._effects[effect.id] = effect

        if klass == "reversible" and auto_execute:
            return await self._execute(effect, approved=False, approval=None)

        duplicate_count = self.queue.pending_matching(effect.effect_type, effect.description)
        if duplicate_count >= self.max_pending_duplicates:
            self.ledger.append("effect.refused", {
                **effect.model_dump(mode="json"),
                "policy": POLICY_PENDING_DUPLICATE_CAP,
                "duplicate_count": duplicate_count,
                "cap": self.max_pending_duplicates,
                "reason": (
                    f"{duplicate_count} pending approvals already share this "
                    "effect_type and description; refusing to grow a "
                    "near-duplicate backlog unboundedly. A genuinely distinct "
                    "effect (different type or description) is unaffected."
                ),
            })
            effect.status = "rejected"
            self._effects[effect.id] = effect
            raise PendingBacklogCapped(effect.id, self.max_pending_duplicates, duplicate_count)

        approval = self.queue.enqueue(
            effect_id=effect.id,
            reversibility_class=klass,
            effect_type=effect.effect_type,
            description=effect.description,
            signal_id=effect.signal_id,
        )
        effect.status = "queued"
        effect.approval_id = approval.id
        self.ledger.append(
            "effect.queued",
            {**effect.model_dump(mode="json"), "approval_id": approval.id,
             "reason": "irreversible effects are held for a human"
             if klass == "irreversible" else "rule requires human release"},
        )
        # Mirror the guardrail's hold decision as corroborating evidence.
        await self.mirror.execute(
            "effect.execute",
            {"effect_id": effect.id, "reversibility_class": klass, "approved": False},
            lambda args: self._forbidden(effect),
        )
        self._effects[effect.id] = effect
        return effect

    @staticmethod
    def _forbidden(effect: Effect):
        raise AssertionError(
            f"gate bypassed: {effect.id} executed while held for approval"
        )

    # --------------------------------------------------------------- execute

    async def _execute(self, effect: Effect, approved: bool, approval) -> Effect:
        # Ledger BEFORE effect. A failed append raises and nothing executes.
        self.ledger.append(
            "effect.executing",
            {**effect.model_dump(mode="json"), "approved_by": approval.decided_by if approval else None},
        )

        async def run(_args):
            outcome = self.executor(effect)
            return await outcome if hasattr(outcome, "__await__") else outcome

        result, record = await self.mirror.execute(
            "effect.execute",
            {
                "effect_id": effect.id,
                "reversibility_class": effect.reversibility_class or effect.reversibility,
                "approved": approved,
                "effect_type": effect.effect_type,
            },
            run,
        )
        if record["rejected"]:
            effect.status = "queued"
            self.ledger.append(
                "effect.blocked",
                {**effect.model_dump(mode="json"), "rejection_reason": record["rejection_reason"]},
            )
            self._effects[effect.id] = effect
            return effect

        effect.status = "executed"
        self.ledger.append(
            "effect.executed", {**effect.model_dump(mode="json"), "result": result}
        )
        self._effects[effect.id] = effect
        return effect

    # ---------------------------------------------------------------- decide

    def _refuse(
        self, approval_id: str, pending, decision: Decision, reason: str, message: str
    ) -> None:
        """Ledger a named refusal, then raise. The approval stays pending."""
        self.ledger.append("approval.refused", {
            "approval_id": approval_id,
            "effect_id": pending.effect_id,
            "reason": message,
            "refusal_reason": reason,
            "decided_by": decision.decided_by,
            "issuer": decision.issuer,
            "auth_mode": decision.auth_mode or "unattested",
            "caller_role": decision.caller_role,
            "decision": decision.decision,
            "reversibility_class": pending.reversibility_class,
            "attested_auth_modes": list(self.attested_modes),
        })
        raise RoleRefused(reason, message)

    def _check_attestation(self, approval_id: str, pending, decision: Decision) -> None:
        """Refuse to RELEASE an irreversible effect on an unattested decision.

        The role check above reads what the caller SAYS it is. This one asks
        the only question that survives a caller who lies: did some authority
        authenticate this subject, and does the record name it?

        throughline still cannot verify the attestation — it has no session,
        no cookie and no key. What it can do, and now does, is refuse to act
        on a decision that carries none. That is the difference between
        recording ``auth_mode: unattested`` beside an executed payment and
        leaving the payment held. The earlier fix bought the first; a review
        proved the second was still missing, with three unauthenticated curls.

        Scope is deliberately narrow, and every conditional is an allowlist:

        * only ``approve`` — refusing to act is never the dangerous direction,
          so an unattested caller may still REJECT and un-hold nothing;
        * only ``irreversible`` — a reversible effect held by rule releases
          exactly as it did before, and an auto-executing reversible effect
          never reaches this path at all (docket's routes are unaffected);
        * only a mode on :data:`self.attested_modes` passes. Absent, empty,
          ``unattested``, ``mock``, ``unverified``, ``anonymous`` or a mode
          invented tomorrow all land in the same place: refused.
        """
        if decision.decision != "approve":
            return
        if pending.reversibility_class != "irreversible":
            return

        mode = (decision.auth_mode or "").strip().lower()
        if mode not in self.attested_modes:
            self._refuse(
                approval_id, pending, decision, REFUSAL_UNATTESTED_IRREVERSIBLE,
                f"auth_mode {decision.auth_mode!r} is not an attestation this "
                f"substrate recognises (accepted: {self.attested_modes}); an "
                "irreversible effect is released only on a decision whose "
                "record names the authority that authenticated the approver, "
                "so the effect stays held",
            )
        # ``oidc`` names an authority by definition, so it must actually name
        # one. A mode an operator widened to on purpose (``mock``) is their
        # explicit, /healthz-visible choice and is not held to that.
        if mode in DEFAULT_ATTESTED_AUTH_MODES and not (decision.issuer or "").strip():
            self._refuse(
                approval_id, pending, decision, REFUSAL_ATTESTATION_NAMES_NO_ISSUER,
                f"auth_mode {mode!r} claims an authority authenticated the "
                "approver but names no issuer; an attestation nobody can be "
                "taken back to is not an attestation, so the effect stays held",
            )

    def _check_role(self, approval_id: str, pending, decision: Decision) -> str:
        """Resolve the caller's role against an allowlist, or refuse.

        The default is refusal. A caller reaches the permissive branch only by
        presenting a role this function recognises — omitting the field, or
        sending something it has never heard of, ends here.
        """
        role = (decision.caller_role or "").strip().lower()

        if not role:
            self._refuse(
                approval_id, pending, decision, REFUSAL_ROLE_MISSING,
                "caller_role is required on every decision; an approval that "
                "does not say who is deciding is not an approval",
            )
        if role not in CALLER_ROLES:
            self._refuse(
                approval_id, pending, decision, REFUSAL_ROLE_UNRECOGNISED,
                f"caller_role {decision.caller_role!r} is not one of "
                f"{CALLER_ROLES}; an unrecognised role is refused, never "
                "treated as human",
            )
        if role == "human" and looks_like_an_agent_subject(decision.decided_by):
            self._refuse(
                approval_id, pending, decision, REFUSAL_AGENT_SUBJECT_CLAIMS_HUMAN,
                f"subject {decision.decided_by!r} names an agent principal but "
                "the decision claims caller_role 'human'; the subject and the "
                "role must agree",
            )
        if (
            role == "agent"
            and decision.decision == "approve"
            and pending.reversibility_class == "irreversible"
        ):
            self._refuse(
                approval_id, pending, decision, REFUSAL_AGENT_APPROVES_IRREVERSIBLE,
                "an agent caller may not approve an irreversible effect; "
                "only an authenticated human subject may release it",
            )
        return role

    async def decide(self, approval_id: str, decision: Decision) -> dict[str, Any]:
        """The only path from queued to executed."""
        pending = self.queue.get(approval_id)
        # Refuse first, ledger the refusal, leave the approval pending.
        self._check_role(approval_id, pending, decision)
        # ...and then ask whether anybody actually authenticated the approver.
        # The role check believes the caller; this one does not.
        self._check_attestation(approval_id, pending, decision)

        approval = self.queue.decide(
            approval_id,
            decision.decision,
            decision.decided_by,
            decision.rationale,
            issuer=decision.issuer,
            auth_mode=decision.auth_mode,
        )
        self.ledger.append(
            "approval.decided",
            {
                "approval_id": approval.id,
                "effect_id": approval.effect_id,
                "decision": decision.decision,
                "decided_by": decision.decided_by,
                # WHO, and WHICH AUTHORITY vouched for them. The entry is
                # hash-chained, so this is where "an identified human
                # approved" stops being an assertion and becomes a record
                # a reader can take back to the issuer and check.
                "issuer": decision.issuer,
                "auth_mode": decision.auth_mode or "unattested",
                "caller_role": decision.caller_role,
                "rationale": decision.rationale,
            },
        )
        effect = self._effects.get(approval.effect_id)
        if effect is None:
            raise EffectNotFound(approval.effect_id)

        if approval.state == "rejected":
            effect.status = "rejected"
            self.ledger.append("effect.rejected", effect.model_dump(mode="json"))
            self._effects[effect.id] = effect
        else:
            effect.status = "approved"
            self.ledger.append("effect.approved", effect.model_dump(mode="json"))
            effect = await self._execute(effect, approved=True, approval=approval)

        return {
            "approval": approval.model_dump(mode="json"),
            "effect": effect.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------ read

    def get(self, effect_id: str) -> Effect:
        if effect_id not in self._effects:
            raise EffectNotFound(effect_id)
        return self._effects[effect_id]

    def all(self) -> list[Effect]:
        return list(self._effects.values())
