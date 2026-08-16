"""An in-memory stand-in for the federation, so tests need no network.

The fake reproduces throughline's CONTRACT, not a convenient version of it:
``decided_by`` is required, an agent caller on an irreversible effect is
refused 403 and the refusal is appended to the ledger, and every entry
carries a digest. Tests that assert helm's behaviour therefore assert it
against the same rules the real substrate applies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

ZERO = "0" * 64


@dataclass
class FakeReply:
    status_code: int
    payload: Any

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        return self.payload

    @property
    def text(self) -> str:
        return json.dumps(self.payload)


@dataclass
class FakeFederation:
    """One object that answers for every sibling port."""

    online: set[str] = field(
        default_factory=lambda: {"throughline", "docket", "breaker", "siren"}
    )
    entries: list[dict[str, Any]] = field(default_factory=list)
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    effects: dict[str, dict[str, Any]] = field(default_factory=dict)
    tamper: bool = False
    #: GET /ledger honours ``offset``, as the real service does. Set false to
    #: model an OLDER throughline that ignores it and answers with a suffix.
    ledger_paging: bool = True
    #: throughline holds a reversibility downgrade at the gate (202).
    hot_reload_status: int = 200
    hot_reload_body: dict[str, Any] | None = None
    #: The caller token this fake substrate accepts on its PRIVILEGED write
    #: surface. Non-empty by default so the suite exercises the authenticated
    #: path; the conftest puts the same value in the environment.
    caller_token: str = "test-caller-token"

    PORTS = {
        "8600": "throughline",
        "8601": "docket",
        "8602": "breaker",
        "8603": "siren",
        "8604": "blindspot",
    }

    def __post_init__(self) -> None:
        if not self.entries:
            self.append("config.loaded", {"source": "config/effects.yaml", "rules": 4})

    # ------------------------------------------------------------- ledger
    def append(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        prev = self.entries[-1]["sha256"] if self.entries else ZERO
        seq = len(self.entries) + 1
        digest = hashlib.sha256(
            json.dumps([seq, prev, kind, body], sort_keys=True).encode()
        ).hexdigest()
        entry = {
            "seq": seq,
            "prev_hash": prev,
            "ts": f"2026-08-16T05:{seq % 60:02d}:00+00:00",
            "type": kind,
            "body": body,
            "sha256": digest,
            "verified": not self.tamper,
        }
        self.entries.append(entry)
        return entry

    # ------------------------------------------------------------ effects
    def propose(
        self,
        effect_id: str,
        reversibility: str = "irreversible",
        description: str = "microgrid load-shed battery_4",
        signal_id: str = "sig-1",
    ) -> str:
        self.append(
            "signal.ingested",
            {"id": signal_id, "class": "grid.telemetry", "source": "breaker"},
        )
        self.append(
            "effect.proposed",
            {
                "id": effect_id,
                "reversibility": reversibility,
                "reversibility_class": reversibility,
                "description": description,
                "signal_id": signal_id,
                "status": "proposed",
            },
        )
        approval_id = f"apr-{effect_id}"
        self.approvals[approval_id] = {
            "id": approval_id,
            "effect_id": effect_id,
            "effect_type": "microgrid_dispatch",
            "reversibility_class": reversibility,
            "description": description,
            "signal_id": signal_id,
            "state": "pending",
            "status": "pending",
            "queued_at": "2026-08-16T05:06:48Z",
            "decided_at": None,
            "decided_by": None,
            "issuer": None,
            "auth_mode": None,
            "rationale": None,
        }
        self.append(
            "effect.queued",
            {
                "id": effect_id,
                "effect_id": effect_id,
                "approval_id": approval_id,
                "reversibility_class": reversibility,
                "description": description,
                "reason": "irreversible effects are held for a human",
                "status": "queued",
            },
        )
        return approval_id

    # ---------------------------------------------------------- transport
    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        **_: Any,
    ) -> FakeReply:
        port = url.split(":")[2].split("/")[0]
        component = self.PORTS.get(port, "unknown")
        path = "/" + url.split(f":{port}", 1)[1].lstrip("/")
        path = path.split("?")[0]
        if component not in self.online:
            raise _ConnectError(f"{component} is not running")
        handler = getattr(self, f"_{component}", None)
        if handler is None:
            return FakeReply(404, {"detail": "no such service"})
        if component == "throughline":
            refusal = self._caller_token_refusal(
                method, path, json or {}, headers or {}
            )
            if refusal is not None:
                return refusal
        return handler(method, path, json or {}, params or {})

    # -------------------------------------------------- privileged surface
    #
    # A fake that is EASIER TO SATISFY than the real substrate is how
    # integration surprises are manufactured — this module's own docstring
    # says so. throughline authenticates the CALLER on the acts that change
    # who may do what, so this does too: same header, same scope, same named
    # refusal. helm's tests therefore fail here for the same reason they
    # would fail against :8600.
    CALLER_TOKEN_HEADER = "X-Throughline-Caller-Token"
    REFUSAL_CALLER_TOKEN = "caller-token-required"

    def _privileged_act(self, method: str, path: str, body: dict[str, Any]) -> str:
        if method == "POST" and path.startswith("/approvals/") and path.endswith("/decide"):
            return "approval.decide"
        if method == "POST" and path == "/effects":
            effect_type = str(body.get("effect_type") or body.get("type") or "")
            return "effect.propose" if effect_type.startswith("authz.") else ""
        if method == "POST" and path == "/signals":
            signal_class = str(body.get("class") or body.get("signal_class") or "")
            return "signal.ingest" if signal_class.startswith("warrant.") else ""
        return ""

    def _caller_token_refusal(
        self, method: str, path: str, body: dict[str, Any], headers: dict[str, str]
    ) -> FakeReply | None:
        act = self._privileged_act(method, path, body)
        if not act:
            return None  # ordinary fleet traffic, untouched
        presented = headers.get(self.CALLER_TOKEN_HEADER, "")
        if presented and presented == self.caller_token:
            return None
        self.append("write.refused", {
            "act": act,
            "refusal_reason": self.REFUSAL_CALLER_TOKEN,
            "presented_a_token": bool(presented),
        })
        return FakeReply(403, {
            "detail": (
                "caller token required: this act changes who may do what. "
                f"Present {self.CALLER_TOKEN_HEADER}."
            ),
            "refused": True,
            "refusal_reason": self.REFUSAL_CALLER_TOKEN,
            "act": act,
            "message_ui": "Refused — the caller is not authenticated to this substrate.",
        })

    # -------------------------------------------------------- throughline
    def _throughline(
        self, method: str, path: str, body: dict[str, Any], params: dict[str, Any]
    ) -> FakeReply:
        if path == "/healthz":
            return FakeReply(200, {"component": "throughline", "version": "0.2.0"})
        if path == "/approvals" and method == "GET":
            state = params.get("state")
            rows = [
                a for a in self.approvals.values() if not state or a["state"] == state
            ]
            return FakeReply(
                200,
                {
                    "approvals": rows,
                    "pending": len([a for a in rows if a["state"] == "pending"]),
                },
            )
        if path == "/ledger" and method == "GET":
            limit = int(params.get("limit", 50))
            if limit > 1000:
                # The real service caps a page at 1000 and rejects more. The
                # fake refuses it too, or a consumer could "pass" here by
                # asking for a window no substrate would ever serve.
                return FakeReply(422, {"detail": "limit must be <= 1000"})
            head = self.entries[-1]["sha256"] if self.entries else ""
            raw_offset = params.get("offset")
            if raw_offset is None or not self.ledger_paging:
                # No offset asked for — the historical tail. ``ledger_paging``
                # false additionally models an OLDER throughline that ignores
                # the parameter entirely, which the projection must detect by
                # the missing echo rather than trust.
                return FakeReply(
                    200,
                    {
                        "entries": self.entries[-limit:],
                        "chain_length": len(self.entries),
                        "head": head,
                    },
                )
            offset = int(raw_offset)
            page = self.entries[offset:offset + limit]
            next_offset = offset + len(page)
            more = next_offset < len(self.entries)
            return FakeReply(
                200,
                {
                    "entries": page,
                    "chain_length": len(self.entries),
                    "head": head,
                    "offset": offset,
                    "limit": limit,
                    "returned": len(page),
                    "total_entries": len(self.entries),
                    "has_more": more,
                    "next_offset": next_offset if more else None,
                },
            )
        if path == "/ledger/verify":
            return FakeReply(
                200,
                {
                    "valid": not self.tamper,
                    "chain_length": len(self.entries),
                    "broken_at": "seq 2" if self.tamper else None,
                },
            )
        if path == "/config":
            return FakeReply(
                200,
                {
                    "config": {
                        "effects": [
                            {"name": "route_permit", "reversibility": "reversible"},
                            {"name": "work_order", "reversibility": "reversible"},
                            {"name": "microgrid_dispatch", "reversibility": "irreversible"},
                        ]
                    },
                    "refusal": None,
                },
            )
        if path == "/signals" and method == "POST":
            entry = self.append("signal.ingested", dict(body))
            return FakeReply(202, {"seq": entry["seq"], "accepted": True})
        if path == "/effects" and method == "POST":
            return self._propose_effect(dict(body))
        if path.endswith("/walk"):
            effect_id = path.split("/")[2]
            hops = []
            for entry in self.entries:
                entry_body = entry["body"]
                if entry_body.get("id") == effect_id and entry["type"] == "effect.proposed":
                    hops.append(_hop("effect", entry))
                    signal_id = entry_body.get("signal_id")
                    for other in self.entries:
                        if other["body"].get("id") == signal_id:
                            hops.append(_hop("signal", other))
            return FakeReply(
                200,
                {
                    "effect_id": effect_id,
                    "hops": hops,
                    "hop_count": len(hops),
                    "complete": len(hops) >= 2,
                    "all_hops_verified": all(h["verified"] for h in hops),
                    "missing": [],
                },
            )
        if path.startswith("/approvals/") and path.endswith("/decide"):
            return self._decide(path.split("/")[2], body)
        return FakeReply(404, {"detail": f"no path {path}"})

    #: throughline's effect registry, as far as helm's tests need it. The
    #: classification is copied from throughline's config/effects.yaml, NOT
    #: from what would be convenient here: an unregistered type is
    #: irreversible (fail closed), and an irreversible effect is queued, never
    #: executed. Tests that assert warrant's behaviour therefore assert it
    #: against the same rules the real gate applies.
    REGISTRY = {
        "authz.dataset.claim": ("reversible", True),
        "authz.grant": ("reversible", True),
        "authz.grant.admin": ("irreversible", False),
        "authz.revoke": ("reversible", True),
        "authz.delegate": ("reversible", True),
        "authz.delegation.revoked": ("reversible", True),
        "authz.delegation.expired": ("reversible", True),
        "authz.refused": ("reversible", True),
        "authz.breakglass.requested": ("reversible", True),
        "authz.breakglass.vote": ("reversible", True),
        "authz.breakglass.entered": ("irreversible", False),
        "authz.breakglass.exited": ("reversible", True),
        "notify.operator": ("reversible", True),
        "payment.release": ("irreversible", False),
    }

    def _propose_effect(self, body: dict[str, Any]) -> FakeReply:
        effect_type = body.get("effect_type") or ""
        rule = self.REGISTRY.get(effect_type)
        if rule is not None:
            klass, auto = rule
            class_source = "registry"
        elif effect_type:
            klass, auto, class_source = "irreversible", False, "unregistered-fail-closed"
        else:
            claimed = body.get("reversibility") or "irreversible"
            klass = claimed
            auto = claimed == "reversible"
            class_source = "caller-claim"

        self._effect_seq = getattr(self, "_effect_seq", 0) + 1
        effect_id = body.get("id") or f"eff-{self._effect_seq:04d}"
        record = {
            "id": effect_id,
            "reversibility": klass,
            "reversibility_class": klass,
            "effect_type": effect_type,
            "class_source": class_source,
            "status": "proposed",
            "signal_id": body.get("signal_id"),
            "description": body.get("description"),
        }
        self.append("effect.proposed", dict(record))
        self.effects[effect_id] = record

        if klass == "reversible" and auto:
            record["status"] = "executed"
            self.append("effect.executing", dict(record))
            self.append("effect.executed", dict(record))
            return FakeReply(202, record)

        approval_id = f"apr-{effect_id}"
        record["status"] = "queued"
        record["approval_id"] = approval_id
        self.approvals[approval_id] = {
            "id": approval_id,
            "effect_id": effect_id,
            "effect_type": effect_type,
            "reversibility_class": klass,
            "description": body.get("description"),
            "signal_id": body.get("signal_id"),
            "state": "pending",
            "status": "pending",
            "queued_at": "2026-08-16T05:06:48Z",
            "decided_at": None,
            "decided_by": None,
            "rationale": None,
        }
        self.append("effect.queued", dict(record))
        return FakeReply(202, record)

    def _decide(self, approval_id: str, body: dict[str, Any]) -> FakeReply:
        record = self.approvals.get(approval_id)
        if record is None:
            return FakeReply(404, {"detail": "unknown approval"})
        if "decided_by" not in body or not body["decided_by"]:
            # The contract's own schema rule, reproduced verbatim.
            return FakeReply(
                422,
                {"detail": [{"type": "missing", "loc": ["body", "decided_by"]}]},
            )
        if record["state"] != "pending":
            return FakeReply(409, {"detail": "already decided"})
        if (
            body.get("caller_role") == "agent"
            and record["reversibility_class"] == "irreversible"
        ):
            self.append(
                "approval.refused",
                {
                    "approval_id": approval_id,
                    "effect_id": record["effect_id"],
                    "decided_by": body["decided_by"],
                    "issuer": body.get("issuer"),
                    "auth_mode": body.get("auth_mode") or "unattested",
                    "caller_role": "agent",
                    "reason": "an agent caller may not approve an irreversible effect",
                },
            )
            return FakeReply(
                403,
                {
                    "detail": (
                        "an agent caller may not approve an irreversible effect; only "
                        "an authenticated human subject may release it"
                    )
                },
            )
        record["state"] = body["decision"] + "d" if body["decision"] == "approve" else "rejected"
        record["state"] = "approved" if body["decision"] == "approve" else "rejected"
        record["status"] = record["state"]
        record["decided_by"] = body["decided_by"]
        record["issuer"] = body.get("issuer")
        record["auth_mode"] = body.get("auth_mode") or "unattested"
        record["decided_at"] = "2026-08-16T05:07:07Z"
        record["rationale"] = body.get("rationale")
        self.append(
            "approval.decided",
            {
                "approval_id": approval_id,
                "effect_id": record["effect_id"],
                "decided_by": body["decided_by"],
                # WHO, and WHICH AUTHORITY vouched for them — the substrate's
                # own rule, reproduced here so helm's tests assert against it.
                "issuer": body.get("issuer"),
                "auth_mode": body.get("auth_mode") or "unattested",
                "decision": body["decision"],
            },
        )
        if body["decision"] == "approve":
            # Echo the whole effect record, as throughline does: the executed
            # entry is what downstream projections read, so a thin one here
            # would let a test pass against a fake that is easier than the
            # contract.
            effect = dict(self.effects.get(record["effect_id"], {}))
            effect.update({"id": record["effect_id"], "effect_id": record["effect_id"],
                           "status": "executed"})
            self.append("effect.approved", dict(effect))
            self.append("effect.executing", dict(effect))
            self.append("effect.executed", effect)
        return FakeReply(200, {"approval": record, "effect": {"id": record["effect_id"]}})

    # ------------------------------------------------------------ siblings
    def _docket(self, method: str, path: str, body: dict, params: dict) -> FakeReply:
        if path == "/healthz":
            return FakeReply(
                200,
                {
                    "status": "ok",
                    "component": "docket",
                    "mode": {"judgment_source": "hosted", "judgment_model": "nemotron"},
                },
            )
        if path == "/permits":
            return FakeReply(
                200,
                {
                    "count": 1,
                    "total": 500,
                    "permits": [
                        {
                            "permitnum": "PN-2026-04471",
                            "description": "Land Use Application for a laboratory building.",
                            "permitclass": "Multifamily",
                            "permittypedesc": "Land Use",
                        }
                    ],
                },
            )
        return FakeReply(404, {"detail": "no path"})

    def _breaker(self, method: str, path: str, body: dict, params: dict) -> FakeReply:
        if path == "/healthz":
            return FakeReply(200, {"component": "breaker", "gate": {"mode": "real"}})
        if path == "/telemetry/series":
            return FakeReply(
                200,
                {
                    "series": [
                        {"soc": 60 - i * 2.5, "temp_c": 28 + i * 0.4} for i in range(9)
                    ],
                    "divergence_index": 23,
                },
            )
        if path == "/proposals":
            return FakeReply(200, {"proposals": []})
        return FakeReply(404, {"detail": "no path"})

    def _siren(self, method: str, path: str, body: dict, params: dict) -> FakeReply:
        if path == "/healthz":
            return FakeReply(200, {"component": "siren", "mode": "live"})
        if path == "/incidents":
            return FakeReply(
                200,
                {
                    "incidents": [
                        {"id": "INC-9931", "type": "fire", "status": "active"},
                        {"id": "INC-9932", "type": "traffic collision", "status": "active"},
                        {"id": "INC-9928", "type": "medical", "status": "closed"},
                    ]
                },
            )
        if path == "/feed/status":
            return FakeReply(200, {"label": "live · Seattle Fire 911", "as_of": "05:05Z"})
        if path == "/hot-reload/timeline":
            return FakeReply(
                200, {"steps": ["validated", "registered", "flowing"]}
            )
        if path == "/hot-reload":
            if self.hot_reload_body is not None:
                return FakeReply(self.hot_reload_status, dict(self.hot_reload_body))
            return FakeReply(
                200,
                {
                    "steps": [
                        {"step": "validated", "state": "ok", "detail": "7 rules"},
                        {"step": "registered", "state": "ok", "detail": "swapped"},
                        {"step": "flowing", "state": "ok", "detail": "emitted"},
                    ],
                    "emitted": int(body.get("emit", 0)),
                    "substrate": "real",
                },
            )
        return FakeReply(404, {"detail": "no path"})

    def _blindspot(self, method: str, path: str, body: dict, params: dict) -> FakeReply:
        return FakeReply(404, {"detail": "not built"})


def _hop(kind: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "seq": entry["seq"],
        "type": entry["type"],
        "ts": entry["ts"],
        "id": entry["body"].get("id"),
        "sha256": entry["sha256"],
        "hash_prefix": entry["sha256"][:8],
        "prev_hash": entry["prev_hash"],
        "verified": entry["verified"],
        "body": entry["body"],
    }


class _ConnectError(httpx.ConnectError):
    """An offline sibling must look exactly like an offline sibling."""
