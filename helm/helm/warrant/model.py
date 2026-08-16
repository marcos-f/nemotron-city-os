"""warrant — the role model, its rules, and the three-valued answer.

The identity provider AUTHENTICATES; the product AUTHORIZES. A verified OIDC
subject says who is on the other end of the session. Only this module says what
they may do with a given dataset, and swapping GitLab for Google for GitHub
changes nothing here except which issuer vouched for a subject.

Nothing in this file reads or writes state. It is the vocabulary — roles,
sources of authority, refusal rules, and the ``Authority`` value that every
surface renders. The projection builds it from the ledger; the service guards
the transitions.

Design: nemo-nvidia-demo-system .viper-context/specs/v0.1.0/architecture/WARRANT.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

# --------------------------------------------------------------------- roles

Role = Literal["reader", "steward", "admin"]

ROLES: tuple[str, ...] = ("reader", "steward", "admin")

#: Total order. A grant of admin implies steward implies reader; the projection
#: stores the highest DIRECT grant and never fabricates the rows beneath it.
RANK: dict[str, int] = {"reader": 1, "steward": 2, "admin": 3}

ROLE_BLURB = {
    "admin": "grants and revokes roles on this dataset, and may delegate",
    "steward": "configures this dataset; may not change who can do anything",
    "reader": "reads this dataset and its metadata",
}

# ---------------------------------------------------------- authority source

#: Where an authority came from. This is PROVENANCE, not a role — and it is
#: never hidden, because "never present a delegated or break-glass actor as if
#: they were the dataset's owner" is a property of the data, not of the CSS.
Source = Literal["direct", "delegated", "breakglass"]

SOURCE_DIRECT = "direct"
SOURCE_DELEGATED = "delegated"
SOURCE_BREAKGLASS = "breakglass"

SOURCE_BLURB = {
    SOURCE_DIRECT: "granted on this dataset",
    SOURCE_DELEGATED: "lent by an administrator, expires",
    SOURCE_BREAKGLASS: "opened by a quorum of other datasets' administrators, expires",
}

#: A named global role, held by people and visible in the UI. It exists so a
#: federation with a single dataset still has somewhere for a break-glass
#: quorum to come from. It is emphatically NOT a silent superuser: it grants no
#: dataset authority by itself, it can only VOTE, and it is listed by name.
GLOBAL_STEWARD_ROLE = "federation-steward"

#: signet's own rule, reused rather than re-derived: an identity is verified
#: only when a real issuer vouched for it. This is an ALLOWLIST on purpose —
#: a denylist of known-unverified modes would silently promote the next mode
#: somebody adds, and a permission granted to an unverified identity must
#: never look like one granted to a verified one.
VERIFIED_AUTH_MODE = "oidc"

# -------------------------------------------------------- identity assurance
#
# A SUBJECT IS VERIFIED BECAUSE THE SUBSTRATE VERIFIED IT, NEVER BECAUSE THE
# PAYLOAD SAID SO.
#
# ``issuer``, ``auth_mode`` and the ``verified_identity`` verdict derived from
# them used to be copied verbatim out of a caller's payload and rendered as a
# verification claim. A security reviewer used exactly that to forge a
# "verified" administrator: post an ``authz.grant`` carrying
# ``auth_mode: oidc, issuer: https://git.nemotron.example.com`` and the console showed a
# vouched-for admin, because the only evidence for the record's verification
# was the record itself. throughline now authenticates the caller on that
# write surface, which closes the unauthenticated route in — but it does not
# make the claim true, and warrant must stop treating a caller's word as
# verification whoever the caller turns out to be.
#
# So the verdict has THREE states, matching helm's own rule everywhere else:
#
#   verified    warrant OBSERVED the verification — the identity came from a
#               live signet session whose ID token signature, issuer,
#               audience, expiry and nonce were checked in this process.
#   unverified  known-not-verified: the mode names something that is not an
#               identity provider at all (mock, agent, anonymous).
#   unknown     the record CLAIMS a verified mode, and warrant is reading that
#               claim back out of the record. We cannot tell. Say so.
#
#: The three states, by name, so a template and a JSON consumer agree.
ASSURANCE_VERIFIED = "verified"
ASSURANCE_UNVERIFIED = "unverified"
ASSURANCE_UNKNOWN = "unknown"

ASSURANCE_BLURB = {
    ASSURANCE_VERIFIED: (
        "verified — an identity provider vouched for this subject and warrant "
        "checked the assertion itself"
    ),
    ASSURANCE_UNVERIFIED: (
        "unverified identity — the mode recorded here is not an identity "
        "provider, so nobody vouched for this subject"
    ),
    ASSURANCE_UNKNOWN: (
        "verification UNKNOWN — this record claims an issuer vouched for the "
        "subject, but that claim is read back out of the record itself. "
        "warrant did not witness the verification and cannot confirm it. This "
        "is not a statement that the subject is unverified."
    ),
}


def identity_assurance(auth_mode: str, *, observed: bool = False) -> str:
    """How well do we actually know this subject was verified?

    ``observed`` is the whole point, and it is False by default so that a new
    caller which forgets to say where its evidence came from gets ``unknown``
    rather than inheriting the verified rendering by omission — the same
    fall-through discipline signet uses for ``auth_mode``.
    """
    mode = (auth_mode or "").strip().lower()
    if mode == VERIFIED_AUTH_MODE:
        return ASSURANCE_VERIFIED if observed else ASSURANCE_UNKNOWN
    if not mode:
        # Nothing was recorded. That is not evidence of non-verification.
        return ASSURANCE_UNKNOWN
    return ASSURANCE_UNVERIFIED


def assurance_blurb(assurance: str) -> str:
    return ASSURANCE_BLURB.get(assurance, ASSURANCE_BLURB[ASSURANCE_UNKNOWN])


def verified_identity(auth_mode: str, *, observed: bool = False) -> bool | None:
    """THREE states. ``None`` means "we cannot tell", and never renders green.

    Previously this returned ``auth_mode == "oidc"`` on a string lifted
    straight out of a caller's payload, which is how a forged grant rendered
    as verified. It still answers ``False`` for a mode that is honestly not a
    provider, because under-claiming is safe; it answers ``True`` only when
    the caller can say it watched the verification happen.
    """
    assurance = identity_assurance(auth_mode, observed=observed)
    if assurance == ASSURANCE_VERIFIED:
        return True
    if assurance == ASSURANCE_UNVERIFIED:
        return False
    return None


def identity_fields(auth_mode: str, issuer: str, *, observed: bool = False) -> dict[str, Any]:
    """The identity half of a rendered record, with its provenance attached.

    ``issuer`` and ``auth_mode`` are still carried — removing them would hide
    the very thing a reviewer needs to see — but they are now accompanied by
    what warrant actually knows about them, so no consumer has to re-derive a
    verdict from a string whose origin it cannot see.
    """
    assurance = identity_assurance(auth_mode, observed=observed)
    return {
        "issuer": issuer,
        "auth_mode": auth_mode,
        # Where the two fields above came from. "session" means warrant read
        # them off an identity it verified; "record" means they are a claim
        # copied out of the ledger, and are reported as a claim.
        "identity_source": "session" if observed else "record",
        "issuer_claimed": issuer,
        "auth_mode_claimed": auth_mode,
        "verified_identity": verified_identity(auth_mode, observed=observed),
        "identity_assurance": assurance,
        "identity_assurance_blurb": assurance_blurb(assurance),
    }

# ---------------------------------------------------------------- effect ids

EFFECT_CLAIM = "authz.dataset.claim"
EFFECT_GRANT = "authz.grant"
#: Promotion to administrator is its own type because it is held at the
#: gate: widening who may widen access is not an ordinary grant.
EFFECT_GRANT_ADMIN = "authz.grant.admin"
EFFECT_REVOKE = "authz.revoke"
EFFECT_DELEGATE = "authz.delegate"
EFFECT_DELEGATION_REVOKED = "authz.delegation.revoked"
EFFECT_DELEGATION_EXPIRED = "authz.delegation.expired"
EFFECT_REFUSED = "authz.refused"
EFFECT_BG_REQUESTED = "authz.breakglass.requested"
EFFECT_BG_VOTE = "authz.breakglass.vote"
EFFECT_BG_ENTERED = "authz.breakglass.entered"
EFFECT_BG_EXITED = "authz.breakglass.exited"

AUTHZ_EFFECTS = (
    EFFECT_CLAIM,
    EFFECT_GRANT,
    EFFECT_GRANT_ADMIN,
    EFFECT_REVOKE,
    EFFECT_DELEGATE,
    EFFECT_DELEGATION_REVOKED,
    EFFECT_DELEGATION_EXPIRED,
    EFFECT_REFUSED,
    EFFECT_BG_REQUESTED,
    EFFECT_BG_VOTE,
    EFFECT_BG_ENTERED,
    EFFECT_BG_EXITED,
)

#: Signal class prefix. Every authorization effect is preceded by a signal
#: carrying its structured record, so the effect can be walked back to its
#: cause exactly like any other effect in this substrate.
SIGNAL_PREFIX = "warrant."

# -------------------------------------------------------------------- limits

MAX_DELEGATION_DAYS = 30
BREAKGLASS_DEFAULT_MINUTES = 60
BREAKGLASS_MAX_MINUTES = 240
BREAKGLASS_QUORUM = 2
#: Votes must come from admins of at least this many DISTINCT other datasets,
#: so one person who administers six datasets is one vote, not six.
BREAKGLASS_DISTINCT_DATASETS = 2

#: throughline caps GET /ledger?limit at 1000. That cap is PER PAGE, not a
#: ceiling on what may be known: the projection reads ``offset=0`` and walks
#: forward until the substrate says there is no more, so a chain of any length
#: folds into a complete permission graph.
#:
#: Kept under its original name because it is still exactly what it says — the
#: largest window one read may return — and other callers pass it as a limit.
LEDGER_WINDOW = 1000

#: One page of that walk. Same number, named for what it now does.
LEDGER_PAGE = LEDGER_WINDOW

#: A stop on the walk. At 1000 rows a page this is a two-million-entry chain;
#: anything beyond it is a substrate that is not terminating, and the honest
#: answer to that is UNKNOWN, not a graph folded from half a chain.
LEDGER_MAX_PAGES = 2000

# ----------------------------------------------------------- refusal rules

RULE_NOT_ADMIN = "WARRANT-NOT-ADMIN"
RULE_ALREADY_CLAIMED = "WARRANT-ALREADY-CLAIMED"
RULE_LAST_ADMIN = "WARRANT-LAST-ADMIN"
RULE_SELF_REVOKE_LAST = "WARRANT-SELF-REVOKE-LAST"
RULE_EXPIRY_REQUIRED = "WARRANT-EXPIRY-REQUIRED"
RULE_DELEGATE_MAY_NOT_DELEGATE = "WARRANT-DELEGATE-MAY-NOT-DELEGATE"
RULE_DELEGATION_EXCEEDS_GRANTOR = "WARRANT-DELEGATION-EXCEEDS-GRANTOR"
RULE_BREAKGLASS_NO_QUORUM = "WARRANT-BREAKGLASS-NO-QUORUM"
RULE_BREAKGLASS_SELF_VOTE = "WARRANT-BREAKGLASS-SELF-VOTE"
RULE_BREAKGLASS_NOT_ELIGIBLE = "WARRANT-BREAKGLASS-NOT-ELIGIBLE"
RULE_AUTHORITY_UNKNOWN = "WARRANT-AUTHORITY-UNKNOWN"
RULE_CHAIN_UNVERIFIED = "WARRANT-CHAIN-UNVERIFIED"
RULE_UNKNOWN_ROLE = "WARRANT-UNKNOWN-ROLE"
RULE_NOT_CLAIMED = "WARRANT-NOT-CLAIMED"

RULES: dict[str, str] = {
    RULE_NOT_ADMIN: "only an administrator of this dataset may change who can do what to it",
    RULE_ALREADY_CLAIMED: "this dataset already has an administrator; onboarding confers ownership once",
    RULE_LAST_ADMIN: "a dataset may not be left with no administrator",
    RULE_SELF_REVOKE_LAST: "you are the last administrator of this dataset; grant someone else first",
    RULE_EXPIRY_REQUIRED: "a delegation without an expiry is not a delegation",
    RULE_DELEGATE_MAY_NOT_DELEGATE: "delegated authority may not itself be delegated",
    RULE_DELEGATION_EXCEEDS_GRANTOR: "a delegation may not exceed its grantor in role or in horizon",
    RULE_BREAKGLASS_NO_QUORUM: "there are not enough eligible voters for a break-glass quorum",
    RULE_BREAKGLASS_SELF_VOTE: "the subject asking for break-glass does not vote for it",
    RULE_BREAKGLASS_NOT_ELIGIBLE: "only an administrator of another dataset, or a named federation steward, may vote",
    # Cause-NEUTRAL on purpose. The catalogue entry says what the rule means;
    # the actual cause is filled in per refusal from ``unknown_reason``, because
    # naming a cause we have not established is a false assertion.
    RULE_AUTHORITY_UNKNOWN: "the chain could not be read, so who may do what here is unknown",
    RULE_CHAIN_UNVERIFIED: "the provenance chain does not verify, so the permission graph cannot be trusted",
    RULE_UNKNOWN_ROLE: "no such role",
    RULE_NOT_CLAIMED: "this dataset has no administrator yet; it must be claimed first",
}


# ------------------------------------------------------- why we cannot say

#: The named ways a read of the chain can fail. Defined here, next to the
#: sentences that render them, and imported by the projection — so a new
#: failure mode cannot be added without giving it words of its own.
UNKNOWN_SUBSTRATE_UNREACHABLE = "substrate-unreachable"
UNKNOWN_CHAIN_INVALID = "chain-unverified"
UNKNOWN_LEDGER_WINDOW = "ledger-window-exceeded"
UNKNOWN_PAGING_INCOMPLETE = "ledger-paging-incomplete"

#: What each one actually means, in the words a person reads.
#:
#: These exist because "unknown" was being explained with a single stock
#: sentence — "the substrate is unreachable" — including in the case where
#: throughline had answered in milliseconds and the read simply had not gone
#: far enough. Saying "unreachable" when you mean "I could not read far
#: enough" is a false assertion about a system, which is the one thing this
#: product is not allowed to do. Every distinct unknown states its own cause.
UNKNOWN_REASON_TEXT: dict[str, str] = {
    UNKNOWN_SUBSTRATE_UNREACHABLE:
        "the substrate is unreachable, so who may do what here is unknown",
    UNKNOWN_CHAIN_INVALID:
        "the provenance chain does not verify, so the permission graph cannot be trusted",
    UNKNOWN_LEDGER_WINDOW:
        "the chain could not be read all the way to its head, so who may do "
        "what here is unknown — the substrate answered, the read fell short",
    UNKNOWN_PAGING_INCOMPLETE:
        "the chain could not be paged to its head, so who may do what here is "
        "unknown — the substrate answered, the read fell short",
}

#: The same fact, addressed to whoever is looking at a screen.
UNKNOWN_MESSAGE_UI: dict[str, str] = {
    UNKNOWN_SUBSTRATE_UNREACHABLE:
        "What you may do is UNKNOWN — the substrate is not answering. "
        "This is not a statement that you have no access.",
    UNKNOWN_CHAIN_INVALID:
        "What you may do is UNKNOWN — the provenance chain does not verify, so "
        "the permission graph read from it is not evidence of anything. "
        "This is not a statement that you have no access.",
    UNKNOWN_LEDGER_WINDOW:
        "What you may do is UNKNOWN — the substrate answered, but the chain "
        "could not be read all the way to its head. "
        "This is not a statement that you have no access.",
    UNKNOWN_PAGING_INCOMPLETE:
        "What you may do is UNKNOWN — the substrate answered, but the chain "
        "could not be paged all the way to its head. "
        "This is not a statement that you have no access.",
}

#: Default for an unknown nobody has named. Deliberately says nothing about
#: reachability, because we would be guessing.
UNKNOWN_REASON_FALLBACK = "the chain could not be read, so who may do what here is unknown"
UNKNOWN_MESSAGE_FALLBACK = (
    "What you may do is UNKNOWN — the chain could not be read. "
    "This is not a statement that you have no access."
)


def unknown_reason_text(unknown_reason: str, default: str = "") -> str:
    """The truthful sentence for a named unknown. Never guesses at a cause."""
    if unknown_reason in UNKNOWN_REASON_TEXT:
        return UNKNOWN_REASON_TEXT[unknown_reason]
    return default or UNKNOWN_REASON_FALLBACK


def unknown_message_ui(unknown_reason: str, detail: str = "") -> str:
    """The same, for a screen, with the specifics appended when we have them."""
    base = UNKNOWN_MESSAGE_UI.get(unknown_reason, UNKNOWN_MESSAGE_FALLBACK)
    return f"{base} ({detail})" if detail else base


class Refused(Exception):
    """A named, failed-closed refusal. Never a bare 403.

    Carries the rule that refused it so the answer to "why not" is the same
    string on the console, in the CLI, in the MCP result and on the ledger.
    """

    #: HTTP status. UNKNOWN is a 503, not a 403 — "we cannot say" is not "no".
    def __init__(self, rule: str, detail: str = "", *, status: int = 403,
                 **extra: Any) -> None:
        self.rule = rule
        self.reason = RULES.get(rule, rule)
        self.detail = detail
        self.status = status
        self.extra = extra
        super().__init__(f"{rule}: {self.reason}{(' — ' + detail) if detail else ''}")

    def as_dict(self) -> dict[str, Any]:
        body = {
            "refused": True,
            "rule": self.rule,
            "reason": self.reason,
            "detail": self.detail,
        }
        body.update(self.extra)
        return body


class Unknown(Refused):
    """Not a denial. We could not find out.

    Kept as a distinct type so no caller can collapse "unreachable" into
    "forbidden" by accident — that collapse is the exact bug reviewers found
    six times, rendered as an empty list instead of an honest unknown.

    When the caller names ``unknown_reason``, the rendered ``reason`` and
    ``message_ui`` are that reason's own words. An unknown caused by a read
    that fell short must not be explained as an unreachable substrate: the
    substrate answered, and saying otherwise is a false assertion about a
    system that is running.
    """

    def __init__(self, rule: str = RULE_AUTHORITY_UNKNOWN, detail: str = "",
                 **extra: Any) -> None:
        super().__init__(rule, detail, status=503, **extra)
        named = str(extra.get("unknown_reason") or "")
        if named:
            self.reason = unknown_reason_text(named, RULES.get(rule, rule))
            self.extra.setdefault("message_ui", unknown_message_ui(named, detail))
            # Rebuild the exception text so a log line and the JSON agree.
            Exception.__init__(
                self, f"{rule}: {self.reason}{(' — ' + detail) if detail else ''}")


# ------------------------------------------------------------------- records


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Grant:
    """A direct grant. The only kind that satisfies the last-admin guard."""

    subject: str
    role: str
    granted_by: str
    issuer: str = ""
    auth_mode: str = ""
    owner: bool = False
    seq: int | None = None
    granted_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "role": self.role,
            "source": SOURCE_DIRECT,
            "source_blurb": SOURCE_BLURB[SOURCE_DIRECT],
            "owner": self.owner,
            "granted_by": self.granted_by,
            # Read back out of the chain, so `observed` is False by
            # construction: warrant did not witness this verification and does
            # not claim to have. See `identity_assurance`.
            **identity_fields(self.auth_mode, self.issuer),
            "granted_at": self.granted_at,
            "ledger_seq": self.seq,
            "expires_at": None,
        }


@dataclass
class Delegation:
    """Lent authority. Always expiring, never a foundation."""

    id: str
    subject: str
    role: str
    granted_by: str
    expires_at: str
    issuer: str = ""
    auth_mode: str = ""
    seq: int | None = None
    granted_at: str = ""
    ended: str = ""          # "" | "revoked" | "expired"
    ended_reason: str = ""

    def active(self, now: datetime | None = None) -> bool:
        """Expiry is EVALUATED, not scheduled.

        A delegation stops working the moment it lapses even if no process
        ever ran. A cron that fails must not silently extend anyone's
        authority.
        """
        if self.ended:
            return False
        deadline = parse_ts(self.expires_at)
        return deadline is not None and (now or utcnow()) < deadline

    def seconds_remaining(self, now: datetime | None = None) -> int | None:
        deadline = parse_ts(self.expires_at)
        if deadline is None:
            return None
        return max(0, int((deadline - (now or utcnow())).total_seconds()))

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "role": self.role,
            "source": SOURCE_DELEGATED,
            "source_blurb": SOURCE_BLURB[SOURCE_DELEGATED],
            "owner": False,
            "via": self.granted_by,
            "granted_by": self.granted_by,
            # Same rule as a direct grant: this came off the chain, so the
            # issuer beside it is a claim and is reported as one.
            **identity_fields(self.auth_mode, self.issuer),
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "active": self.active(now),
            "ended": self.ended,
            "ended_reason": self.ended_reason,
            "seconds_remaining": self.seconds_remaining(now),
            "ledger_seq": self.seq,
        }


@dataclass
class BreakGlass:
    """A time-boxed window opened by administrators of OTHER datasets."""

    id: str
    dataset_id: str
    requester: str
    reason: str
    window_minutes: int = BREAKGLASS_DEFAULT_MINUTES
    votes: list[dict[str, Any]] = field(default_factory=list)
    entered_at: str = ""
    expires_at: str = ""
    approval_id: str = ""
    exited_at: str = ""
    exit_reason: str = ""
    seq: int | None = None
    withdrawn: bool = False

    @property
    def state(self) -> str:
        if self.withdrawn:
            return "withdrawn"
        if self.exited_at:
            return "closed"
        if not self.entered_at:
            return "collecting"
        return "active" if self.unexpired() else "overdue"

    def unexpired(self, now: datetime | None = None) -> bool:
        deadline = parse_ts(self.expires_at)
        return deadline is not None and (now or utcnow()) < deadline

    def open(self, now: datetime | None = None) -> bool:
        """Confers authority only while entered, unexited and unexpired."""
        return bool(self.entered_at) and not self.exited_at and self.unexpired(now)

    def seconds_remaining(self, now: datetime | None = None) -> int | None:
        deadline = parse_ts(self.expires_at)
        if deadline is None:
            return None
        return max(0, int((deadline - (now or utcnow())).total_seconds()))

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset_id": self.dataset_id,
            "requester": self.requester,
            "reason": self.reason,
            "state": self.state,
            "open": self.open(now),
            # An entered window with no exit record is a bug, and it is
            # surfaced as one rather than quietly dropped.
            "overdue": self.state == "overdue",
            "window_minutes": self.window_minutes,
            "votes": self.votes,
            "votes_needed": BREAKGLASS_QUORUM,
            "quorum_met": bool(self.entered_at),
            "entered_at": self.entered_at,
            "expires_at": self.expires_at,
            "exited_at": self.exited_at,
            "exit_reason": self.exit_reason,
            "approval_id": self.approval_id,
            "seconds_remaining": self.seconds_remaining(now),
            "ledger_seq": self.seq,
        }


@dataclass
class Refusal:
    """A refusal that reached the chain. Shown, not swallowed."""

    rule: str
    actor: str
    detail: str = ""
    at: str = ""
    seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "reason": RULES.get(self.rule, self.rule),
            "actor": self.actor,
            "detail": self.detail,
            "at": self.at,
            "ledger_seq": self.seq,
        }


@dataclass
class Authority:
    """Everything known about who may do what to one dataset.

    ``known`` is the whole point. ``known=False`` means we could not find out
    — the substrate was unreachable, or the chain did not verify, or the read
    window was too small to be sure. It does NOT mean nobody has access, and
    the shapes are deliberately different so the two cannot be confused:
    unknown carries ``grants=None``, a known-empty carries ``grants=[]``.
    """

    dataset_id: str
    known: bool = True
    reason: str = ""
    detail: str = ""
    claimed: bool = False
    grants: dict[str, Grant] = field(default_factory=dict)
    delegations: dict[str, Delegation] = field(default_factory=dict)
    breakglass: dict[str, BreakGlass] = field(default_factory=dict)
    refusals: list[Refusal] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    as_of: str = ""
    chain_length: int = 0
    chain_valid: bool | None = None

    # ------------------------------------------------------------- queries
    @property
    def owner(self) -> Grant | None:
        for grant in self.grants.values():
            if grant.owner:
                return grant
        return None

    def direct_admins(self) -> list[Grant]:
        return [g for g in self.grants.values() if g.role == "admin"]

    def direct_admins_after(self, subject: str, new_role: str | None) -> list[str]:
        """Who would DIRECTLY administer this dataset if that row became that role.

        ``new_role=None`` means the row goes away entirely. The answer is
        computed from the RESULTING state, which is what makes this an
        invariant rather than a special case: a verb does not have to know
        whether it is a downgrade, a revocation, or whatever the third verb
        turns out to be. It only has to say which subject's direct grant it is
        about to set to which role, and the guard reads off the consequence.

        Delegates and break-glass holders never appear here, deliberately.
        Both expire, and a dataset whose only authority is on a timer with
        nobody able to renew it has merely been orphaned on a delay.
        """
        remaining = [g.subject for g in self.direct_admins() if g.subject != subject]
        if new_role == "admin":
            remaining.append(subject)
        return sorted(remaining)

    def active_delegations(self, now: datetime | None = None) -> list[Delegation]:
        out = []
        for delegation in self.delegations.values():
            if not delegation.active(now):
                continue
            # Derivative: a delegation dies with its grantor's admin grant.
            grantor = self.grants.get(delegation.granted_by)
            if grantor is None or grantor.role != "admin":
                continue
            out.append(delegation)
        return out

    def open_breakglass(self, now: datetime | None = None) -> list[BreakGlass]:
        return [b for b in self.breakglass.values() if b.open(now)]

    def capability(self, subject: str, now: datetime | None = None) -> dict[str, Any] | None:
        """The highest authority ``subject`` holds here, with its chain.

        Returns ``None`` for "no authority" — which is only meaningful when
        ``known`` is true. Callers must check ``known`` first.
        """
        candidates: list[dict[str, Any]] = []
        grant = self.grants.get(subject)
        if grant is not None:
            entry = grant.to_dict()
            entry["chain"] = self._chain_for_grant(grant)
            candidates.append(entry)
        for delegation in self.active_delegations(now):
            if delegation.subject != subject:
                continue
            entry = delegation.to_dict(now)
            entry["chain"] = self._chain_for_delegation(delegation)
            candidates.append(entry)
        for window in self.open_breakglass(now):
            if window.requester != subject:
                continue
            candidates.append({
                "subject": subject,
                "role": "admin",
                "source": SOURCE_BREAKGLASS,
                "source_blurb": SOURCE_BLURB[SOURCE_BREAKGLASS],
                "owner": False,
                "via": f"break-glass {window.id}",
                "granted_by": ", ".join(v["voter"] for v in window.votes),
                "expires_at": window.expires_at,
                "seconds_remaining": window.seconds_remaining(now),
                "ledger_seq": window.seq,
                "chain": self._chain_for_breakglass(window),
            })
        if not candidates:
            return None
        # Highest role wins; on a tie prefer the durable source, so a person
        # who is genuinely an admin is never labelled a delegate.
        order = {SOURCE_DIRECT: 0, SOURCE_DELEGATED: 1, SOURCE_BREAKGLASS: 2}
        candidates.sort(key=lambda c: (-RANK.get(c["role"], 0), order.get(c["source"], 9)))
        return candidates[0]

    def may(self, subject: str, role: str, now: datetime | None = None) -> bool:
        """True iff ``subject`` holds at least ``role`` here, however sourced."""
        held = self.capability(subject, now)
        return held is not None and RANK.get(held["role"], 0) >= RANK.get(role, 99)

    def is_direct_admin(self, subject: str) -> bool:
        grant = self.grants.get(subject)
        return grant is not None and grant.role == "admin"

    # --------------------------------------------------------------- chains
    def _chain_for_grant(self, grant: Grant) -> list[dict[str, Any]]:
        chain = []
        owner = self.owner
        if owner is not None and owner.subject != grant.subject:
            chain.append({"seq": owner.seq, "event": EFFECT_CLAIM,
                          "subject": owner.subject, "by": "self (onboarding)"})
        chain.append({
            "seq": grant.seq,
            "event": (
                EFFECT_CLAIM if grant.owner
                else EFFECT_GRANT_ADMIN if grant.role == "admin"
                else EFFECT_GRANT
            ),
            "subject": grant.subject,
            "by": "self (onboarding)" if grant.owner else grant.granted_by,
        })
        return chain

    def _chain_for_delegation(self, delegation: Delegation) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        grantor = self.grants.get(delegation.granted_by)
        if grantor is not None:
            chain.extend(self._chain_for_grant(grantor))
        chain.append({"seq": delegation.seq, "event": EFFECT_DELEGATE,
                      "subject": delegation.subject, "by": delegation.granted_by,
                      "expires_at": delegation.expires_at})
        return chain

    def _chain_for_breakglass(self, window: BreakGlass) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = [
            {"seq": window.seq, "event": EFFECT_BG_REQUESTED,
             "subject": window.requester, "by": "self", "reason": window.reason},
        ]
        for vote in window.votes:
            chain.append({"seq": vote.get("seq"), "event": EFFECT_BG_VOTE,
                          "subject": vote.get("voter"),
                          "by": f"administrator of {vote.get('dataset')}"})
        chain.append({"seq": window.seq, "event": EFFECT_BG_ENTERED,
                      "subject": window.requester,
                      "by": "quorum", "expires_at": window.expires_at})
        return chain

    # ----------------------------------------------------------- rendering
    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        base: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "known": self.known,
            "as_of": self.as_of,
            "substrate": "throughline",
            "chain_length": self.chain_length,
            "chain_valid": self.chain_valid,
        }
        if not self.known:
            # UNKNOWN. Not an empty list, not a denial, not a zero. The keys
            # are present and null so a consumer that forgets to check
            # ``known`` gets a None it must handle, rather than a [] it will
            # happily render as "nobody has access".
            base.update({
                "reason": self.reason,
                # The named reason spelled out. ``reason`` stays the machine
                # code it has always been; this is the sentence, and it names
                # THIS cause rather than a stock one.
                "reason_text": unknown_reason_text(self.reason),
                "unknown_reason": self.reason,
                "detail": self.detail,
                "claimed": None,
                "people": None,
                "grants": None,
                "delegations": None,
                "breakglass": None,
                "refusals": None,
                "message_ui": (
                    "Who may do what to this dataset is UNKNOWN — "
                    f"{self.detail or self.reason}. This is not a statement that "
                    "nobody has access."
                ),
            })
            return base
        people = self.people(now)
        base.update({
            "claimed": self.claimed,
            "owner": self.owner.to_dict() if self.owner else None,
            "people": people,
            "grants": [g.to_dict() for g in self._sorted_grants()],
            "delegations": [d.to_dict(now) for d in self._sorted_delegations()],
            "breakglass": [b.to_dict(now) for b in self._sorted_breakglass()],
            "breakglass_active": [b.to_dict(now) for b in self.open_breakglass(now)],
            "refusals": [r.to_dict() for r in self.refusals[-25:]],
            "admin_count": len(self.direct_admins()),
        })
        return base

    def _sorted_grants(self) -> list[Grant]:
        return sorted(self.grants.values(), key=lambda g: (not g.owner, -RANK.get(g.role, 0), g.subject))

    def _sorted_delegations(self) -> list[Delegation]:
        return sorted(self.delegations.values(), key=lambda d: (bool(d.ended), d.expires_at))

    def _sorted_breakglass(self) -> list[BreakGlass]:
        return sorted(self.breakglass.values(), key=lambda b: (b.state != "active", b.id))

    def people(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Every subject with any authority here, each with its provenance.

        One row per subject per SOURCE, deliberately: a delegate who is also a
        reader appears twice, because collapsing them would hide which of the
        two is expiring.
        """
        rows: list[dict[str, Any]] = []
        for grant in self._sorted_grants():
            row = grant.to_dict()
            row["chain"] = self._chain_for_grant(grant)
            rows.append(row)
        for delegation in self._sorted_delegations():
            if not delegation.active(now):
                continue
            grantor = self.grants.get(delegation.granted_by)
            if grantor is None or grantor.role != "admin":
                continue
            row = delegation.to_dict(now)
            row["chain"] = self._chain_for_delegation(delegation)
            rows.append(row)
        for window in self.open_breakglass(now):
            rows.append({
                "subject": window.requester,
                "role": "admin",
                "source": SOURCE_BREAKGLASS,
                "source_blurb": SOURCE_BLURB[SOURCE_BREAKGLASS],
                "owner": False,
                "via": f"break-glass {window.id}",
                "granted_by": ", ".join(v["voter"] for v in window.votes),
                "expires_at": window.expires_at,
                "seconds_remaining": window.seconds_remaining(now),
                "ledger_seq": window.seq,
                "chain": self._chain_for_breakglass(window),
            })
        return rows


def unknown_authority(dataset_id: str, reason: str, detail: str = "",
                      chain_valid: bool | None = None) -> Authority:
    return Authority(dataset_id=dataset_id, known=False, reason=reason,
                     detail=detail, chain_valid=chain_valid)


def horizon(days: int = MAX_DELEGATION_DAYS, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(days=days)
