"""The console model: everything the views and the tools both read.

There is exactly one implementation of each console action, and both the
HTTP API and NemoClerk's tools go through it. That is what
``test://helm/openapi-surface-complete`` means in practice: no privileged
path exists, because there is no second implementation to privilege.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from helm.attest import ActionResult, LastSeen, Reading, Step
from helm.clients import NO_CALLER_TOKEN, Federation
from helm.signet import Signet

STAGES = ("signal", "judged", "gated", "approved", "executed", "ledgered")

# Matches an http(s) address with a host and optional port — the shape of
# every sibling's ``base_url`` (``http://127.0.0.1:8600``) and of the string
# an httpx connection error sometimes embeds. Used to keep one of those out
# of a response an anonymous caller can read; see ``Console._redact``.
_ADDRESS_RE = re.compile(r"https?://[^\s\"'<>]+")

FEEDS = (
    {
        "name": "document",
        "component": "docket",
        "status": "✓ 2/hr",
        "kind": "ok",
        "signal_class": "permit.document",
    },
    {
        "name": "telemetry",
        "component": "breaker",
        "status": "✓ live",
        "kind": "ok live",
        "signal_class": "grid.telemetry",
    },
    {
        "name": "incident",
        "component": "siren",
        "status": "+hot-loaded",
        "kind": "hot",
        "signal_class": "fire.incident",
    },
    {
        "name": "video",
        "component": "blindspot",
        "status": "batch",
        "kind": "",
        "signal_class": "camera.frame",
    },
)


@dataclass
class Decision:
    ok: bool
    refused: bool
    reason: str
    status: int
    data: dict[str, Any]


class Console:
    """Reads the real federation; never fabricates a value it cannot fetch."""

    def __init__(self, federation: Federation, signet: Signet) -> None:
        self.federation = federation
        self.signet = signet
        self._status_cache: dict[str, Any] = {}
        self._status_at = 0.0
        self._ledger_cache: list[dict[str, Any]] = []
        self._ledger_at = 0.0
        # The last value that WAS known, so an outage can say "last seen 6
        # held at 06:12Z" instead of the confident zero that used to render.
        self.last_seen = LastSeen(Path(signet.data_dir) / "last-seen.json")
        #: class name -> when it was (re)registered, for the arrival flash
        self.arrivals: dict[str, float] = {}

    ARRIVAL_WINDOW = 15.0

    def mark_arrival(self, feed_name: str) -> None:
        """A class that just hot-loaded gets its arrival choreography."""
        self.arrivals[feed_name] = time.time()

    def recently_arrived(self, feed_name: str) -> bool:
        at = self.arrivals.get(feed_name)
        return at is not None and (time.time() - at) < self.ARRIVAL_WINDOW

    # ------------------------------------------------------------ health
    def service_statuses(self, max_age: float = 3.0) -> dict[str, Any]:
        now = time.time()
        if self._status_cache and now - self._status_at < max_age:
            return self._status_cache
        statuses = {
            name: {
                "name": status.name,
                "url": status.url,
                "online": status.online,
                "label": status.label,
                "detail": status.detail,
                "health": status.health,
            }
            for name, status in self.federation.statuses().items()
        }
        self._status_cache = statuses
        self._status_at = now
        return statuses

    @staticmethod
    def _redact(text: Any) -> str:
        """Strip any http(s) address out of free text before it can reach
        an anonymous caller. A connection failure's own error string can
        embed the address it failed to reach, so a caller that redacts
        ``url`` and forwards ``detail`` unexamined can still leak it back.
        """
        return _ADDRESS_RE.sub("[address withheld]", str(text or ""))

    def sibling_reachability(self, *, reveal_addresses: bool = False) -> dict[str, Any]:
        """Every sibling's NAME and STATE — never its loopback address to an
        anonymous caller.

        ``/healthz`` and ``/feeds`` are unauthenticated and answer 200 to
        anyone; both used to publish ``http://127.0.0.1:8600`` through
        ``:8604`` — one per sibling — straight into that response. Four
        reviewers found it, and throughline's own disclosure block claims
        internal host:port topology is redacted from every unauthenticated
        response "on this surface and every other one", which made helm's
        leak falsify a sibling's published guarantee. This is the one place
        that answer is assembled now, so every anonymous-reachable route
        reads it instead of building its own dict by hand and drifting
        again. An authenticated caller the admin role trusts with
        infrastructure detail may still ask for the address.
        """
        statuses = self.service_statuses()
        out: dict[str, Any] = {}
        for name, status in statuses.items():
            entry: dict[str, Any] = {
                "online": bool(status.get("online")),
                "label": status.get("label", ""),
                "detail": self._redact(status.get("detail", "")),
            }
            if reveal_addresses:
                entry["url"] = status.get("url", "")
            out[name] = entry
        return out

    # ------------------------------------------------------------- feeds
    def feeds(self, *, reveal_addresses: bool = False) -> list[dict[str, Any]]:
        statuses = self.service_statuses()
        out = []
        for feed in FEEDS:
            status = statuses.get(feed["component"], {})
            online = bool(status.get("online"))
            entry = dict(feed)
            entry["online"] = online
            if reveal_addresses:
                entry["url"] = status.get("url", "")
            entry["detail"] = self._redact(status.get("detail", ""))
            if not online:
                entry["status"] = "component offline"
                entry["kind"] = "offline"
            entry["events"] = self.events_for(feed["name"]) if online else []
            entry["arrived"] = self.recently_arrived(feed["name"])
            out.append(entry)
        return out

    def events_for(self, feed_name: str, limit: int = 6) -> list[dict[str, Any]]:
        """Event rows for a feed, read from the substrate's own ledger."""
        entries = self._recent_entries()
        if not entries:
            return []
        rows = []
        for entry in reversed(entries):
            body = entry.get("body") or {}
            kind = entry.get("type", "")
            klass = str(body.get("class") or body.get("signal_class") or "")
            if feed_name != "all" and not self._matches_feed(feed_name, kind, klass, body):
                continue
            rows.append(
                {
                    "seq": entry.get("seq"),
                    "ts": str(entry.get("ts", ""))[11:19],
                    "type": kind,
                    "id": body.get("id") or body.get("effect_id") or "",
                    "description": self._describe(kind, body),
                    "hash_prefix": str(entry.get("sha256", ""))[:8],
                    "verified": bool(entry.get("verified")),
                    "effect_id": body.get("effect_id") or (
                        body.get("id") if kind.startswith("effect") else ""
                    ),
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _recent_entries(self, max_age: float = 2.0) -> list[dict[str, Any]]:
        """One ledger read serves all four feed panes on a page render."""
        now = time.time()
        if self._ledger_cache and now - self._ledger_at < max_age:
            return self._ledger_cache
        reply = self.federation.throughline.ledger(limit=120)
        if not reply.ok:
            return []
        self._ledger_cache = list(reply.data.get("entries", []))
        self._ledger_at = now
        return self._ledger_cache

    @staticmethod
    def _matches_feed(feed_name: str, kind: str, klass: str, body: dict[str, Any]) -> bool:
        haystack = f"{kind} {klass} {body.get('source', '')}".lower()
        keys = {
            "document": ("permit", "document", "docket"),
            "telemetry": ("telemetry", "grid", "breaker", "battery", "dispatch"),
            "incident": ("incident", "fire", "siren", "config"),
            "video": ("frame", "camera", "blindspot", "video"),
        }[feed_name]
        return any(k in haystack for k in keys)

    @staticmethod
    def _describe(kind: str, body: dict[str, Any]) -> str:
        for field_name in ("description", "reason", "rejection_reason", "verdict", "message"):
            value = body.get(field_name)
            if value:
                return str(value)[:140]
        # The template already prints the entry kind; do not repeat it.
        return str(body.get("id") or body.get("effect_id") or "")

    # -------------------------------------------------- three-state reads
    def substrate_online(self) -> bool:
        return bool(self.service_statuses().get("throughline", {}).get("online"))

    def substrate_alarms(self) -> dict[str, Any]:
        """What the substrate says about ITSELF, including its own alarms.

        A substrate that is answering but degraded is a third state again:
        reachable is not the same as healthy.
        """
        status = self.service_statuses().get("throughline", {})
        if not status.get("online"):
            return {"known": False, "status": "unknown", "alarms": [], "allowlist": []}
        health = status.get("health") or {}
        return {
            "known": True,
            "status": str(health.get("status", "unknown")),
            "alarms": list(health.get("alarms") or []),
            "allowlist": list((health.get("config") or {}).get("allowlist") or []),
        }

    def gate(self) -> Reading:
        """What is held at the gate — or the honest admission that we cannot see.

        Rendering an unreachable gate as "0 waiting — nothing is held" tells
        an operator that nothing needs them at the exact moment the system
        has lost the ability to know. Absence of evidence is reported as
        absence of evidence.
        """
        reply = self.federation.throughline.approvals("pending")
        if not reply.ok:
            return self.last_seen.reading(
                "gate.pending",
                "throughline",
                reply.error or "throughline is not responding",
            )
        pending = list(reply.data.get("approvals", []))
        self.last_seen.record("gate.pending", len(pending))
        return Reading.seen(pending, source="throughline")

    def chain(self) -> Reading:
        reply = self.federation.throughline.verify()
        if not reply.ok:
            return self.last_seen.reading(
                "ledger.chain",
                "throughline",
                reply.error or "throughline is not responding",
            )
        data = dict(reply.data)
        self.last_seen.record(
            "ledger.chain",
            {"chain_length": data.get("chain_length"), "valid": data.get("valid")},
        )
        return Reading.seen(data, source="throughline")

    def head(self) -> Reading:
        reply = self.federation.throughline.ledger(limit=1)
        if not reply.ok:
            return self.last_seen.reading(
                "ledger.head", "throughline", reply.error or "not responding"
            )
        head = str(reply.data.get("head", ""))
        self.last_seen.record("ledger.head", head[:12])
        return Reading.seen(head, source="throughline")

    # ---------------------------------------------------------- overview
    def overview(self, *, reveal_addresses: bool = False) -> dict[str, Any]:
        gate = self.gate()
        chain = self.chain()
        feeds = [
            {
                "name": f["name"],
                "component": f["component"],
                "online": f["online"],
                "status": f["status"],
            }
            for f in self.feeds()
        ]
        chain_value = chain.value if chain.known else {}
        return {
            # Kept for the published contract, but NEVER the only signal: a
            # consumer that reads pending_approvals without reading
            # gate_known would mistake "cannot see" for "nothing there".
            "feeds": feeds,
            "pending_approvals": len(gate.value) if gate.known else None,
            "gate_known": gate.known,
            "gate": gate.to_dict(),
            "ledger_valid": bool(chain_value.get("valid")) if chain.known else None,
            "ledger_known": chain.known,
            "chain_length": chain_value.get("chain_length") if chain.known else None,
            "substrate_online": self.substrate_online(),
            # By name and state, never by address, unless the caller may see
            # one — see ``sibling_reachability``. This field used to be
            # ``self.service_statuses()`` verbatim, which is how /overview
            # leaked every sibling's loopback address to an anonymous GET.
            "services": self.sibling_reachability(reveal_addresses=reveal_addresses),
        }

    # ----------------------------------------------------- signal classes
    #: Which component can actually reload a class. A class whose owner has
    #: no reload surface is never reported as reloaded.
    RELOADABLE = {"incident": ("siren", "/hot-reload")}

    def reload_class(self, name: str) -> ActionResult:
        """Reload a signal class, and report only what actually happened.

        This used to return ``ok: true`` with "3 incident signals emitted
        after the swap" while the component that would have emitted them was
        down — and it sent ``class_name`` to an endpoint whose contract takes
        ``path``/``emit``, so every class reloaded incident.yaml and returned
        a byte-identical body. An action that reports an effect which did not
        occur is worse than a display that overstates: it is a false record
        of the world, in the one product built to prove the opposite.
        """
        owner_path = self.RELOADABLE.get(name)
        if owner_path is None:
            owners = {f["name"]: f["component"] for f in FEEDS}
            return ActionResult(
                action="reload",
                target=name,
                performed=False,
                detail=(
                    f"the {name} class is owned by {owners.get(name, 'no component')}, "
                    "which publishes no reload surface. Nothing was reloaded."
                ),
                steps=[
                    Step("validated", "skipped", "no reload endpoint for this class"),
                    Step("registered", "skipped", "no reload endpoint for this class"),
                    Step("flowing", "skipped", "no reload endpoint for this class"),
                ],
            )

        owner, path = owner_path
        if not self.service_statuses().get(owner, {}).get("online"):
            return ActionResult(
                action="reload",
                target=name,
                performed=False,
                detail=f"{owner} is not responding, so the class was not reloaded",
                steps=[
                    Step("validated", "skipped", f"{owner} unreachable"),
                    Step("registered", "skipped", f"{owner} unreachable"),
                    Step("flowing", "skipped", f"{owner} unreachable"),
                ],
            )

        client = getattr(self.federation, owner)
        reply = client.post(path, json={"emit": 3})
        if not reply.ok:
            body = reply.data if isinstance(reply.data, dict) else {}
            named = "; ".join(
                f"{row.get('step')}: {row.get('detail')}"
                for row in (body.get("steps") or [])
                if isinstance(row, dict) and row.get("state") == "refused"
            )
            reason = (named or reply.error or f"{owner} refused the reload")[:240]
            # Name WHICH refusal this is. throughline confines config loads to
            # an allowlisted directory, so a swap can be refused BY PATH
            # before it ever reaches the rule refusal the beat exists to show
            # — the beat looks like it fired while demonstrating the wrong
            # thing.
            allowlist = self.substrate_alarms().get("allowlist") or []
            if any(
                marker in reason
                for marker in ("allowlist", "config-source", "unreadable config",
                               "No such file")
            ):
                reason += (
                    ". This is a PATH refusal, not the rule refusal this beat "
                    "demonstrates: throughline only loads config from "
                    + (", ".join(allowlist) or "its own config directory")
                    + ". Set THROUGHLINE_CONFIG_DIRS to include "
                    f"{owner}'s config directory to exercise the real beat."
                )
            return ActionResult(
                action="reload",
                target=name,
                performed=False,
                substrate=self._substrate_of(reply.data),
                detail=reason,
                steps=[Step("validated", "refused", reason[:160])],
            )

        data = reply.data if isinstance(reply.data, dict) else {}
        substrate = self._substrate_of(data)

        # throughline holds a reversibility DOWNGRADE at the gate rather than
        # applying it: the downgrade is itself an irreversible effect. That is
        # a third outcome, and reporting it as success would be the same class
        # of lie as reporting an unreachable component as reloaded.
        # accepted / refused / HELD is three-valued. Collapsing it to two is
        # the same bug shape as unknown-vs-empty: siren was reporting "the
        # substrate accepted the swap" for a swap throughline had held.
        state = str(data.get("state", "")).lower()
        held = (
            reply.status == 202
            or bool(data.get("held"))
            or state in {"held", "config.downgrade_held"}
            or "held" in state
        )
        if held:
            return ActionResult(
                action="reload",
                target=name,
                performed=False,
                substrate=substrate,
                detail=(
                    "HELD AT THE GATE — this swap is itself an irreversible "
                    "effect, so it is waiting for a human exactly like any "
                    "other. Nothing has been registered and nothing is "
                    "flowing. "
                    + (
                        f"Approval {data['approval_id']} is in the queue. "
                        if data.get("approval_id")
                        else ""
                    )
                    + str(data.get("detail") or data.get("reason") or "")
                ).strip(),
                steps=[
                    Step("validated", "ok", "the candidate config parsed"),
                    Step("registered", "unknown", "held at the gate, not applied"),
                    Step("flowing", "skipped", "nothing flows until a human releases it"),
                ],
            )

        steps = [
            Step(
                str(row.get("step", "?")),
                str(row.get("state", "unknown")),
                str(row.get("detail", "")),
            )
            for row in (data.get("steps") or [])
            if isinstance(row, dict)
        ] or [Step("registered", "ok", "")]
        # siren answers 200 with state:"refused" and the reason inside its
        # steps, so a caller that only checks the HTTP status sees success.
        refused_steps = [st for st in steps if st.state == "refused"]
        if state == "refused" or refused_steps:
            reason = "; ".join(
                f"{st.name}: {st.detail}" for st in refused_steps
            ) or str(data.get("refusal") or "the swap was refused")
            allowlist = self.substrate_alarms().get("allowlist") or []
            if allowlist and any(
                path not in reason for path in allowlist
            ) and ("unreadable" in reason or "No such file" in reason
                   or "allowlist" in reason):
                reason += (
                    " — this is a PATH refusal, not the rule refusal the beat "
                    "demonstrates. throughline only loads config from "
                    + ", ".join(allowlist)
                    + f"; set THROUGHLINE_CONFIG_DIRS to include {owner}'s "
                    "config directory to exercise the real one."
                )
            return ActionResult(
                action="reload",
                target=name,
                performed=False,
                substrate=substrate,
                detail=reason[:400],
                steps=steps,
            )

        counts: dict[str, int] = {}
        emitted = data.get("emitted")
        if isinstance(emitted, int):
            counts["signals emitted"] = emitted
        # "performed" means it HAPPENED, not "the call returned 200".
        performed = (
            all(step.state == "ok" for step in steps)
            and not data.get("refusal")
            and not data.get("refused")
        )
        self.mark_arrival(name)
        return ActionResult(
            action="reload",
            target=name,
            performed=performed,
            substrate=substrate,
            detail=str(data.get("refusal") or "")[:200],
            steps=steps,
            counts=counts,
        )

    @staticmethod
    def _substrate_of(data: Any) -> str:
        if isinstance(data, dict) and data.get("substrate"):
            return str(data["substrate"])
        return "real"

    # --------------------------------------------------------- approvals
    def approvals(self, state: str | None = None) -> list[dict[str, Any]]:
        reply = self.federation.throughline.approvals(state)
        if not reply.ok:
            return []
        return list(reply.data.get("approvals", []))

    def approval(self, approval_id: str) -> dict[str, Any] | None:
        for item in self.approvals():
            if item.get("id") == approval_id:
                return item
        return None

    def decide(
        self,
        *,
        approval_id: str,
        decision: str,
        approver: str,
        role: str,
        principal: str = "",
        rationale: str | None = None,
        attested_by: str | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """The role gate.

        ``approver`` is the SIGNET SUBJECT FROM THE SESSION and is the only
        thing that ever reaches the ledger. ``attested_by`` is what the
        caller CLAIMED in the request body: it is checked against the
        session and then discarded.

        This used to take the caller's string and forward it untouched, so
        an unauthenticated POST could write any name it liked into the
        approval record. The chain recorded that faithfully — integrity
        without authenticity, which is the more dangerous failure, because
        the record looks exactly as trustworthy as a true one.
        """
        principal = principal or approver
        record = self.approval(approval_id)
        if record is None:
            return {
                "ok": False,
                "refused": False,
                "status": 404,
                "reason": f"no approval {approval_id!r} in the queue",
            }
        irreversible = record.get("reversibility_class") == "irreversible"

        # An attestation that disagrees with the session is an impersonation
        # attempt, whether or not it was meant as one.
        if attested_by and approver and attested_by != approver:
            return {
                "ok": False,
                "refused": True,
                "status": 403,
                "refusal_kind": "impersonation",
                "claimed": attested_by,
                "actual": approver,
                "reason": (
                    f"REFUSED: this session is {approver}, and the request "
                    f"attested {attested_by}. The approver written to the "
                    "ledger is the signet subject of the session, never a "
                    "name supplied in the body. The mismatch is on the record."
                ),
                "principal": principal,
                "approval": record,
                "summary": (
                    f"REFUSED · impersonation · session {approver} claimed "
                    f"to be {attested_by}"
                ),
            }

        # An agent is identified by its PRINCIPAL, not by a cookie — that is
        # how the MCP surface works and the refusal beat depends on it. A
        # human with no session is a different thing entirely.
        if role != "agent" and (not authenticated or not approver):
            return {
                "ok": False,
                "refused": True,
                "status": 403,
                "refusal_kind": "anonymous",
                "reason": (
                    "REFUSED: deciding an approval requires a signed-in signet "
                    "identity. This request carried no session, so there is no "
                    "subject to write into the record. Sign in first."
                ),
                "principal": principal or "anonymous",
                "approval": record,
                "summary": "REFUSED · anonymous caller · no signet subject",
            }

        if role == "viewer":
            return {
                "ok": False,
                "refused": True,
                "status": 403,
                "refusal_kind": "role",
                "reason": (
                    "REFUSED: the viewer role may read the queue but may not "
                    "decide. Ask an operator."
                ),
                "principal": principal,
                "approval": record,
                "summary": (
                    f"REFUSED · role viewer · {approver} may not decide "
                    f"{record.get('description') or record.get('effect_id')}"
                ),
            }

        if role == "agent" and not irreversible:
            # throughline's gate blocks agent principals on IRREVERSIBLE
            # effects only, so forwarding a REVERSIBLE one would not be
            # refused down there — it would be DECIDED. Nothing reversible is
            # queued today, but that is a property of effects.yaml, not a
            # guarantee: one `reversible` + `auto_execute: false` rule and the
            # agent would be approving. The role gate is helm's, and it does
            # not depend on a config file. Refuse here, and do not forward.
            return {
                "ok": False,
                "refused": True,
                "status": 403,
                "refusal_kind": "role",
                "reason": (
                    "REFUSED: deciding a queued effect requires a human signet "
                    f"role. {principal} holds every read and reload tool and "
                    "none of the approve ones — reversibility does not change "
                    "who may decide."
                ),
                "principal": principal,
                "ledgered": False,
                "approval": record,
                "summary": (
                    f"REFUSED · role agent · {principal} may not decide "
                    f"{record.get('description') or record.get('effect_id')}"
                ),
            }

        if role == "agent" and irreversible:
            # Forward the attempt so the substrate ledgers the refusal with
            # this principal, then refuse. Role, not transport.
            forwarded = self.federation.throughline.decide(
                approval_id,
                decision=decision,
                decided_by=principal,
                caller_role="agent",
                rationale=rationale or "agent principal attempted approval",
            )
            return {
                "ok": False,
                "refused": True,
                "status": 403,
                "reason": (
                    "REFUSED: approving an irreversible effect requires a human "
                    f"signet role. {principal} holds every read and reload tool "
                    "and none of the approve ones."
                ),
                "principal": principal,
                # The attempt is only ON THE RECORD if the substrate actually
                # ledgered it. An unauthenticated forward never reached the
                # ledger, so this says False rather than claiming a row that
                # is not there.
                "ledgered": forwarded.status == 403 and not forwarded.unauthenticated,
                "unauthenticated": forwarded.unauthenticated,
                "substrate_status": forwarded.status,
                "approval": record,
                "summary": (
                    f"REFUSED · ledgered as {principal} · "
                    f"{record.get('description') or record.get('effect_id')}"
                ),
            }

        # The approver reaching the chain is the session subject, full stop.
        forwarded = self.federation.throughline.decide(
            approval_id,
            decision=decision,
            decided_by=approver,
            caller_role="agent" if role == "agent" else "human",
            rationale=rationale,
        )
        if forwarded.unauthenticated:
            # NOT a refusal. The gate never saw this decision — helm could not
            # authenticate to the substrate, so the act was never judged. The
            # three-state rule applies to outcomes as much as to readings:
            # "did not happen because we could not ask" is its own state,
            # never a confident "refused" and never a bare 403 passed through.
            outcome = ActionResult(
                action=decision,
                performed=False,
                target=approval_id,
                substrate="throughline",
                detail=forwarded.error or NO_CALLER_TOKEN,
                steps=[
                    Step("authenticate-caller", "refused", forwarded.error),
                    Step("decide", "skipped",
                         "the decision was never sent; the effect is still held "
                         "and nothing was recorded against it"),
                ],
            )
            return {
                "ok": False,
                # `refused` means the GATE declined. It did not.
                "refused": False,
                "unauthenticated": True,
                "status": forwarded.status or 401,
                "refusal_kind": "authentication",
                "reason": (
                    f"COULD NOT DECIDE — "
                    f"{(forwarded.error or NO_CALLER_TOKEN).rstrip('.')}. "
                    "This is an authentication problem between helm and the "
                    "substrate, not a decision about the effect: the gate was "
                    "never asked, and the effect is still held."
                ),
                "principal": principal,
                "ledgered": False,
                "approval": record,
                "action_result": outcome.to_dict(),
                "summary": outcome.summary,
            }
        if not forwarded.ok:
            return {
                "ok": False,
                "refused": forwarded.status == 403,
                "status": forwarded.status or 502,
                "refusal_kind": "gate" if forwarded.status == 403 else "",
                "reason": forwarded.error or "throughline unreachable",
                "principal": principal,
                "approval": record,
            }
        return {
            "ok": True,
            "refused": False,
            "status": 200,
            "reason": "",
            "principal": principal,
            "decided_by": approver,
            "approver_source": "session",
            "data": forwarded.data,
            "approval": (forwarded.data or {}).get("approval", record),
            "summary": (
                f"{decision}d {approval_id} as {approver} — "
                f"{record.get('description') or record.get('effect_id')}"
            ),
        }

    # ------------------------------------------------------------ ledger
    def ledger(self, limit: int = 12, *, reveal_identity: bool = False) -> dict[str, Any]:
        """The ledger tail. ``reveal_identity`` mirrors ``reveal_addresses``:
        throughline pseudonymizes ``decided_by`` for a reader who presents
        no caller token, and helm must stay exactly that anonymous reader
        for any caller it has not itself verified as admin — see
        ``Throughline.ledger``.
        """
        reply = self.federation.throughline.ledger(limit=limit, privileged=reveal_identity)
        if not reply.ok:
            return {
                "entries": [],
                "chain_length": None,
                "head": "",
                "online": False,
                "rows": [],
                "detail": reply.error or "throughline is not responding",
            }
        data = dict(reply.data)
        data["online"] = True
        rows: list[dict[str, Any]] = []
        for entry in reversed(data.get("entries", [])):
            body = entry.get("body") or {}
            row = {
                "seq": entry.get("seq"),
                "kind": entry.get("type"),
                "hash_prefix": str(entry.get("sha256", ""))[:8],
                "verified": bool(entry.get("verified")),
                "effect_id": body.get("effect_id")
                or (body.get("id") if str(entry.get("type", "")).startswith("effect") else ""),
                "ts": str(entry.get("ts", ""))[11:19],
                "subject": (
                    body.get("decided_by")
                    or body.get("changed_by")
                    or body.get("source")
                    or ""
                ),
                "repeats": 1,
            }
            # Four identical approval.refused rows for one approval is noise
            # that hides the chain. Collapse consecutive duplicates with a
            # count — nothing is dropped, the seq of the newest is kept.
            if (
                rows
                and rows[-1]["kind"] == row["kind"]
                and rows[-1]["subject"] == row["subject"]
                and rows[-1]["effect_id"] == row["effect_id"]
            ):
                rows[-1]["repeats"] += 1
                continue
            rows.append(row)
        data["rows"] = rows
        return data

    def verify(self) -> dict[str, Any]:
        """Verification is a THREE-state answer: valid, invalid, or unread.

        Returning ``valid: false, chain_length: 0`` for an unreachable
        substrate asserts a broken empty chain, which is a different and
        more alarming claim than "helm could not read it".
        """
        reply = self.federation.throughline.verify()
        if not reply.ok:
            last, at = self.last_seen.get("ledger.chain")
            return {
                "known": False,
                "valid": None,
                "chain_length": None,
                "online": False,
                "detail": reply.error or "throughline is not responding",
                "last_seen": {"chain": last, "at": at},
            }
        data = dict(reply.data)
        data["online"] = True
        data["known"] = True
        return data

    def effects(self, limit: int = 50) -> dict[str, Any]:
        """Every effect the chain has typed, newest first."""
        reply = self.federation.throughline.ledger(limit=max(limit * 6, 120))
        if not reply.ok:
            return {
                "known": False,
                "effects": [],
                "count": None,
                "detail": reply.error or "throughline is not responding",
            }
        seen: dict[str, dict[str, Any]] = {}
        for entry in reply.data.get("entries", []):
            body = entry.get("body") or {}
            kind = str(entry.get("type", ""))
            if not kind.startswith("effect"):
                continue
            effect_id = str(body.get("id") or body.get("effect_id") or "")
            if not effect_id:
                continue
            row = seen.setdefault(
                effect_id,
                {
                    "id": effect_id,
                    "reversibility": body.get("reversibility_class")
                    or body.get("reversibility", ""),
                    "description": body.get("description", ""),
                    "signal_id": body.get("signal_id", ""),
                    "status": "",
                    "seq": entry.get("seq"),
                    "walk": f"/walk/{effect_id}/json",
                },
            )
            row["status"] = kind.split(".", 1)[-1]
            row["seq"] = entry.get("seq")
        effects = sorted(seen.values(), key=lambda r: r["seq"] or 0, reverse=True)[:limit]
        return {"known": True, "effects": effects, "count": len(effects)}

    def newest_effect_id(self) -> str:
        for row in self.ledger(limit=60).get("rows", []):
            if row.get("effect_id"):
                return str(row["effect_id"])
        return ""

    def walk(self, effect_id: str) -> dict[str, Any]:
        reply = self.federation.throughline.walk(effect_id)
        if not reply.ok:
            return {"effect_id": effect_id, "hops": [], "hop_count": 0, "online": False}
        data = dict(reply.data)
        data["online"] = True
        return data

    # ---------------------------------------------------------- composed
    def composed(self) -> dict[str, Any]:
        """The composed state assembles ON a gate-hold, not on a schedule."""
        pending = self.approvals("pending")
        gate_hold = next(
            (a for a in pending if a.get("reversibility_class") == "irreversible"),
            None,
        )
        statuses = self.service_statuses()
        panes = []
        for feed in FEEDS:
            status = statuses.get(feed["component"], {})
            panes.append(
                {
                    "component": feed["component"],
                    "feed": feed["name"],
                    "online": bool(status.get("online")),
                    "label": status.get("label", "component offline"),
                    "detail": self._redact(status.get("detail", "")),
                }
            )
        return {
            "assembled": gate_hold is not None,
            "trigger": "gate-hold" if gate_hold else "none",
            "gate_hold": gate_hold,
            "panes": panes,
            "ledger": self.ledger(limit=8).get("rows", []),
            "waiting_since": (gate_hold or {}).get("queued_at", ""),
        }

    # ----------------------------------------------------------- staging
    #: What each stage means for a REVERSIBLE route versus an IRREVERSIBLE
    #: effect. The console used to print "human-signed" under APPROVED for
    #: every chain, including docket's — on the same screen as its own
    #: banner saying routing auto-executes with no human. Claiming a human
    #: signature that never happened is a false claim on the exact axis the
    #: product exists to prove.
    STAGE_SUBS = {
        "irreversible": (
            "ingested", "judged", "gate-checked", "human-signed", "applied",
            "hash-verified",
        ),
        "reversible": (
            "ingested", "judged", "auto — reversible",
            "no signature required (reversible)", "applied", "hash-verified",
        ),
        "mixed": (
            "flowing", "flowing", "gate-checked", "signed or auto, by class",
            "applied", "hash-verified",
        ),
    }

    def stage_states(
        self,
        approval: dict[str, Any] | None,
        *,
        reversibility: str = "mixed",
    ) -> list[dict[str, str]]:
        """Where one event stands in the one loop, labelled by its class."""
        if approval is not None:
            reversibility = str(
                approval.get("reversibility_class") or reversibility
            )
        subs = self.STAGE_SUBS.get(reversibility, self.STAGE_SUBS["mixed"])

        if approval is None:
            return [
                {"stage": stage, "state": "done", "sub": sub}
                for stage, sub in zip(STAGES, subs)
            ]

        state = approval.get("state", "pending")
        if state == "pending":
            states = ["done", "done", "current", "pending", "pending", "pending"]
            subs = list(subs)
            subs[2] = "waiting — no timeout exists"
            subs[3] = subs[4] = subs[5] = ""
        elif state == "rejected":
            states = ["done", "done", "refused", "pending", "pending", "pending"]
            subs = list(subs)
            subs[2] = "refused"
            subs[3] = subs[4] = subs[5] = ""
        else:
            states = ["done"] * 6
            subs = list(subs)
            if reversibility == "irreversible" and approval.get("decided_by"):
                subs[3] = f"signed by {approval['decided_by']}"
        return [
            {"stage": stage, "state": state_, "sub": sub}
            for stage, state_, sub in zip(STAGES, states, subs)
        ]
