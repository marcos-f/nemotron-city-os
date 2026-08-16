"""Wire models. These mirror contracts/openapi.yaml exactly.

Input models are deliberately a superset of the contract (optional ids that
are generated, an optional ``effect_type`` that routes through the effect
registry). Responses are exactly the contract shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

Reversibility = Literal["reversible", "irreversible"]
EffectStatus = Literal["proposed", "queued", "approved", "rejected", "executed"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Signal(BaseModel):
    """A signal envelope. ``class`` is a Python keyword, hence the alias."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    signal_class: str = Field(alias="class")
    source: str
    ingest_time: datetime = Field(default_factory=utcnow)
    real_or_synthetic: Literal["real", "synthetic"]
    payload_ref: Optional[str] = None
    staleness: Optional[str] = None


class Judgment(BaseModel):
    """A judgment about a signal — the middle hop of the cause walk."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    signal_id: str
    verdict: Optional[str] = None
    produced_by: Optional[str] = None
    rationale: Optional[str] = None
    confidence: Optional[float] = None
    real_or_synthetic: Optional[Literal["real", "synthetic"]] = None
    produced_at: datetime = Field(default_factory=utcnow)


class EffectProposal(BaseModel):
    """Inbound effect proposal.

    ``reversibility`` is what the caller *claims*; the registry is what the
    gate *believes*. Where they disagree the registry wins (fail closed).
    """

    model_config = ConfigDict(populate_by_name=True)

    id: Optional[str] = None
    effect_type: Optional[str] = None
    reversibility: Optional[Reversibility] = None
    status: Optional[EffectStatus] = None
    signal_id: Optional[str] = None
    judgment_id: Optional[str] = None
    description: Optional[str] = None
    #: The structured record this effect carries out, when the caller inlines
    #: it rather than putting it in the signal it names. The gate reads
    #: ``role`` out of it, because for an ``authz.*`` effect what is being
    #: CONFERRED decides the reversibility class — the type string does not.
    #: Optional and free-form: it is evidence, never permission.
    payload: Optional[dict[str, Any]] = None


class Effect(BaseModel):
    """Effect record, exactly the contract shape plus the registry echo."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    reversibility: Reversibility
    status: EffectStatus
    signal_id: Optional[str] = None
    judgment_id: Optional[str] = None
    description: Optional[str] = None
    effect_type: Optional[str] = None
    reversibility_class: Optional[str] = None
    approval_id: Optional[str] = None


class Decision(BaseModel):
    """A human decision on a queued approval.

    ``decided_by`` is the OIDC subject and is REQUIRED at the schema level:
    an approval without a subject is not an approval, so it is rejected by
    validation before any handler runs.

    ``caller_role`` is REQUIRED, and its absence is a refusal rather than a
    quiet pass. It used to be optional, which made the safe path opt-in: a
    caller who simply omitted the key skipped the agent check entirely and an
    irreversible effect executed. A security review proved that against a
    scratch instance. The role is now an allowlist of exactly ``human`` and
    ``agent``; anything missing, empty or unrecognised is refused and
    ledgered by :meth:`throughline.gate.Gate.decide`.

    It is typed ``str`` rather than a ``Literal`` on purpose: a schema
    rejection returns 422 and writes nothing, whereas the whole point of this
    field is that a bad role becomes a **named refusal in the chain**. The
    allowlist therefore lives in the gate, where a ledger is in reach.

    ``issuer`` is the authority that VOUCHED for ``decided_by``, and
    ``auth_mode`` is how the caller came by that subject. Together they are
    the decision's ATTESTATION, and they are the fields that let the
    substrate tell an identified human from a stranger with curl. They stay
    OPTIONAL at the schema level on purpose: their absence is not a 422 that
    writes nothing, it is a **named, ledgered refusal** — the same reasoning
    that keeps ``caller_role`` a ``str`` rather than a ``Literal``.

    What the gate does with them (see
    :meth:`throughline.gate.Gate._check_attestation`): releasing an
    IRREVERSIBLE effect requires an ``auth_mode`` on the accepted allowlist
    — ``oidc`` by default, matching helm/signet's own rule for a verified
    identity — naming an ``issuer``. Anything else, absent included, is
    refused as ``unattested-decision-may-not-release-irreversible`` and the
    effect stays held. Rejecting, and releasing a reversible effect, are
    unchanged: neither is the dangerous direction.

    The limits are still worth stating plainly. throughline does not VERIFY
    the attestation — it holds no session, no cookie and no key, so a caller
    that fabricates ``auth_mode: oidc`` with a plausible issuer is recorded
    as attested and released. What changed is that the honour-system default
    now fails closed instead of open: silence no longer executes. The
    remaining gap is authentication of the caller itself, which lives at the
    network boundary (see ``THROUGHLINE_CALLER_TOKEN``), not here.
    """

    decision: Literal["approve", "reject"]
    decided_by: str = Field(min_length=1)
    rationale: Optional[str] = None
    caller_role: Optional[str] = None
    issuer: Optional[str] = None
    auth_mode: Optional[str] = None


class Approval(BaseModel):
    """A queue entry. It has no expiry and no timeout — it waits for a human."""

    id: str
    effect_id: str
    effect_type: Optional[str] = None
    reversibility_class: str
    description: Optional[str] = None
    signal_id: Optional[str] = None
    state: Literal["pending", "approved", "rejected"] = "pending"
    queued_at: datetime = Field(default_factory=utcnow)
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    #: The authority that vouched for ``decided_by``, as attested by the
    #: console that authenticated them. ``None`` means nobody did.
    issuer: Optional[str] = None
    auth_mode: Optional[str] = None
    rationale: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> str:
        """Alias of ``state``.

        helm's published contract calls this field ``status``; ours calls it
        ``state``. Emitting both costs nothing and saves every consumer a
        mapping — see contracts/openapi.yaml.
        """
        return self.state


class LedgerVerification(BaseModel):
    valid: bool
    chain_length: int
    broken_at: Optional[str] = None
    detail: Optional[str] = None
