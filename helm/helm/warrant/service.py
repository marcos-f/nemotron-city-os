"""warrant's write path: guard, ledger, gate — in that order, always.

Every authorization change is an EFFECT. It is proposed to throughline, typed
by the effect registry, classified by its reversibility, and written to the
hash chain *before* it takes hold. There is no other way to change who may do
what, because there is nowhere else the state lives.

The guards fail closed and every refusal is named, so "why not" is the same
string on the console, in the CLI, in an MCP result and on the ledger. A
refusal that is not written down is indistinguishable from one that never
happened, so refusals are effects too.

Design: nemo-nvidia-demo-system .viper-context/specs/v0.1.0/architecture/WARRANT.md
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from helm.warrant import projection
from helm.warrant.model import (
    BREAKGLASS_DEFAULT_MINUTES,
    BREAKGLASS_DISTINCT_DATASETS,
    BREAKGLASS_MAX_MINUTES,
    BREAKGLASS_QUORUM,
    EFFECT_BG_ENTERED,
    EFFECT_BG_EXITED,
    EFFECT_BG_REQUESTED,
    EFFECT_BG_VOTE,
    EFFECT_CLAIM,
    EFFECT_DELEGATE,
    EFFECT_DELEGATION_EXPIRED,
    EFFECT_DELEGATION_REVOKED,
    EFFECT_GRANT,
    EFFECT_GRANT_ADMIN,
    EFFECT_REFUSED,
    EFFECT_REVOKE,
    GLOBAL_STEWARD_ROLE,
    MAX_DELEGATION_DAYS,
    RANK,
    ROLES,
    RULE_ALREADY_CLAIMED,
    RULE_AUTHORITY_UNKNOWN,
    RULE_BREAKGLASS_NOT_ELIGIBLE,
    RULE_BREAKGLASS_NO_QUORUM,
    RULE_BREAKGLASS_SELF_VOTE,
    RULE_CHAIN_UNVERIFIED,
    RULE_DELEGATE_MAY_NOT_DELEGATE,
    RULE_DELEGATION_EXCEEDS_GRANTOR,
    RULE_EXPIRY_REQUIRED,
    RULE_LAST_ADMIN,
    RULE_NOT_ADMIN,
    RULE_NOT_CLAIMED,
    RULE_SELF_REVOKE_LAST,
    RULE_UNKNOWN_ROLE,
    Refused,
    SIGNAL_PREFIX,
    SOURCE_DELEGATED,
    VERIFIED_AUTH_MODE,
    Unknown,
    identity_fields,
    iso,
    parse_ts,
    utcnow,
    verified_identity,
)

log = logging.getLogger("helm.warrant")


def _id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"


class Actor:
    """Who is asking, and how well we know that.

    ``auth_mode`` is carried verbatim — including ``mock``. A permission
    granted to an unverified identity must never look like one granted to a
    verified one, so the truth travels with the grant instead of being
    laundered on the way in.

    ``observed`` says whether warrant WATCHED the verification happen, and it
    is the only thing that can produce a ``verified`` verdict. It is False by
    default and is set in exactly one place — :meth:`Warrant.actor_from_identity`,
    from a signet session this process checked. An Actor assembled from a
    payload, a fixture or a ledger row therefore renders "we cannot tell"
    rather than borrowing the verified rendering from a string it was handed.
    """

    def __init__(self, subject: str, issuer: str = "", auth_mode: str = "",
                 display: str = "", global_role: str = "",
                 observed: bool = False) -> None:
        self.subject = subject
        self.issuer = issuer
        self.auth_mode = auth_mode or ("mock" if not issuer else VERIFIED_AUTH_MODE)
        self.display = display or subject
        self.global_role = global_role
        self.observed = observed

    @property
    def is_federation_steward(self) -> bool:
        return self.global_role == GLOBAL_STEWARD_ROLE

    def to_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "display": self.display,
                **identity_fields(self.auth_mode, self.issuer,
                                  observed=self.observed)}


class Warrant:
    """The authorization service. Reads project; writes are gated effects."""

    def __init__(self, federation: Any, signet: Any = None) -> None:
        self.federation = federation
        self.signet = signet

    # ------------------------------------------------------------------ read
    def snapshot(self) -> projection.Snapshot:
        return projection.build(self.federation.throughline)

    def authority(self, dataset_id: str, snap: projection.Snapshot | None = None):
        return (snap or self.snapshot()).authority(dataset_id)

    def global_role_for(self, subject: str) -> str:
        """The named global role, from signet's role table. Never inferred."""
        if self.signet is None:
            return ""
        try:
            for row in self.signet.role_table():
                if row.get("subject") == subject:
                    return str(row.get("global_role") or "")
        except Exception:  # pragma: no cover - signet store unavailable
            return ""
        return ""

    def actor_from_identity(self, identity: Any) -> Actor:
        """The ONE place a verification may be called observed.

        signet checked this session's ID token — signature, issuer, audience,
        expiry and nonce — in this process, so warrant is not taking anybody's
        word for it here. Every other route to an Actor leaves ``observed``
        False and renders "we cannot tell".
        """
        auth_mode = getattr(identity, "auth_mode", "") or ""
        issuer = getattr(identity, "issuer", "") or ""
        observed = bool(
            getattr(identity, "authenticated", False)
            and issuer
            and auth_mode == VERIFIED_AUTH_MODE
        )
        return Actor(
            subject=getattr(identity, "subject", "") or "",
            issuer=issuer,
            auth_mode=auth_mode,
            display=getattr(identity, "display", "") or "",
            global_role=self.global_role_for(getattr(identity, "subject", "") or ""),
            observed=observed,
        )

    # --------------------------------------------------------------- guards
    def _require_known(self, snap: projection.Snapshot) -> None:
        """Unknown is a 503 and says "cannot say", never "you may not"."""
        if snap.known:
            return
        rule = (
            RULE_CHAIN_UNVERIFIED
            if snap.reason == projection.REASON_CHAIN_INVALID
            else RULE_AUTHORITY_UNKNOWN
        )
        raise Unknown(rule, snap.detail, unknown_reason=snap.reason,
                      substrate="throughline")

    def _require_admin(self, authority: Any, actor: Actor, what: str) -> dict[str, Any]:
        held = authority.capability(actor.subject)
        if held is None or RANK.get(held["role"], 0) < RANK["admin"]:
            raise Refused(
                RULE_NOT_ADMIN,
                f"{actor.subject} is not an administrator of {authority.dataset_id}"
                + (f" (holds {held['role']} by {held['source']})" if held else " (holds nothing here)"),
                dataset_id=authority.dataset_id,
                subject=actor.subject,
                what=what,
                held=held,
            )
        return held

    def _require_admin_floor(self, authority: Any, subject: str,
                             new_role: str | None, actor: Actor, act: str) -> None:
        """THE LAST-ADMINISTRATOR INVARIANT: **this dataset must retain at least
        one DIRECT administrator.**

        Stated once, here, and checked by every verb that can change an
        existing grant's role — not just by the verb somebody happened to think
        of first. The guard used to live inside ``revoke`` alone, so ``grant``
        could reduce the same set by a different route: a sole administrator
        could grant ITSELF ``reader``, be told 201, and orphan its own dataset.
        Nobody could then appoint an administrator, because appointing one
        requires being one; break-glass became the only way back, and its
        quorum may not exist. That is the exact failure the guard exists to
        prevent, reached by a different verb.

        So the check is phrased as a question about the RESULTING state —
        "would this dataset still have a direct administrator afterwards?" —
        which a verb cannot answer wrongly by forgetting to special-case
        itself. The third verb is covered by construction: it cannot change a
        grant without saying which subject moves to which role, and saying that
        is the whole input this needs.

        DIRECT administrators only. A delegate does not satisfy it and neither
        does a break-glass holder: both expire, and leaving a dataset whose
        only authority is on a timer with nobody able to renew it is orphaning
        it on a delay. That is deliberate, not an oversight — it is why the
        vacation problem is answered with a time-boxed delegation ON TOP of a
        standing administrator rather than instead of one.

        Fails closed, names one rule, ledgers the refusal. The rule is the same
        pair the revoke path has always used, chosen the same way, so "why not"
        is one string on the console, in the CLI, in an MCP result and on the
        chain regardless of which verb was refused.
        """
        would_remain = authority.direct_admins_after(subject, new_role)
        if would_remain:
            return
        if not authority.direct_admins():
            # Already orphaned before this call — by a chain written while the
            # hole was open, which is the only way this state can now exist.
            # Refusing here would say "X is the last administrator" of a
            # dataset that has none, and would freeze the orphan in place: the
            # repair for an orphaned dataset is a grant, and a grant is what
            # we would be blocking. The invariant is about not BREAKING it.
            log.warning(
                "warrant: %s has no direct administrator; %s is not what removed it",
                authority.dataset_id, act)
            return
        rule = RULE_SELF_REVOKE_LAST if subject == actor.subject else RULE_LAST_ADMIN
        delegates = [d.subject for d in authority.active_delegations()]
        raise self.refuse(Refused(
            rule,
            f"{subject} is the last administrator of {authority.dataset_id}"
            f"; {act} would leave it with none"
            + (
                f". {', '.join(delegates)} hold delegated authority, which expires "
                "and does not count"
                if delegates else ""
            ),
            status=409, dataset_id=authority.dataset_id, subject=subject,
            direct_admins=[g.subject for g in authority.direct_admins()],
            would_leave_direct_admins=would_remain,
            delegates_do_not_count=delegates,
            attempted=act,
        ), actor, authority.dataset_id)

    # ------------------------------------------------------------- ledgering
    def _emit(self, effect_type: str, payload: dict[str, Any], description: str) -> dict[str, Any]:
        """Signal the structured record, then propose the effect that gates it.

        Two calls, in this order, so the effect can be walked back to its cause
        with ``GET /effects/{id}/walk`` like any other consequence here. If the
        signal cannot be written, no effect is proposed: no ledger line, no
        change.
        """
        signal_id = _id("wsig")
        signal = self.federation.throughline.signal({
            "id": signal_id,
            "class": f"{SIGNAL_PREFIX}{effect_type}",
            "source": "helm/warrant",
            "ingest_time": iso(utcnow()),
            "real_or_synthetic": "real",
            "payload_ref": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        })
        if not signal.ok:
            raise Unknown(
                RULE_AUTHORITY_UNKNOWN,
                f"the change was NOT made: throughline would not record it "
                f"({signal.error or 'unreachable'})",
                unknown_reason=projection.REASON_UNREACHABLE,
                effect_type=effect_type, recorded=False,
            )
        effect_id = _id("eff")
        effect = self.federation.throughline.effect({
            "id": effect_id,
            "effect_type": effect_type,
            "signal_id": signal_id,
            "description": description,
        })
        if not effect.ok:
            raise Unknown(
                RULE_AUTHORITY_UNKNOWN,
                f"the record was written but the gate would not accept the effect "
                f"({effect.error or 'unreachable'}); the change did NOT take hold",
                unknown_reason=projection.REASON_UNREACHABLE,
                effect_type=effect_type, recorded=True, applied=False,
            )
        data = effect.data if isinstance(effect.data, dict) else {}
        return {
            "effect_id": data.get("id"),
            "effect_type": effect_type,
            "status": data.get("status"),
            "reversibility_class": data.get("reversibility_class") or data.get("reversibility"),
            "approval_id": data.get("approval_id"),
            "signal_id": signal_id,
            "payload": payload,
        }

    def _ledger_refusal(self, refused: Refused, actor: Actor, dataset_id: str) -> dict[str, Any]:
        """Write the refusal down. Honestly report if we could not."""
        try:
            record = self._emit(
                EFFECT_REFUSED,
                {"dataset": dataset_id, "rule": refused.rule, "actor": actor.subject,
                 "issuer": actor.issuer, "detail": refused.detail},
                f"{refused.rule}: {refused.detail or refused.reason}",
            )
            return {"ledgered": True, "ledger": record}
        except Refused as exc:
            # We refused, and we could not write the refusal down. Say both.
            log.warning("warrant: refusal %s not ledgered: %s", refused.rule, exc)
            return {"ledgered": False, "ledger_error": str(exc)}

    def refuse(self, refused: Refused, actor: Actor, dataset_id: str) -> Refused:
        """Attach the ledger outcome to a refusal and hand it back to raise."""
        refused.extra.update(self._ledger_refusal(refused, actor, dataset_id))
        return refused

    # ----------------------------------------------------- lazy expiry sweep
    def _expire_lapsed(self, authority: Any, now: datetime | None = None) -> list[dict[str, Any]]:
        """Close out delegations the clock has already ended.

        Enforcement does not wait for this: ``Delegation.active()`` compares
        against the clock, so a lapsed delegation stops working immediately
        whether or not this ever runs. This only closes the audit trail behind
        it — which is the right way round. A sweep that fails must not be able
        to extend anyone's authority.
        """
        now = now or utcnow()
        closed = []
        for delegation in list(authority.delegations.values()):
            if delegation.ended:
                continue
            deadline = parse_ts(delegation.expires_at)
            if deadline is None or now < deadline:
                continue
            try:
                closed.append(self._emit(
                    EFFECT_DELEGATION_EXPIRED,
                    {"dataset": authority.dataset_id, "id": delegation.id,
                     "subject": delegation.subject, "reason": "window elapsed"},
                    f"delegation {delegation.id} to {delegation.subject} on "
                    f"{authority.dataset_id} expired at {delegation.expires_at}",
                ))
                delegation.ended = "expired"
                delegation.ended_reason = "window elapsed"
            except Refused:  # substrate gone; enforcement already happened
                log.info("warrant: could not record expiry of %s", delegation.id)
        return closed

    def _expire_breakglass(self, authority: Any, now: datetime | None = None) -> list[dict[str, Any]]:
        """Close windows the clock has ended. Exit is ledgered, always."""
        now = now or utcnow()
        closed = []
        for window in list(authority.breakglass.values()):
            if not window.entered_at or window.exited_at or window.unexpired(now):
                continue
            try:
                closed.append(self._emit(
                    EFFECT_BG_EXITED,
                    {"dataset": authority.dataset_id, "request_id": window.id,
                     "by": "clock", "reason": "window elapsed"},
                    f"break-glass {window.id} on {authority.dataset_id} closed: "
                    "window elapsed",
                ))
                window.exited_at = iso(now)
                window.exit_reason = "window elapsed"
            except Refused:
                log.info("warrant: could not record exit of %s", window.id)
        return closed

    def sweep(self, snap: projection.Snapshot | None = None) -> dict[str, Any]:
        """Close out everything the clock has ended, across every dataset."""
        snap = snap or self.snapshot()
        if not snap.known:
            return {"swept": False, "reason": snap.reason, "detail": snap.detail}
        delegations, windows = [], []
        for authority in snap.datasets.values():
            delegations.extend(self._expire_lapsed(authority))
            windows.extend(self._expire_breakglass(authority))
        return {"swept": True, "delegations_expired": len(delegations),
                "breakglass_closed": len(windows)}

    # ================================================================= claim
    def claim(self, dataset_id: str, actor: Actor) -> dict[str, Any]:
        """Onboarding confers ownership.

        Deliberately does NOT require a pre-existing administrator — there is
        not one yet, and a bootstrap that needs an approver is not a bootstrap.
        What protects the dataset afterwards is the last-administrator guard.
        """
        snap = self.snapshot()
        self._require_known(snap)
        authority = snap.authority(dataset_id)
        if authority.claimed:
            owner = authority.owner
            raise self.refuse(Refused(
                RULE_ALREADY_CLAIMED,
                f"{dataset_id} was claimed by {owner.subject if owner else 'someone'}"
                + (f" on {owner.granted_at}" if owner and owner.granted_at else ""),
                status=409, dataset_id=dataset_id,
                owner=owner.to_dict() if owner else None,
            ), actor, dataset_id)

        record = self._emit(
            EFFECT_CLAIM,
            {"dataset": dataset_id, "subject": actor.subject, "issuer": actor.issuer,
             "auth_mode": actor.auth_mode},
            f"{dataset_id} onboarded by {actor.subject}"
            + (f" (vouched by {actor.issuer})" if actor.issuer else "")
            + f"; {actor.subject} is its first administrator",
        )
        return {"dataset_id": dataset_id, "administrator": actor.to_dict(),
                "role": "admin", "owner": True, **record}

    # ================================================================= grant
    def grant(self, dataset_id: str, subject: str, role: str, actor: Actor) -> dict[str, Any]:
        if role not in ROLES:
            raise Refused(RULE_UNKNOWN_ROLE, f"{role!r}; known roles are {', '.join(ROLES)}",
                          status=422)
        snap = self.snapshot()
        self._require_known(snap)
        authority = snap.authority(dataset_id)
        if not authority.claimed:
            raise self.refuse(Refused(
                RULE_NOT_CLAIMED, f"{dataset_id} has no administrator to grant on its behalf",
                status=409, dataset_id=dataset_id), actor, dataset_id)
        try:
            held = self._require_admin(authority, actor, f"grant {role} to {subject}")
        except Refused as refused:
            raise self.refuse(refused, actor, dataset_id) from None

        # A grant can REDUCE the set of direct administrators: granting an
        # existing administrator a lesser role is a downgrade, and a sole
        # administrator granting itself `reader` orphans the dataset just as
        # thoroughly as revoking itself would. Same invariant, same rule, same
        # refusal — asked here rather than re-derived.
        self._require_admin_floor(
            authority, subject, role, actor,
            f"granting {subject} the {role} role")

        # Promotion to administrator is a different effect type, because it is
        # a different KIND of act: it widens the set of people who can widen
        # access. throughline classifies it irreversible and holds it, exactly
        # as it holds a reversibility downgrade. Ordinary grants auto-execute.
        promoting = role == "admin"
        record = self._emit(
            EFFECT_GRANT_ADMIN if promoting else EFFECT_GRANT,
            {"dataset": dataset_id, "subject": subject, "role": role,
             "by": actor.subject, "by_source": held["source"], "issuer": actor.issuer,
             "auth_mode": actor.auth_mode},
            f"{actor.subject} granted {subject} the {role} role on {dataset_id}"
            + (" — promotion to administrator, held at the gate" if promoting else ""),
        )
        result = {"dataset_id": dataset_id, "subject": subject, "role": role,
                  "granted_by": actor.to_dict(), "granter_source": held["source"],
                  **record}
        if promoting:
            result.update(self._release_promotion(record, actor, dataset_id, subject))
        return result

    def _release_promotion(self, record: dict[str, Any], actor: Actor,
                           dataset_id: str, subject: str) -> dict[str, Any]:
        """The promoting administrator releases their own promotion, as a human.

        This is not a rubber stamp. It is the reason an AGENT cannot promote
        anyone: the release goes through ``POST /approvals/{id}/decide`` under
        the caller's real subject with ``caller_role`` set honestly, and
        throughline refuses an ``agent:``/``bot:``/``svc:`` subject on an
        irreversible effect under ``agent-may-not-approve-irreversible``. The
        refusal is throughline's, already ledgered there, and the promotion
        stays HELD — which is the correct outcome, not an error to paper over.
        """
        approval_id = record.get("approval_id")
        if not approval_id:
            return {"in_effect": False, "held": False, "detail": (
                "throughline did not queue the promotion. Check that "
                "authz.grant.admin is registered irreversible in "
                "config/effects.yaml; until then the promotion has NOT taken hold."
            )}
        caller_role = "agent" if actor.auth_mode == "agent" else "human"
        release = self.federation.throughline.decide(
            approval_id,
            decision="approve",
            decided_by=actor.subject,
            caller_role=caller_role,
            rationale=f"{actor.subject} promotes {subject} to administrator of {dataset_id}",
        )
        if not release.ok:
            return {
                "in_effect": False,
                "held": True,
                "approval_id": approval_id,
                # WHO said no, and whether anybody did. An unauthenticated
                # release never reached the gate, so naming throughline as the
                # refuser would credit it with a decision it never made.
                "refused_by": "" if release.unauthenticated else "throughline",
                "unauthenticated": release.unauthenticated,
                "detail": (
                    f"the promotion is HELD at the gate and {subject} is NOT an "
                    "administrator: "
                    + (
                        "helm could not authenticate to the substrate, so the "
                        f"release was never judged — {release.error}"
                        if release.unauthenticated
                        else (release.error or "the release was refused")
                    )
                ),
            }
        return {"in_effect": True, "held": False, "approval_id": approval_id,
                "released_by": actor.to_dict()}

    # ================================================================ revoke
    def revoke(self, dataset_id: str, subject: str, actor: Actor,
               rationale: str = "") -> dict[str, Any]:
        snap = self.snapshot()
        self._require_known(snap)
        authority = snap.authority(dataset_id)
        try:
            held = self._require_admin(authority, actor, f"revoke {subject}")
        except Refused as refused:
            raise self.refuse(refused, actor, dataset_id) from None

        target = authority.grants.get(subject)
        if target is None:
            raise self.refuse(Refused(
                "WARRANT-NO-SUCH-GRANT",
                f"{subject} holds no direct grant on {dataset_id}",
                status=404, dataset_id=dataset_id), actor, dataset_id)

        # THE LAST-ADMIN GUARD. It counts DIRECT admins only: a delegate does
        # not satisfy it and neither does a break-glass holder. Otherwise an
        # admin could delegate, remove itself, and leave a dataset whose only
        # authority expires on a timer with nobody able to renew it. Time-boxed
        # authority is a bridge, never a foundation.
        #
        # The guard itself now lives in `_require_admin_floor`, stated as an
        # invariant over the resulting state rather than as this verb's own
        # special case — because it was this verb's own special case, and
        # `grant` reduced the very same set by a different route with nothing
        # looking. Revoking a row is "set it to no role at all", so that is
        # what is asked. Nothing about this path's answer has changed: same
        # rule, same 409, same ledgered refusal.
        self._require_admin_floor(authority, subject, None, actor,
                                  f"revoking {subject}'s {target.role} role")

        record = self._emit(
            EFFECT_REVOKE,
            {"dataset": dataset_id, "subject": subject, "role": target.role,
             "by": actor.subject, "by_source": held["source"], "issuer": actor.issuer,
             "rationale": rationale},
            f"{actor.subject} revoked {subject}'s {target.role} role on {dataset_id}"
            + (f" — {rationale}" if rationale else ""),
        )
        return {"dataset_id": dataset_id, "subject": subject,
                "revoked_role": target.role, "revoked_by": actor.to_dict(), **record}

    # ============================================================= delegate
    def delegate(self, dataset_id: str, subject: str, role: str, expires_at: str,
                 actor: Actor) -> dict[str, Any]:
        if role not in ROLES:
            raise Refused(RULE_UNKNOWN_ROLE, f"{role!r}", status=422)
        if not expires_at:
            raise Refused(
                RULE_EXPIRY_REQUIRED,
                "pass expires_at; authority that never lapses is a grant, not a "
                "delegation — use grant if that is what you mean",
                status=422, dataset_id=dataset_id)
        deadline = parse_ts(expires_at)
        now = utcnow()
        if deadline is None:
            raise Refused(RULE_EXPIRY_REQUIRED, f"{expires_at!r} is not an ISO-8601 instant",
                          status=422)
        if deadline <= now:
            raise Refused(RULE_EXPIRY_REQUIRED,
                          f"{expires_at} is in the past; a delegation that has already "
                          "expired is not a delegation", status=422)
        if deadline > now + timedelta(days=MAX_DELEGATION_DAYS):
            raise Refused(
                RULE_DELEGATION_EXCEEDS_GRANTOR,
                f"{expires_at} is beyond the {MAX_DELEGATION_DAYS}-day horizon; "
                "a longer absence is a grant decision, not a delegation",
                status=422, max_horizon=iso(now + timedelta(days=MAX_DELEGATION_DAYS)))

        snap = self.snapshot()
        self._require_known(snap)
        authority = snap.authority(dataset_id)
        try:
            held = self._require_admin(authority, actor, f"delegate to {subject}")
        except Refused as refused:
            raise self.refuse(refused, actor, dataset_id) from None

        # Depth 1. Lent authority may not be lent onward: the chain of
        # authority has to stay walkable, and a delegate of a delegate has no
        # accountable administrator at its root.
        if held["source"] != "direct":
            raise self.refuse(Refused(
                RULE_DELEGATE_MAY_NOT_DELEGATE,
                f"{actor.subject} holds admin here by {held['source']}, not by grant",
                status=403, dataset_id=dataset_id, held=held), actor, dataset_id)

        if RANK.get(role, 0) > RANK.get(held["role"], 0):
            raise self.refuse(Refused(
                RULE_DELEGATION_EXCEEDS_GRANTOR,
                f"{actor.subject} holds {held['role']} and cannot lend {role}",
                status=403, dataset_id=dataset_id), actor, dataset_id)

        delegation_id = _id("dlg")
        record = self._emit(
            EFFECT_DELEGATE,
            {"dataset": dataset_id, "id": delegation_id, "subject": subject,
             "role": role, "by": actor.subject, "expires_at": iso(deadline),
             "issuer": actor.issuer, "auth_mode": actor.auth_mode},
            f"{actor.subject} delegated {role} on {dataset_id} to {subject} "
            f"until {iso(deadline)}",
        )
        return {"dataset_id": dataset_id, "delegation_id": delegation_id,
                "subject": subject, "role": role, "source": SOURCE_DELEGATED,
                "expires_at": iso(deadline), "delegated_by": actor.to_dict(), **record}

    def revoke_delegation(self, dataset_id: str, delegation_id: str,
                          actor: Actor) -> dict[str, Any]:
        snap = self.snapshot()
        self._require_known(snap)
        authority = snap.authority(dataset_id)
        try:
            self._require_admin(authority, actor, f"end delegation {delegation_id}")
        except Refused as refused:
            raise self.refuse(refused, actor, dataset_id) from None
        delegation = authority.delegations.get(delegation_id)
        if delegation is None:
            raise Refused("WARRANT-NO-SUCH-DELEGATION",
                          f"{delegation_id} is not a delegation on {dataset_id}",
                          status=404)
        record = self._emit(
            EFFECT_DELEGATION_REVOKED,
            {"dataset": dataset_id, "id": delegation_id, "by": actor.subject,
             "reason": "ended early by an administrator"},
            f"{actor.subject} ended delegation {delegation_id} on {dataset_id}",
        )
        return {"dataset_id": dataset_id, "delegation_id": delegation_id, **record}

    # =========================================================== break-glass
    def eligible_voters(self, dataset_id: str, snap: projection.Snapshot) -> dict[str, list[str]]:
        """Administrators of OTHER datasets, plus named federation stewards.

        Returned as ``{subject: [datasets they administer]}``. Stewards appear
        with an empty list — they vote by their named global role, and that
        role is listed in the UI. It is a person, not a hidden flag.
        """
        voters: dict[str, list[str]] = {}
        for other_id, other in snap.datasets.items():
            if other_id == dataset_id:
                continue
            for grant in other.direct_admins():
                voters.setdefault(grant.subject, []).append(other_id)
        if self.signet is not None:
            try:
                for row in self.signet.role_table():
                    if str(row.get("global_role") or "") == GLOBAL_STEWARD_ROLE:
                        voters.setdefault(str(row.get("subject")), [])
            except Exception:  # pragma: no cover
                pass
        return voters

    def request_breakglass(self, dataset_id: str, reason: str, actor: Actor,
                           window_minutes: int = BREAKGLASS_DEFAULT_MINUTES) -> dict[str, Any]:
        if not reason.strip():
            raise Refused("WARRANT-BREAKGLASS-REASON-REQUIRED",
                          "say why the glass must be broken; it goes on the record",
                          status=422)
        window_minutes = max(1, min(int(window_minutes), BREAKGLASS_MAX_MINUTES))
        snap = self.snapshot()
        self._require_known(snap)
        authority = snap.authority(dataset_id)

        voters = self.eligible_voters(dataset_id, snap)
        voters.pop(actor.subject, None)  # the requester never votes for itself
        if len(voters) < BREAKGLASS_QUORUM:
            raise self.refuse(Refused(
                RULE_BREAKGLASS_NO_QUORUM,
                f"{len(voters)} eligible voter(s); {BREAKGLASS_QUORUM} are needed. "
                f"Eligible voters are administrators of other datasets and named "
                f"{GLOBAL_STEWARD_ROLE}s. Refusing rather than lowering the bar.",
                status=409, dataset_id=dataset_id,
                eligible_voters=sorted(voters), quorum=BREAKGLASS_QUORUM,
            ), actor, dataset_id)

        request_id = _id("bg")
        record = self._emit(
            EFFECT_BG_REQUESTED,
            {"dataset": dataset_id, "id": request_id, "requester": actor.subject,
             "reason": reason, "window_minutes": window_minutes,
             "issuer": actor.issuer, "auth_mode": actor.auth_mode},
            f"{actor.subject} asked to break the glass on {dataset_id} "
            f"for {window_minutes} minutes: {reason}",
        )
        return {"dataset_id": dataset_id, "request_id": request_id,
                "requester": actor.to_dict(), "reason": reason,
                "window_minutes": window_minutes,
                "votes_needed": BREAKGLASS_QUORUM,
                "distinct_datasets_needed": BREAKGLASS_DISTINCT_DATASETS,
                "eligible_voters": sorted(voters),
                "administrators_of_this_dataset": [g.subject for g in authority.direct_admins()],
                **record}

    def vote_breakglass(self, dataset_id: str, request_id: str, actor: Actor) -> dict[str, Any]:
        snap = self.snapshot()
        self._require_known(snap)
        authority = snap.authority(dataset_id)
        window = authority.breakglass.get(request_id)
        if window is None:
            raise Refused("WARRANT-NO-SUCH-BREAKGLASS",
                          f"{request_id} is not a break-glass request on {dataset_id}",
                          status=404)
        if window.entered_at:
            raise Refused("WARRANT-BREAKGLASS-ALREADY-OPEN",
                          f"{request_id} already reached quorum", status=409,
                          breakglass=window.to_dict())
        if actor.subject == window.requester:
            raise self.refuse(Refused(
                RULE_BREAKGLASS_SELF_VOTE,
                f"{actor.subject} asked for this window", status=403,
                dataset_id=dataset_id), actor, dataset_id)

        voters = self.eligible_voters(dataset_id, snap)
        if actor.subject not in voters and not actor.is_federation_steward:
            raise self.refuse(Refused(
                RULE_BREAKGLASS_NOT_ELIGIBLE,
                f"{actor.subject} administers no other dataset and is not a named "
                f"{GLOBAL_STEWARD_ROLE}", status=403, dataset_id=dataset_id,
                eligible_voters=sorted(voters)), actor, dataset_id)
        if any(v.get("voter") == actor.subject for v in window.votes):
            raise Refused("WARRANT-BREAKGLASS-DOUBLE-VOTE",
                          f"{actor.subject} has already voted on {request_id}",
                          status=409)

        voter_datasets = voters.get(actor.subject, [])
        vote = self._emit(
            EFFECT_BG_VOTE,
            {"dataset": dataset_id, "request_id": request_id, "voter": actor.subject,
             "voter_dataset": voter_datasets[0] if voter_datasets else GLOBAL_STEWARD_ROLE,
             "issuer": actor.issuer},
            f"{actor.subject} voted to break the glass on {dataset_id} ({request_id})",
        )
        window.votes.append({
            "voter": actor.subject,
            "dataset": voter_datasets[0] if voter_datasets else GLOBAL_STEWARD_ROLE,
            "issuer": actor.issuer, "at": iso(utcnow()), "seq": None,
        })

        distinct = {v["dataset"] for v in window.votes}
        result: dict[str, Any] = {
            "dataset_id": dataset_id, "request_id": request_id,
            "voter": actor.to_dict(), "votes": window.votes,
            "votes_needed": BREAKGLASS_QUORUM,
            "distinct_datasets": sorted(distinct),
            "distinct_datasets_needed": BREAKGLASS_DISTINCT_DATASETS,
            "quorum_met": False,
            **vote,
        }
        if len(window.votes) < BREAKGLASS_QUORUM or len(distinct) < BREAKGLASS_DISTINCT_DATASETS:
            result["still_needed"] = (
                f"{max(0, BREAKGLASS_QUORUM - len(window.votes))} more vote(s) from "
                f"{max(0, BREAKGLASS_DISTINCT_DATASETS - len(distinct))} more distinct dataset(s)"
            )
            return result

        result["quorum_met"] = True
        result["entry"] = self._enter_breakglass(dataset_id, window, actor)
        return result

    def _enter_breakglass(self, dataset_id: str, window: Any, final_voter: Actor) -> dict[str, Any]:
        """Open the window — through the gate, as a human, on the record.

        ``authz.breakglass.entered`` is irreversible, so throughline QUEUES it
        rather than executing it. That is the point: a break-glass cannot be
        silent because it cannot be automatic. The quorum's final voter then
        supplies the human decision under their own OIDC subject, which means
        throughline's existing agent-may-not-approve-irreversible refusal is
        what stops an agent breaking the glass. We wrote no rule for that.
        """
        expires_at = iso(utcnow() + timedelta(minutes=window.window_minutes))
        record = self._emit(
            EFFECT_BG_ENTERED,
            {"dataset": dataset_id, "request_id": window.id,
             "subject": window.requester, "expires_at": expires_at,
             "voters": [v["voter"] for v in window.votes],
             "reason": window.reason},
            f"BREAK-GLASS on {dataset_id}: {window.requester} holds administrative "
            f"access until {expires_at}, on the vote of "
            f"{', '.join(v['voter'] for v in window.votes)} — {window.reason}",
        )
        approval_id = record.get("approval_id")
        if not approval_id:
            # It did not queue. Either the registry has not been reloaded with
            # RULE-015 or something classified it reversible. Either way we do
            # not claim a window is open.
            return {**record, "open": False, "released": False, "detail": (
                "throughline did not queue the entry for a human decision; "
                "the window was NOT opened. Check that authz.breakglass.entered "
                "is registered irreversible in config/effects.yaml."
            )}

        release = self.federation.throughline.decide(
            approval_id,
            decision="approve",
            decided_by=final_voter.subject,
            caller_role="human",
            rationale=(
                f"break-glass quorum for {dataset_id}: "
                f"{', '.join(v['voter'] for v in window.votes)}"
            ),
        )
        if not release.ok:
            return {**record, "open": False, "released": False,
                    "approval_id": approval_id,
                    "unauthenticated": release.unauthenticated,
                    "detail": (
                        "the entry is HELD at the gate and the window is NOT open: "
                        + (
                            "helm could not authenticate to the substrate, so the "
                            f"release was never judged — {release.error}"
                            if release.unauthenticated
                            else (release.error or "the release was refused")
                        )
                    )}
        return {**record, "open": True, "released": True, "approval_id": approval_id,
                "expires_at": expires_at, "released_by": final_voter.to_dict()}

    def exit_breakglass(self, dataset_id: str, request_id: str, actor: Actor,
                        reason: str = "closed by hand") -> dict[str, Any]:
        """Closing is never blocked. A window that cannot be shut is worse."""
        snap = self.snapshot()
        self._require_known(snap)
        authority = snap.authority(dataset_id)
        window = authority.breakglass.get(request_id)
        if window is None:
            raise Refused("WARRANT-NO-SUCH-BREAKGLASS",
                          f"{request_id} is not a break-glass request on {dataset_id}",
                          status=404)
        if window.exited_at:
            return {"dataset_id": dataset_id, "request_id": request_id,
                    "already_closed": True, "exited_at": window.exited_at,
                    "exit_reason": window.exit_reason}
        record = self._emit(
            EFFECT_BG_EXITED,
            {"dataset": dataset_id, "request_id": request_id, "by": actor.subject,
             "reason": reason},
            f"break-glass {request_id} on {dataset_id} closed by {actor.subject}: {reason}",
        )
        return {"dataset_id": dataset_id, "request_id": request_id,
                "closed_by": actor.to_dict(), "reason": reason, **record}


__all__ = ["Warrant", "Actor"]
