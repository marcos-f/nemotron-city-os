"""The permission graph, rebuilt from throughline's hash chain.

warrant keeps **no private permission table**. There is no store to write to,
so there is no write path that can skip the ledger — which is what makes every
grant auditable by construction rather than by discipline.

Three consequences, each one a requirement rather than a nicety:

1. A tampered chain is a tampered permission graph, *visibly*: if
   ``GET /ledger/verify`` says the chain is broken, the projection is UNKNOWN.
2. An unreachable substrate yields UNKNOWN, never an empty list. There is no
   local copy that could be mistaken for the truth.
3. **Only an executed effect counts.** An authorization event that is still
   queued at the gate has not happened yet. That is how break-glass entry —
   irreversible, and therefore held for a human — becomes real only once its
   quorum releases it. The gate genuinely governs the permission graph.

Each authorization effect is preceded by a signal carrying its structured
record (``warrant.<effect_type>``, payload in ``payload_ref``), and the effect
names that signal as its cause. So an authority can be walked back through
``GET /effects/{id}/walk`` like any other consequence in this substrate.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from helm.warrant.model import (
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
    LEDGER_MAX_PAGES,
    LEDGER_PAGE,
    LEDGER_WINDOW,
    UNKNOWN_CHAIN_INVALID,
    UNKNOWN_LEDGER_WINDOW,
    UNKNOWN_PAGING_INCOMPLETE,
    UNKNOWN_SUBSTRATE_UNREACHABLE,
    Authority,
    BreakGlass,
    Delegation,
    Grant,
    Refusal,
    SIGNAL_PREFIX,
    identity_fields,
    iso,
    unknown_authority,
    utcnow,
)

log = logging.getLogger("helm.warrant")

REASON_UNREACHABLE = UNKNOWN_SUBSTRATE_UNREACHABLE
REASON_CHAIN_INVALID = UNKNOWN_CHAIN_INVALID
REASON_WINDOW = UNKNOWN_LEDGER_WINDOW
#: The substrate answered and pages, but the walk did not reach the head.
REASON_PAGING_INCOMPLETE = UNKNOWN_PAGING_INCOMPLETE


class ChainRead:
    """The outcome of reading the chain: either every entry, or why not.

    ``entries`` is the COMPLETE chain when ``ok``. Anything less is a failure
    with a named reason, never a short list handed back as if it were whole.
    """

    def __init__(self, *, ok: bool, entries: list[dict[str, Any]] | None = None,
                 chain_length: int = 0, reason: str = "", detail: str = "",
                 pages: int = 0, paged: bool = False) -> None:
        self.ok = ok
        self.entries = entries or []
        self.chain_length = chain_length
        self.reason = reason
        self.detail = detail
        self.pages = pages
        self.paged = paged


def read_chain(throughline: Any, *, page: int = LEDGER_PAGE,
               max_pages: int = LEDGER_MAX_PAGES) -> ChainRead:
    """Read the WHOLE chain, in pages, from offset zero to the head.

    throughline caps one read at 1000 entries so an unbounded read cannot be
    used against it. That cap is a per-page cap; it is not a limit on what a
    consumer may know. Before this walk existed the projection asked for a
    single window and, on a chain longer than that window, gave up — which
    made warrant unusable the moment the real chain passed 1000 entries.
    Append-only means that was a one-way door.

    The walk stops when the substrate says ``has_more`` is false. Because the
    ledger is append-only, a page at a given offset never changes underneath
    us; an entry appended mid-walk simply appears on a later page or on the
    next request, and either way no entry is skipped or seen twice.

    A substrate that does not understand ``offset`` is detected by the missing
    echo, and the old single-window semantics are used against it unchanged —
    so an older throughline still gets the honest window-exceeded answer
    rather than a silently truncated graph.
    """
    entries: list[dict[str, Any]] = []
    offset = 0
    pages = 0
    chain_length = 0

    while True:
        reply = throughline.ledger(limit=page, offset=offset)
        if not reply.ok or not isinstance(reply.data, dict):
            return ChainRead(
                ok=False, reason=REASON_UNREACHABLE,
                detail=(
                    f"could not read the ledger at offset {offset} "
                    f"({reply.error or 'no response'})"
                ),
            )
        data = reply.data
        batch = data.get("entries") or []
        chain_length = int(data.get("chain_length") or chain_length)

        if "offset" not in data:
            # An older substrate that ignored the parameter and answered with
            # a suffix. Do not pretend it paged: fall back to exactly the
            # single-window behaviour, guard included.
            return ChainRead(
                ok=True, entries=list(batch), chain_length=chain_length,
                pages=1, paged=False,
            )
        if int(data.get("offset") or 0) != offset:
            return ChainRead(
                ok=False, reason=REASON_PAGING_INCOMPLETE,
                chain_length=chain_length,
                detail=(
                    f"asked the substrate for offset {offset} and it answered "
                    f"for offset {data.get('offset')}; a chain read out of order "
                    "is not a chain"
                ),
            )

        entries.extend(batch)
        pages += 1

        if not data.get("has_more"):
            total = data.get("total_entries")
            if total is not None and len(entries) != int(total):
                return ChainRead(
                    ok=False, reason=REASON_PAGING_INCOMPLETE,
                    chain_length=chain_length,
                    detail=(
                        f"paged {len(entries)} entries out of {int(total)}; the "
                        "walk did not reach the head and a partial graph is not "
                        "a permission graph"
                    ),
                )
            return ChainRead(ok=True, entries=entries, chain_length=chain_length,
                             pages=pages, paged=True)

        next_offset = data.get("next_offset")
        if not isinstance(next_offset, int) or next_offset <= offset:
            return ChainRead(
                ok=False, reason=REASON_PAGING_INCOMPLETE,
                chain_length=chain_length,
                detail=(
                    f"the substrate said there was more after offset {offset} "
                    f"but pointed at {next_offset!r}; the walk cannot continue"
                ),
            )
        offset = next_offset

        if pages >= max_pages:
            return ChainRead(
                ok=False, reason=REASON_PAGING_INCOMPLETE,
                chain_length=chain_length,
                detail=(
                    f"stopped after {pages} pages of {page} at offset {offset}; "
                    "the substrate has not reached the head of the chain and "
                    "we will not fold half of one"
                ),
            )


class Snapshot:
    """Every dataset's authority, as of one read of the chain.

    Built once per request. A read that cannot be built is UNKNOWN for every
    dataset, including ones the caller did not ask about — we do not know
    those either.
    """

    def __init__(
        self,
        *,
        known: bool,
        reason: str = "",
        detail: str = "",
        as_of: str = "",
        chain_length: int = 0,
        chain_valid: bool | None = None,
    ) -> None:
        self.known = known
        self.reason = reason
        self.detail = detail
        self.as_of = as_of or iso(utcnow())
        self.chain_length = chain_length
        self.chain_valid = chain_valid
        self.datasets: dict[str, Authority] = {}

    # ------------------------------------------------------------- lookups
    def authority(self, dataset_id: str) -> Authority:
        if not self.known:
            return unknown_authority(dataset_id, self.reason, self.detail, self.chain_valid)
        existing = self.datasets.get(dataset_id)
        if existing is not None:
            return existing
        # A dataset nobody has claimed is a KNOWN empty, and it is honest to
        # say so. This is the one case where an empty list is the truth.
        empty = Authority(dataset_id=dataset_id, known=True, claimed=False,
                          as_of=self.as_of, chain_length=self.chain_length,
                          chain_valid=self.chain_valid)
        return empty

    def dataset_ids(self) -> list[str]:
        return sorted(self.datasets)

    def datasets_administered_by(self, subject: str, now: datetime | None = None) -> list[str]:
        """Datasets where ``subject`` holds a DIRECT admin grant."""
        return sorted(d for d, a in self.datasets.items() if a.is_direct_admin(subject))

    def datasets_with_authority(self, subject: str, now: datetime | None = None) -> list[dict[str, Any]]:
        rows = []
        for dataset_id in sorted(self.datasets):
            held = self.datasets[dataset_id].capability(subject, now)
            if held is None:
                continue
            rows.append({"dataset_id": dataset_id, **held})
        return rows

    def _ensure(self, dataset_id: str) -> Authority:
        if dataset_id not in self.datasets:
            self.datasets[dataset_id] = Authority(
                dataset_id=dataset_id, known=True, as_of=self.as_of,
                chain_length=self.chain_length, chain_valid=self.chain_valid,
            )
        return self.datasets[dataset_id]


def _payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    body = entry.get("body") or {}
    raw = body.get("payload_ref")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build(throughline: Any, *, limit: int = LEDGER_WINDOW) -> Snapshot:
    """Read the WHOLE chain, in pages, and fold it into a permission graph.

    Every failure mode here produces UNKNOWN with a named reason. None of them
    produces an empty graph, because "we could not read the chain" and "the
    chain says nobody has access" are different facts and must not render the
    same. And each named reason renders in its OWN words: a read that fell
    short is never reported as a substrate that did not answer.

    ``limit`` is the PER-PAGE size, capped by the substrate at 1000. The
    projection walks from offset zero to the head, so the graph is built from
    the complete history at any chain length.
    """
    verify = throughline.verify()
    if not verify.ok:
        return Snapshot(
            known=False,
            reason=REASON_UNREACHABLE,
            detail=(
                f"throughline at {getattr(throughline, 'base_url', 'the substrate')} "
                f"is unreachable ({verify.error or 'no response'})"
            ),
        )
    verification = verify.data if isinstance(verify.data, dict) else {}
    chain_valid = verification.get("valid")
    if chain_valid is False:
        return Snapshot(
            known=False,
            reason=REASON_CHAIN_INVALID,
            detail=(
                "the provenance chain does not verify"
                + (f" at {verification.get('broken_at')}" if verification.get("broken_at") else "")
                + " — a permission graph read from a broken chain is not evidence of anything"
            ),
            chain_valid=False,
        )

    read = read_chain(throughline, page=limit)
    if not read.ok:
        return Snapshot(
            known=False,
            reason=read.reason,
            detail=read.detail,
            chain_length=read.chain_length,
            chain_valid=chain_valid,
        )

    entries = read.entries
    chain_length = read.chain_length or len(entries)
    if chain_length > len(entries):
        # Only reachable against a substrate that does not page: we can see
        # the tail but not the head. A partial graph presented as a whole one
        # is exactly the lie this project exists not to tell. Note the reason
        # is WINDOW, not UNREACHABLE — the substrate answered, and saying it
        # did not would be a false claim about a running service.
        return Snapshot(
            known=False,
            reason=REASON_WINDOW,
            detail=(
                f"the chain is {chain_length} entries and this read returned "
                f"{len(entries)}; this substrate does not support paged reads, "
                "so the permission graph before that window cannot be "
                "reconstructed from here"
            ),
            chain_length=chain_length,
            chain_valid=chain_valid,
        )

    snapshot = Snapshot(known=True, chain_length=chain_length, chain_valid=chain_valid)
    _fold(snapshot, entries)
    return snapshot


def _fold(snapshot: Snapshot, entries: list[dict[str, Any]]) -> None:
    """Apply the chain in sequence order. Executed effects only."""
    signals: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("type") != "signal.ingested":
            continue
        body = entry.get("body") or {}
        signal_class = str(body.get("class") or "")
        if not signal_class.startswith(SIGNAL_PREFIX):
            continue
        payload = _payload(entry)
        if payload is None:
            continue
        signals[str(body.get("id"))] = {"payload": payload, "seq": entry.get("seq"),
                                        "ts": entry.get("ts")}

    for entry in entries:
        if entry.get("type") != "effect.executed":
            continue
        body = entry.get("body") or {}
        effect_type = body.get("effect_type") or ""
        if not str(effect_type).startswith("authz."):
            continue
        cause = signals.get(str(body.get("signal_id")))
        if cause is None:
            # An authorization effect whose structured cause we cannot see.
            # Recorded as an event so it is visible, but never applied: we do
            # not guess what a grant said.
            log.info("warrant: authz effect %s has no readable cause", body.get("id"))
            continue
        _apply(snapshot, effect_type, cause["payload"], entry.get("seq"), entry.get("ts") or "")


def _apply(snapshot: Snapshot, effect_type: str, payload: dict[str, Any],
           seq: int | None, ts: str) -> None:
    dataset_id = str(payload.get("dataset") or "")
    if not dataset_id:
        return
    authority = snapshot._ensure(dataset_id)
    authority.events.append({"seq": seq, "at": ts, "event": effect_type, **payload})

    if effect_type == EFFECT_CLAIM:
        subject = str(payload.get("subject") or "")
        if not subject or authority.claimed:
            return
        authority.claimed = True
        # ``issuer`` and ``auth_mode`` below are copied out of the chain, and
        # every record built here therefore renders them as a CLAIM: `Grant`
        # and `Delegation` carry `observed=False` by construction, so the
        # verification verdict on a chain-read row is "we cannot tell", never
        # "verified". A forged payload can still say `auth_mode: oidc`; what
        # it can no longer do is make a screen agree with it.
        authority.grants[subject] = Grant(
            subject=subject, role="admin", granted_by="self (onboarding)",
            issuer=str(payload.get("issuer") or ""),
            auth_mode=str(payload.get("auth_mode") or ""),
            owner=True, seq=seq, granted_at=ts,
        )
        return

    if effect_type in (EFFECT_GRANT, EFFECT_GRANT_ADMIN):
        subject = str(payload.get("subject") or "")
        role = str(payload.get("role") or "")
        if not subject or not role:
            return
        previous = authority.grants.get(subject)
        authority.grants[subject] = Grant(
            subject=subject, role=role,
            granted_by=str(payload.get("by") or ""),
            issuer=str(payload.get("issuer") or ""),
            auth_mode=str(payload.get("auth_mode") or ""),
            # Ownership is provenance and survives a role change; it is never
            # conferred by an ordinary grant.
            owner=bool(previous.owner) if previous else False,
            seq=seq, granted_at=ts,
        )
        return

    if effect_type == EFFECT_REVOKE:
        authority.grants.pop(str(payload.get("subject") or ""), None)
        return

    if effect_type == EFFECT_DELEGATE:
        delegation_id = str(payload.get("id") or "")
        if not delegation_id:
            return
        authority.delegations[delegation_id] = Delegation(
            id=delegation_id,
            subject=str(payload.get("subject") or ""),
            role=str(payload.get("role") or "admin"),
            granted_by=str(payload.get("by") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            issuer=str(payload.get("issuer") or ""),
            auth_mode=str(payload.get("auth_mode") or ""),
            seq=seq, granted_at=ts,
        )
        return

    if effect_type in (EFFECT_DELEGATION_REVOKED, EFFECT_DELEGATION_EXPIRED):
        delegation = authority.delegations.get(str(payload.get("id") or ""))
        if delegation is None:
            return
        delegation.ended = "revoked" if effect_type == EFFECT_DELEGATION_REVOKED else "expired"
        delegation.ended_reason = str(payload.get("reason") or "")
        return

    if effect_type == EFFECT_REFUSED:
        authority.refusals.append(Refusal(
            rule=str(payload.get("rule") or ""),
            actor=str(payload.get("actor") or ""),
            detail=str(payload.get("detail") or ""),
            at=ts, seq=seq,
        ))
        return

    if effect_type == EFFECT_BG_REQUESTED:
        request_id = str(payload.get("id") or "")
        if not request_id:
            return
        authority.breakglass[request_id] = BreakGlass(
            id=request_id, dataset_id=dataset_id,
            requester=str(payload.get("requester") or ""),
            reason=str(payload.get("reason") or ""),
            window_minutes=int(payload.get("window_minutes") or 60),
            seq=seq,
        )
        return

    if effect_type == EFFECT_BG_VOTE:
        window = authority.breakglass.get(str(payload.get("request_id") or ""))
        if window is None:
            return
        voter = str(payload.get("voter") or "")
        if any(v.get("voter") == voter for v in window.votes):
            return
        window.votes.append({
            "voter": voter,
            "dataset": str(payload.get("voter_dataset") or ""),
            # Carried, and labelled as what it is: a claim read out of the
            # chain. warrant did not witness this voter's verification and
            # does not render one. A quorum vote is exactly the kind of record
            # a forger would want to look vouched-for.
            **identity_fields(
                str(payload.get("auth_mode") or ""),
                str(payload.get("issuer") or ""),
            ),
            "at": ts,
            "seq": seq,
        })
        return

    if effect_type == EFFECT_BG_ENTERED:
        window = authority.breakglass.get(str(payload.get("request_id") or ""))
        if window is None:
            return
        window.entered_at = ts
        window.expires_at = str(payload.get("expires_at") or "")
        window.approval_id = str(payload.get("approval_id") or "")
        return

    if effect_type == EFFECT_BG_EXITED:
        window = authority.breakglass.get(str(payload.get("request_id") or ""))
        if window is None:
            return
        window.exited_at = ts
        window.exit_reason = str(payload.get("reason") or "")
        return


__all__ = ["Snapshot", "ChainRead", "build", "read_chain",
           "REASON_UNREACHABLE", "REASON_CHAIN_INVALID", "REASON_WINDOW",
           "REASON_PAGING_INCOMPLETE"]
