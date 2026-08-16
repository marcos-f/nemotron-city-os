"""signet — identity binding, roles, and per-user preferences.

signet answers WHO as verifiably as throughline answers WHY. A decision
carries an OIDC subject; the role attached to that subject decides whether
the decision is allowed at all.

Roles
-----
``admin``     everything, including /admin and role edits (ledgered)
``operator``  the signet role: may approve, including irreversible effects
``viewer``    read-only
``agent``     every read/reload tool; REFUSED on irreversible approval

The refusal is by ROLE, not by transport: the same human, over the console
with their subject, succeeds where their agent is refused.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "helm_session"
SESSION_MAX_AGE = 60 * 60 * 12

ROLES = ("admin", "operator", "viewer", "agent")
APPROVER_ROLES = frozenset({"admin", "operator"})
ADMIN_ROLES = frozenset({"admin"})

#: Federation-level roles, attached to a subject ALONGSIDE its console role.
#: A separate axis on purpose: nothing here widens what the console role may
#: do, and nothing the console role says grants one of these.
#:
#: ``federation-steward`` is a VOTING FRANCHISE, not a superuser. warrant's
#: break-glass path needs a quorum of two votes from admins of OTHER datasets
#: across at least two DISTINCT datasets; on a small or single-dataset
#: federation no such quorum can exist, and the stewards are the named people
#: who may vote in one. Holding it grants no dataset authority of any kind —
#: it lets you vote to open somebody else's escape hatch, and nothing else.
#:
#: warrant READS this field and never writes it.
GLOBAL_ROLES = ("federation-steward",)
NO_GLOBAL_ROLE = ""

#: Deliberately EMPTY, and it stays that way. Seeding a holder would mean a
#: break-glass quorum exists on every fresh install, which is exactly the
#: silent lowering of the bar this design refuses. With nobody holding it,
#: warrant refuses with ``WARRANT-BREAKGLASS-NO-QUORUM``. That is the right
#: failure, and it must remain reachable.
DEFAULT_GLOBAL_ROLE_HOLDERS: tuple[str, ...] = ()

DEFAULT_ROLES: dict[str, dict[str, str]] = {
    "dana@nvidia-demo.example": {
        "provider": "github",
        "role": "admin",
        "display": "dana",
        "last_seen": "",
        "global_role": "",
    },
    "ruiz@nvidia-demo.example": {
        "provider": "github",
        "role": "operator",
        "display": "ruiz",
        "last_seen": "",
        "global_role": "",
    },
    "kim@nvidia-demo.example": {
        "provider": "google",
        "role": "viewer",
        "display": "kim",
        "last_seen": "",
        "global_role": "",
    },
    "agent:nemoclerk": {
        "provider": "helm",
        "role": "agent",
        "display": "NemoClerk",
        "last_seen": "",
        "global_role": "",
    },
}

DEFAULT_PREFS: dict[str, Any] = {
    "theme": "system",
    "feeds_collapsed": False,
    "rail_collapsed": False,
    "feeds_width": 260,
    "rail_width": 320,
}


@dataclass
class Identity:
    """A bound identity: the subject that lands in the approval record.

    ``issuer`` is the authority that VOUCHED for ``subject``. It is empty
    for the mock identity on purpose: a record naming an issuer makes a
    claim someone else can go and check, and the mock path has nobody to
    check with. Every authentication label the console shows is derived
    from these fields, so the badge cannot drift from the truth of the
    session it is drawn beside.

    ``global_role`` is a FEDERATION-level assignment held alongside ``role``,
    never derived from it. Today its only value is ``federation-steward``,
    which grants nothing on its own — see :data:`GLOBAL_ROLES`.
    """

    subject: str
    provider: str = "mock"
    display: str = ""
    role: str = "viewer"
    mock: bool = False
    authenticated: bool = True
    issuer: str = ""
    global_role: str = ""

    @property
    def initials(self) -> str:
        source = (self.display or self.subject).replace("agent:", "")
        letters = [part[0] for part in source.replace(".", " ").replace("@", " ").split() if part]
        return ("".join(letters[:2]) or source[:2]).upper()

    @property
    def is_agent(self) -> bool:
        return self.role == "agent" or self.subject.startswith("agent:")

    @property
    def may_approve(self) -> bool:
        return self.role in APPROVER_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES

    @property
    def is_federation_steward(self) -> bool:
        """May this subject VOTE in a break-glass quorum? Nothing more.

        Deliberately not folded into :attr:`is_admin`, :attr:`may_approve`
        or any other capability. A steward who is a viewer here is still a
        viewer here; the franchise is orthogonal to the console role, and
        the moment it starts implying authority it has become the superuser
        it was defined not to be.
        """
        return self.global_role == "federation-steward"

    @property
    def issuer_host(self) -> str:
        """``https://git.nemotron.example.com`` -> ``git.nemotron.example.com``."""
        if not self.issuer:
            return ""
        return self.issuer.split("://", 1)[-1].split("/", 1)[0]

    @property
    def auth_mode(self) -> str:
        """``oidc`` | ``mock`` | ``unverified`` | ``anonymous``.

        ``unverified`` is the DEFAULT for anything authenticated that has no
        issuer. It is deliberately the fall-through case: a path added later
        that forgets to record an authority is described as unverified
        rather than inheriting the verified rendering by omission.
        """
        if not self.authenticated:
            return "anonymous"
        if self.mock:
            return "mock"
        return "oidc" if self.issuer else "unverified"

    @property
    def auth_badge(self) -> str:
        """The badge text. It never claims verification it does not have.

        Three reviewers noted that ``MOCK LOGIN`` sat eight pixels from
        "Approve as dana@nvidia-demo.example" on all 26 screenshots. The
        answer is not to delete the badge — it was the only honest thing on
        the page — but to make it tell the truth in every direction: name
        the AUTHORITY when one vouched, keep saying MOCK when the identity
        is fabricated, and say UNVERIFIED when it is a real sign-in that
        nobody signed an assertion for. A null issuer has its own words; it
        never renders blank, and it never renders as the verified case.
        """
        if not self.authenticated:
            return "SIGNED OUT"
        if self.mock:
            return "MOCK LOGIN"
        if not self.issuer:
            return "UNVERIFIED IDENTITY"
        return f"SIGNET · {self.issuer_host}"

    @property
    def auth_detail(self) -> str:
        """The sentence behind the badge — always says what is missing."""
        if not self.authenticated:
            return "no session; nobody is signed in"
        if self.mock:
            return (
                "MOCK LOGIN — a fixed dev/test identity with no credential "
                "behind it. No issuer vouched for this subject and no "
                "approval it makes should be read as a human's signature."
            )
        if not self.issuer:
            return (
                "UNVERIFIED — this subject signed in, but helm holds no "
                "signed assertion from an authority for it. The name is "
                "real to this console and checkable by nobody else."
            )
        return (
            f"verified against {self.issuer} — the ID token's signature, "
            "issuer, audience, expiry and nonce were all checked before "
            "this session existed."
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["initials"] = self.initials
        data["may_approve"] = self.may_approve
        data["is_admin"] = self.is_admin
        data["issuer_host"] = self.issuer_host
        data["auth_mode"] = self.auth_mode
        data["auth_badge"] = self.auth_badge
        data["auth_detail"] = self.auth_detail
        data["is_federation_steward"] = self.is_federation_steward
        data["verified"] = self.auth_mode == "oidc"
        return data


@dataclass(frozen=True)
class Attestation:
    """WHO acted, and WHICH AUTHORITY vouched for them, for one request.

    "An identified human approved this" is an assertion until the record
    also says who identified them. The subject alone is a name helm chose;
    the subject WITH its issuer is a claim anyone can take back to
    ``git.nemotron.example.com`` and check.

    It is request-scoped context, set at the HTTP boundary where the
    session is read and consumed at the boundary where the outbound
    substrate call is made. Nothing in between can strip it or forge one:
    the only writer is the entry point that just verified the cookie.
    """

    subject: str
    issuer: str = ""
    auth_mode: str = "anonymous"
    display: str = ""

    @property
    def verified(self) -> bool:
        """Did a third party vouch for this subject?"""
        return bool(self.subject and self.issuer and self.auth_mode == "oidc")

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "auth_mode": self.auth_mode,
            "verified": self.verified,
        }

    @classmethod
    def of(cls, identity: "Identity") -> "Attestation":
        return cls(
            subject=identity.subject,
            issuer=identity.issuer,
            auth_mode=identity.auth_mode,
            display=identity.display,
        )


