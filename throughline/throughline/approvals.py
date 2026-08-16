"""Durable approval queue.

An irreversible effect that reaches this queue waits for a human. There is no
expiry, no deadline, no auto-approve and **no timeout code path**: nothing in
this module or in ``gate.py`` accepts, stores or passes a timeout, because a
queue that drains itself after N seconds is not an approval queue — it is an
auto-executor with extra steps. ``tests/test_approval_queue.py`` asserts this
property against the source, so it cannot be reintroduced quietly.

The queue is durable, and durability here means the **ledger**, not the JSON
file. ``approvals.json`` is a cache: a fast, convenient snapshot of the same
facts the hash chain already records. It used to be treated as the source of
truth, and ``_load`` swallowed ``OSError`` and ``JSONDecodeError`` and
returned an empty queue — so a truncated write left an operator looking at an
empty gate while the ledger still said ``effect.queued`` for every held
effect. That is the inverse of this product's thesis, and it is fixed here:

* the authoritative queue is rebuilt from the ledger on every load;
* the cache can only *add* entries the ledger never saw (a direct
  ``enqueue`` with no gate behind it), never contradict it;
* where the two disagree the ledger wins, which means a decision the chain
  does not record leaves the effect **held**;
* a cache that fails to load is an alarm — logged, ledgered, and surfaced on
  ``/healthz`` as degraded. It is never a silent empty queue.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import Approval

LOGGER = logging.getLogger("throughline.approvals")


class ApprovalNotFound(KeyError):
    pass


class AlreadyDecided(RuntimeError):
    pass


class ApprovalQueue:
    """A durable, human-only queue of held effects."""

    def __init__(self, path: str | os.PathLike[str], ledger: Any = None) -> None:
        self.path = Path(path)
        self.ledger = ledger
        self._lock = threading.RLock()
        self._items: dict[str, Approval] = {}
        self._waiters: dict[str, threading.Event] = {}
        self.health: dict[str, Any] = {
            "degraded": False,
            "cache": "ok",
            "cache_error": None,
            "rebuilt_from_ledger": 0,
            "cache_only_entries": 0,
            "reconciled": [],
            "detail": None,
        }
        self._load()

    # ------------------------------------------------------------ durability

    def _load(self) -> None:
        """Rebuild from the ledger, then fold in whatever the cache adds.

        Never returns an empty queue because a file was unreadable. If the
        cache is gone or corrupt the ledger still knows what is held, and if
        the ledger is unavailable too, that is an alarm, not a clean gate.
        """
        cached = self._read_cache()
        from_ledger = self._read_ledger()

        self._items.update(from_ledger)
        self.health["rebuilt_from_ledger"] = len(from_ledger)

        reconciled: list[dict[str, Any]] = []
        cache_only = 0
        for approval_id, approval in cached.items():
            authoritative = self._items.get(approval_id)
            if authoritative is None:
                # The ledger never saw it (a direct enqueue). Keep it: losing
                # a hold is the failure mode we are here to prevent.
                self._items[approval_id] = approval
                cache_only += 1
                continue
            if authoritative.state != approval.state:
                # The chain is the record. A decision it does not carry does
                # not release an effect — the effect stays held.
                reconciled.append({
                    "approval_id": approval_id,
                    "cache_state": approval.state,
                    "ledger_state": authoritative.state,
                    "resolved_to": authoritative.state,
                })
        self.health["cache_only_entries"] = cache_only
        self.health["reconciled"] = reconciled

        if self.health["degraded"] or reconciled:
            self._alarm()
        if self.health["degraded"] and self._items:
            # Repair the cache from the authoritative rebuild so the next boot
            # is cheap again. The ledger is untouched either way.
            try:
                self._persist()
                self.health["cache"] = "rebuilt"
            except OSError as exc:  # pragma: no cover - disk still broken
                self.health["detail"] = f"cache could not be rewritten: {exc}"

    def _read_cache(self) -> dict[str, Approval]:
        """Read the JSON cache. A failure degrades; it never empties."""
        if not self.path.exists():
            return {}
        items: dict[str, Approval] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except (OSError, json.JSONDecodeError) as exc:
            self.health.update({
                "degraded": True,
                "cache": "unreadable",
                "cache_error": f"{type(exc).__name__}: {exc}",
                "detail": (
                    f"approvals cache at {self.path} is unreadable; the queue "
                    "was rebuilt from the ledger, which is the durable record"
                ),
            })
            LOGGER.error(
                "approvals cache at %s is unreadable (%s) — rebuilding the "
                "queue from the ledger; the gate is NOT empty",
                self.path, exc,
            )
            return {}
        if not isinstance(raw, list):
            self.health.update({
                "degraded": True,
                "cache": "malformed",
                "cache_error": f"expected a list, found {type(raw).__name__}",
                "detail": f"approvals cache at {self.path} is not a list of approvals",
            })
            LOGGER.error("approvals cache at %s is not a list", self.path)
            return {}
        dropped = 0
        for item in raw:
            try:
                approval = Approval.model_validate(item)
            except Exception:  # a malformed row must not take the queue down
                dropped += 1
                continue
            items[approval.id] = approval
        if dropped:
            self.health.update({
                "degraded": True,
                "cache": "partial",
                "cache_error": f"{dropped} malformed row(s) in the cache",
                "detail": (
                    f"{dropped} row(s) in {self.path} did not validate; the "
                    "ledger rebuild covers anything the gate queued"
                ),
            })
            LOGGER.error("%d malformed row(s) in approvals cache %s", dropped, self.path)
        return items

    def _read_ledger(self) -> dict[str, Approval]:
        """Replay the chain into the queue it implies.

        ``effect.queued`` created a hold; ``approval.decided`` released or
        rejected it; ``approval.refused`` leaves it held, which is the point.
        """
        if self.ledger is None:
            return {}
        try:
            entries = self.ledger.read()
        except Exception as exc:
            self.health.update({
                "degraded": True,
                "cache": self.health["cache"],
                "detail": f"ledger could not be replayed: {exc}",
            })
            LOGGER.error("approval queue could not replay the ledger: %s", exc)
            return {}

        items: dict[str, Approval] = {}
        for entry in entries:
            kind = entry.get("type")
            body = entry.get("body") or {}
            if kind == "effect.queued":
                approval_id = body.get("approval_id")
                effect_id = body.get("id")
                if not approval_id or not effect_id:
                    continue
                try:
                    items[approval_id] = Approval(
                        id=approval_id,
                        effect_id=effect_id,
                        effect_type=body.get("effect_type"),
                        reversibility_class=body.get("reversibility_class")
                        or body.get("reversibility")
                        or "irreversible",
                        description=body.get("description"),
                        signal_id=body.get("signal_id"),
                        queued_at=entry.get("ts") or datetime.now(timezone.utc),
                    )
                except Exception:
                    continue
            elif kind == "approval.decided":
                approval = items.get(body.get("approval_id"))
                if approval is None:
                    continue
                approval.state = (
                    "approved" if body.get("decision") == "approve" else "rejected"
                )
                approval.decided_by = body.get("decided_by")
                approval.rationale = body.get("rationale")
                approval.decided_at = entry.get("ts")
        return items

    def _alarm(self) -> None:
        """Surface the degradation: log, ledger, and /healthz all say so."""
        payload = {
            "reason": "approval cache did not agree with the ledger at load",
            "path": str(self.path),
            **{k: v for k, v in self.health.items() if k != "detail"},
            "detail": self.health["detail"],
        }
        LOGGER.error("approvals degraded: %s", payload)
        if self.ledger is None:
            return
        try:
            self.ledger.append("approvals.alarm", payload)
        except Exception as exc:  # pragma: no cover - ledger already alarmed
            LOGGER.error("could not ledger the approvals alarm: %s", exc)

    def _persist(self) -> None:
        """Write the queue atomically: temp file then rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [a.model_dump(mode="json") for a in self._items.values()]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    # ----------------------------------------------------------------- queue

    def enqueue(
        self,
        effect_id: str,
        reversibility_class: str,
        effect_type: Optional[str] = None,
        description: Optional[str] = None,
        signal_id: Optional[str] = None,
        approval_id: Optional[str] = None,
    ) -> Approval:
        with self._lock:
            approval = Approval(
                id=approval_id or f"apr-{uuid.uuid4().hex[:12]}",
                effect_id=effect_id,
                effect_type=effect_type,
                reversibility_class=reversibility_class,
                description=description,
                signal_id=signal_id,
            )
            self._items[approval.id] = approval
            self._waiters[approval.id] = threading.Event()
            self._persist()
            return approval

    def get(self, approval_id: str) -> Approval:
        with self._lock:
            if approval_id not in self._items:
                raise ApprovalNotFound(approval_id)
            return self._items[approval_id]

    def for_effect(self, effect_id: str) -> Optional[Approval]:
        with self._lock:
            for approval in self._items.values():
                if approval.effect_id == effect_id:
                    return approval
            return None

    def pending(self) -> list[Approval]:
        with self._lock:
            return [a for a in self._items.values() if a.state == "pending"]

    def pending_matching(self, effect_type: Optional[str], description: Optional[str]) -> int:
        """Count PENDING approvals sharing this exact (effect_type,
        description) signature — how deep a near-duplicate backlog already
        is. Used by the gate to cap unbounded accumulation of near-identical
        proposals (a repeatedly-firing feed, real or synthetic) without
        touching anything already queued or decided.
        """
        with self._lock:
            return sum(
                1 for a in self._items.values()
                if a.state == "pending"
                and a.effect_type == effect_type
                and a.description == description
            )

    def all(self) -> list[Approval]:
        with self._lock:
            return list(self._items.values())

    def decide(
        self,
        approval_id: str,
        decision: str,
        decided_by: str,
        rationale: Optional[str] = None,
        issuer: Optional[str] = None,
        auth_mode: Optional[str] = None,
    ) -> Approval:
        """Record a human decision. ``decided_by`` is required, always.

        ``issuer`` and ``auth_mode`` are recorded as attested. They are not
        verified here and are not treated as permission — the role check
        has already happened — but WHO approved is only half a claim
        without WHICH AUTHORITY said so, and the second half is the half a
        reader can go and check.
        """
        if not decided_by:
            raise ValueError("decided_by (the approver's subject) is required")
        with self._lock:
            approval = self.get(approval_id)
            if approval.state != "pending":
                raise AlreadyDecided(
                    f"{approval_id} was already {approval.state} by {approval.decided_by!r}"
                )
            approval.state = "approved" if decision == "approve" else "rejected"
            approval.decided_by = decided_by
            approval.issuer = issuer
            approval.auth_mode = auth_mode
            approval.rationale = rationale
            approval.decided_at = datetime.now(timezone.utc)
            self._persist()
            waiter = self._waiters.get(approval_id)
            if waiter is not None:
                waiter.set()
            return approval

    def wait(self, approval_id: str) -> Approval:
        """Block until a human decides. Deliberately unbounded.

        This call has no timeout parameter and never gains one: it returns
        when — and only when — a human has decided.
        """
        with self._lock:
            approval = self.get(approval_id)
            if approval.state != "pending":
                return approval
            waiter = self._waiters.setdefault(approval_id, threading.Event())
        waiter.wait()
        return self.get(approval_id)

    def snapshot(self) -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in self.all()]