NO_ATTESTATION = Attestation(subject="", issuer="", auth_mode="anonymous")

#: The attestation in force for the current request, if any.
CURRENT_ATTESTATION: ContextVar[Attestation] = ContextVar(
    "helm_signet_attestation", default=NO_ATTESTATION
)


@contextmanager
def attesting(identity: "Identity") -> Iterator[Attestation]:
    """Bind ``identity`` as the attestation for the enclosing block."""
    attestation = Attestation.of(identity)
    token = CURRENT_ATTESTATION.set(attestation)
    try:
        yield attestation
    finally:
        CURRENT_ATTESTATION.reset(token)


def current_attestation() -> Attestation:
    return CURRENT_ATTESTATION.get()


ANONYMOUS = Identity(
    subject="",
    provider="",
    display="",
    role="viewer",
    mock=False,
    authenticated=False,
)


class JsonStore:
    """A small, flock-free, thread-safe JSON document on disk."""

    def __init__(self, path: Path, default: dict[str, Any]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._default = default
        self._data: dict[str, Any] | None = None

    def read(self) -> dict[str, Any]:
        with self._lock:
            if self._data is None:
                if self.path.exists():
                    try:
                        self._data = json.loads(self.path.read_text())
                    except (OSError, ValueError):
                        self._data = json.loads(json.dumps(self._default))
                else:
                    self._data = json.loads(json.dumps(self._default))
            return json.loads(json.dumps(self._data))

    def write(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._data = json.loads(json.dumps(data))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
            os.replace(tmp, self.path)


@dataclass
class RoleChange:
    subject: str
    old_role: str
    new_role: str
    changed_by: str
    at: str


@dataclass
class GlobalRoleChange:
    """A federation-level assignment. Its own type, because it is ledgered
    under its own class and read by warrant, not by the console's role gate.
    """

    subject: str
    old_global_role: str
    new_global_role: str
    changed_by: str
    at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Signet:
    """The identity service: sessions, the role table, and preferences."""

    def __init__(
        self,
        data_dir: str | Path,
        secret: str,
        *,
        auth_disabled: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.auth_disabled = auth_disabled
        self._serializer = URLSafeTimedSerializer(secret, salt="helm.signet")
        self.roles = JsonStore(self.data_dir / "roles.json", DEFAULT_ROLES)
        self.prefs = JsonStore(self.data_dir / "prefs.json", {})
        self._oauth_states: dict[str, float] = {}

    # ---------------------------------------------------------------- roles
    def role_table(self) -> list[dict[str, Any]]:
        table = self.roles.read()
        rows = []
        for subject, row in sorted(table.items()):
            entry = {"subject": subject}
            entry.update(row)
            # Rows written before this field existed read as "holds nothing",
            # never as absent. warrant asks every row the same question and
            # must get the same shape of answer from all of them.
            entry.setdefault("global_role", NO_GLOBAL_ROLE)
            rows.append(entry)
        return rows

    def role_for(self, subject: str, provider: str = "") -> str:
        table = self.roles.read()
        row = table.get(subject)
        if row:
            return str(row.get("role", "viewer"))
        # An unknown but authenticated subject is a viewer until an admin
        # says otherwise. It is never silently promoted.
        return "viewer"

    # -------------------------------------------------------- global roles
    def global_role_for(self, subject: str) -> str:
        """The federation role this subject holds, or ``""`` for none.

        An unknown subject holds nothing. There is no inference here and no
        default: the only way to hold a global role is for someone to have
        assigned it, on the record.
        """
        row = self.roles.read().get(subject)
        if not row:
            return NO_GLOBAL_ROLE
        value = str(row.get("global_role", NO_GLOBAL_ROLE))
        return value if value in GLOBAL_ROLES else NO_GLOBAL_ROLE

    def global_role_holders(self, global_role: str = "federation-steward") -> list[dict[str, Any]]:
        """The named people who hold ``global_role``. Possibly nobody.

        This list is what the UI renders. The escape hatch has faces
        attached to it or it has nobody's, and an empty list is a real,
        renderable answer — not a reason to fall back to something.
        """
        return [
            {
                "subject": row["subject"],
                "display": row.get("display") or row["subject"],
                "provider": row.get("provider", ""),
                "role": row.get("role", "viewer"),
                "global_role": row.get("global_role", NO_GLOBAL_ROLE),
                "last_seen": row.get("last_seen", ""),
            }
            for row in self.role_table()
            if row.get("global_role") == global_role
        ]

    def set_global_role(
        self, subject: str, new_global_role: str, changed_by: str
    ) -> GlobalRoleChange:
        """Assign or clear a federation role. ``""`` clears it.

        Validated against :data:`GLOBAL_ROLES` so a typo becomes a 422
        rather than a holder nobody can find and nobody can revoke.
        """
        if new_global_role not in GLOBAL_ROLES and new_global_role != NO_GLOBAL_ROLE:
            raise ValueError(
                f"unknown global role {new_global_role!r}; expected one of "
                f"{GLOBAL_ROLES} or '' to clear it"
            )
        table = self.roles.read()
        row = table.get(subject)
        if row is None:
            raise KeyError(subject)
        old = str(row.get("global_role", NO_GLOBAL_ROLE))
        row["global_role"] = new_global_role
        self.roles.write(table)
        return GlobalRoleChange(
            subject=subject,
            old_global_role=old,
            new_global_role=new_global_role,
            changed_by=changed_by,
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def ensure_subject(self, subject: str, provider: str, display: str) -> None:
        table = self.roles.read()
        row = table.get(subject)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if row is None:
            table[subject] = {
                "provider": provider,
                "role": "viewer",
                "display": display or subject,
                "last_seen": now,
                # A new subject holds no federation role. Signing in is not
                # a way to acquire one.
                "global_role": NO_GLOBAL_ROLE,
            }
        else:
            row["last_seen"] = now
            row["provider"] = provider or row.get("provider", "")
            if display:
                row["display"] = display
            # Signing in must never disturb a federation assignment, in
            # either direction: not grant one, and not silently drop one.
            row.setdefault("global_role", NO_GLOBAL_ROLE)
        self.roles.write(table)

    def set_role(self, subject: str, new_role: str, changed_by: str) -> RoleChange:
        if new_role not in ROLES:
            raise ValueError(f"unknown role {new_role!r}; expected one of {ROLES}")
        table = self.roles.read()
        row = table.get(subject)
        if row is None:
            raise KeyError(subject)
        old = str(row.get("role", "viewer"))
        row["role"] = new_role
        # The console role and the federation role are separate axes. An
        # admin editing the first must not move the second, which is the
        # whole reason they are not one field.
        row.setdefault("global_role", NO_GLOBAL_ROLE)
        self.roles.write(table)
        return RoleChange(
            subject=subject,
            old_role=old,
            new_role=new_role,
            changed_by=changed_by,
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    # ------------------------------------------------------------- sessions
    def issue(
        self,
        subject: str,
        provider: str,
        display: str = "",
        *,
        mock: bool = False,
        issuer: str = "",
    ) -> str:
        """Mint a session cookie for ``subject``.

        ``issuer`` travels in the cookie because the approval record needs
        it later: WHO approved is only half the claim, and WHICH AUTHORITY
        vouched for them is the half that makes it checkable. A mock
        session carries no issuer, which is why the badge can never mistake
        one for the other.
        """
        self.ensure_subject(subject, provider, display)
        return self._serializer.dumps(
            {
                "sub": subject,
                "prov": provider,
                "disp": display or subject,
                "mock": mock,
                "iss": issuer,
            }
        )

    def issue_for_claims(self, claims: Any) -> tuple[str, Identity]:
        """Bind a VERIFIED OIDC assertion to a signet identity and session.

        The role model does not change here, and neither does
        ``data/roles.json``: the change is entirely in WHERE the subject
        comes from. A provider's raw ``sub`` is usually an opaque number,
        so the subject is resolved through the aliases the role table is
        actually keyed by — a verified email first, then
        ``issuer-host/username``, then ``iss#sub`` as the last resort that
        is always unique. An unrecognised subject is a VIEWER, exactly as
        before; nothing is ever silently promoted by signing in.
        """
        subject = self.subject_for_claims(claims)
        display = getattr(claims, "display", "") or subject
        issuer = getattr(claims, "issuer", "")
        host = getattr(claims, "issuer_host", "") or issuer
        token = self.issue(subject, host, display, mock=False, issuer=issuer)
        identity = Identity(
            subject=subject,
            provider=host,
            display=display,
            role=self.role_for(subject),
            mock=False,
            authenticated=True,
            issuer=issuer,
            # Read from the table, never from the token. A provider can say
            # who you are; only this federation says whether you may vote in
            # somebody else's break-glass.
            global_role=self.global_role_for(subject),
        )
        return token, identity

    def subject_for_claims(self, claims: Any) -> str:
        """The signet subject for a verified assertion.

        Tried in order, first hit wins, and only aliases the role table
        already knows are considered — so an existing operator keeps their
        role the first time they sign in for real.
        """
        table = self.roles.read()
        email = getattr(claims, "email", "")
        verified_email = email if getattr(claims, "email_verified", False) else ""
        host = getattr(claims, "issuer_host", "")
        username = getattr(claims, "preferred_username", "")
        raw_subject = getattr(claims, "subject", "")

        candidates = [
            verified_email,
            f"{host}/{username}" if host and username else "",
            f"{getattr(claims, 'issuer', '')}#{raw_subject}",
        ]
        for candidate in candidates:
            if candidate and candidate in table:
                return candidate
        # Nothing known yet: prefer the most human-readable stable handle.
        for candidate in candidates:
            if candidate:
                return candidate
        return raw_subject

    def identity_from_token(self, token: str | None) -> Identity:
        if not token:
            return self.default_identity()
        try:
            payload = self._serializer.loads(token, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return self.default_identity()
        subject = str(payload.get("sub", ""))
        if not subject:
            return self.default_identity()
        return Identity(
            subject=subject,
            provider=str(payload.get("prov", "")),
            display=str(payload.get("disp", subject)),
            role=self.role_for(subject),
            mock=bool(payload.get("mock", False)),
            authenticated=True,
            issuer=str(payload.get("iss", "")),
            # Not carried in the cookie: read live from the table, so a
            # revoked franchise takes effect on the next request rather than
            # when a twelve-hour session happens to expire.
            global_role=self.global_role_for(subject),
        )

    def default_identity(self) -> Identity:
        """The identity when no valid session cookie is presented: NOBODY.

        This used to hand a cookieless caller the whole mock identity —
        dana, admin, authenticated — whenever a mock flag was set. That made
        the session cookie decorative: anyone who could reach the port was
        an admin, and an unauthenticated POST could approve an irreversible
        effect. The cookie is load-bearing now.

        There is no product route that mints a fixed identity any more. A
        test that needs one constructs an ``Identity(mock=True)`` directly.
        """
        return ANONYMOUS

    # ---------------------------------------------------------------- oauth
    def new_oauth_state(self) -> str:
        state = secrets.token_urlsafe(24)
        now = time.time()
        self._oauth_states = {
            key: ts for key, ts in self._oauth_states.items() if now - ts < 600
        }
        self._oauth_states[state] = now
        return state

    def consume_oauth_state(self, state: str) -> bool:
        return self._oauth_states.pop(state, None) is not None

    # ---------------------------------------------------------------- prefs
    def prefs_for(self, subject: str) -> dict[str, Any]:
        store = self.prefs.read()
        prefs = dict(DEFAULT_PREFS)
        prefs.update(store.get(subject, {}))
        return prefs

    def set_prefs(self, subject: str, updates: dict[str, Any]) -> dict[str, Any]:
        store = self.prefs.read()
        current = dict(DEFAULT_PREFS)
        current.update(store.get(subject, {}))
        for key, value in updates.items():
            if key in DEFAULT_PREFS and value is not None:
                current[key] = value
        store[subject] = current
        self.prefs.write(store)
        return current
