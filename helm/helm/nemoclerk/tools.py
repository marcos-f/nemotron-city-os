"""The tool surface NemoClerk is allowed to touch.

Every tool here is the SAME documented API path the console uses. There is no
privileged path: what the agent can do, the console can do, and vice versa —
except approving an irreversible effect, which is refused by ROLE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from helm.clients import Federation
from helm.datasets import DESIGNED_NOT_BUILT, FederatedDatasets


@dataclass
class ToolResult:
    """One tool call and its result — this is what a tool CHIP renders."""

    name: str
    ok: bool
    data: Any = None
    error: str = ""
    refused: bool = False
    summary: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def chip(self) -> str:
        if self.refused:
            return f"tool: {self.name} → REFUSED"
        return f"tool: {self.name} {'✓' if self.ok else '✗'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "refused": self.refused,
            "chip": self.chip,
            "summary": self.summary,
            "error": self.error,
            "arguments": self.arguments,
            "data": self.data,
        }


class RefusedByRole(Exception):
    """Raised when a principal's ROLE forbids the effect it asked for."""

    def __init__(self, message: str, principal: str, tool: str) -> None:
        super().__init__(message)
        self.principal = principal
        self.tool = tool


READ_TOOLS = (
    "get_overview",
    "list_approvals",
    "read_ledger",
    "verify_ledger",
    "walk_effect",
    "list_feeds",
    "get_composed",
    "get_permit",
    "list_incidents",
    "get_config",
    "list_roles",
    # Dataset discovery is a READ. It enumerates what data the federation is
    # standing on and where each dataset came from. There is deliberately no
    # tool to CHANGE a dataset's source or mode: that is a config edit,
    # refused or accepted by the loader, and an agent does not get a side door.
    "list_datasets",
    "get_dataset",
)
RELOAD_TOOLS = ("reload_signal_class",)
EFFECT_TOOLS = ("approve_effect",)


class ToolRegistry:
    """Bind the tool names in ``mcp/tools.json`` to real substrate calls."""

    def __init__(self, federation: Federation, console: Any,
                 datasets: "FederatedDatasets | None" = None) -> None:
        self.federation = federation
        self.console = console
        self._datasets = datasets

    # ------------------------------------------------------------- read
    def get_overview(self, **_: Any) -> ToolResult:
        data = self.console.overview()
        return ToolResult(
            name="get_overview",
            ok=True,
            data=data,
            summary=(
                f"{data['pending_approvals']} approval(s) waiting; "
                f"{len([f for f in data['feeds'] if f['online']])} of "
                f"{len(data['feeds'])} feeds online; ledger "
                f"{'valid' if data['ledger_valid'] else 'INVALID'} over "
                f"{data['chain_length']} entries"
            ),
        )

    def list_approvals(self, state: str | None = "pending", **_: Any) -> ToolResult:
        reply = self.federation.throughline.approvals(state)
        if not reply.ok:
            return ToolResult(
                name="list_approvals",
                ok=False,
                error=reply.error or "throughline unreachable",
                summary="throughline unreachable — no approval data",
                arguments={"state": state},
            )
        approvals = reply.data.get("approvals", [])
        descriptions = "; ".join(
            f"{a['id']} {a.get('description') or a.get('effect_id')} "
            f"({a.get('reversibility_class')})"
            for a in approvals[:5]
        )
        return ToolResult(
            name="list_approvals",
            ok=True,
            data=approvals,
            arguments={"state": state},
            summary=f"{len(approvals)} approval(s): {descriptions or 'none'}",
        )

    def read_ledger(self, limit: int = 10, **_: Any) -> ToolResult:
        reply = self.federation.throughline.ledger(limit=int(limit))
        if not reply.ok:
            return ToolResult(
                name="read_ledger",
                ok=False,
                error=reply.error or "throughline unreachable",
                summary="throughline unreachable — no ledger data",
                arguments={"limit": limit},
            )
        entries = reply.data.get("entries", [])
        tail = "; ".join(
            f"seq {e['seq']} {e['type']} {e['sha256'][:8]}" for e in entries[-5:]
        )
        return ToolResult(
            name="read_ledger",
            ok=True,
            data=entries,
            arguments={"limit": limit},
            summary=(
                f"chain length {reply.data.get('chain_length')}, newest: {tail}"
            ),
        )

    def verify_ledger(self, **_: Any) -> ToolResult:
        reply = self.federation.throughline.verify()
        if not reply.ok:
            return ToolResult(
                name="verify_ledger",
                ok=False,
                error=reply.error or "throughline unreachable",
                summary="throughline unreachable — cannot verify",
            )
        data = reply.data
        return ToolResult(
            name="verify_ledger",
            ok=True,
            data=data,
            summary=(
                f"ledger {'valid' if data.get('valid') else 'INVALID'} over "
                f"{data.get('chain_length')} entries"
            ),
        )

    def walk_effect(self, effect_id: str = "", **_: Any) -> ToolResult:
        if not effect_id:
            newest = self.console.newest_effect_id()
            effect_id = newest or ""
        if not effect_id:
            return ToolResult(
                name="walk_effect",
                ok=False,
                error="no effect to walk",
                summary="no effect in the ledger to walk",
            )
        reply = self.federation.throughline.walk(effect_id)
        if not reply.ok:
            return ToolResult(
                name="walk_effect",
                ok=False,
                error=reply.error or "throughline unreachable",
                summary=f"could not walk {effect_id}",
                arguments={"effect_id": effect_id},
            )
        data = reply.data
        hops = data.get("hops", [])
        return ToolResult(
            name="walk_effect",
            ok=True,
            data=data,
            arguments={"effect_id": effect_id},
            summary=(
                f"{data.get('hop_count')} hop(s) for {effect_id}: "
                + " → ".join(f"{h['kind']} {h['hash_prefix']}" for h in hops)
                + (
                    " — all hops hash-verified"
                    if all(h.get("verified") for h in hops)
                    else " — A HOP FAILED VERIFICATION"
                )
            ),
        )

    def list_feeds(self, **_: Any) -> ToolResult:
        feeds = self.console.feeds()
        live = [f["name"] for f in feeds if f["online"]]
        offline = [f["name"] for f in feeds if not f["online"]]
        return ToolResult(
            name="list_feeds",
            ok=True,
            data=feeds,
            summary=(
                f"online: {', '.join(live) or 'none'}"
                + (f"; offline: {', '.join(offline)}" if offline else "")
            ),
        )

    def get_composed(self, **_: Any) -> ToolResult:
        data = self.console.composed()
        return ToolResult(
            name="get_composed",
            ok=True,
            data=data,
            summary=(
                "composed view assembled: gate-hold present"
                if data.get("assembled")
                else "composed view not assembled — no gate-hold"
            ),
        )

    def get_permit(self, permitnum: str = "", **_: Any) -> ToolResult:
        """docket's read surface: the permit, and the text a judgment quotes."""
        path = f"/permits/{permitnum}" if permitnum else "/permits"
        params = None if permitnum else {"limit": 1}
        reply = self.federation.docket.get(path, params=params)
        if not reply.ok:
            return ToolResult(
                name="get_permit",
                ok=False,
                error=reply.error or "docket unreachable",
                summary="docket is not serving — no permit to quote",
                arguments={"permitnum": permitnum},
            )
        data = reply.data
        permit = data
        if isinstance(data, dict) and data.get("permits"):
            permit = data["permits"][0]
        if not isinstance(permit, dict):
            return ToolResult(
                name="get_permit", ok=False, error="unexpected shape",
                summary="docket returned no permit",
            )
        return ToolResult(
            name="get_permit",
            ok=True,
            data=permit,
            arguments={"permitnum": permit.get("permitnum", "")},
            summary=(
                f"{permit.get('permitnum', '?')} ({permit.get('permitclass', '?')}, "
                f"{permit.get('permittypedesc', '?')}): "
                f"{str(permit.get('description', ''))[:160]}"
            ),
        )

    def list_incidents(self, limit: int = 5, **_: Any) -> ToolResult:
        """siren's read surface: the live 911 feed."""
        reply = self.federation.siren.get("/incidents", params={"limit": int(limit)})
        if not reply.ok:
            return ToolResult(
                name="list_incidents",
                ok=False,
                error=reply.error or "siren unreachable",
                summary="siren is not serving — no incident data",
            )
        rows = reply.data if isinstance(reply.data, list) else (
            reply.data.get("incidents", []) if isinstance(reply.data, dict) else []
        )
        described = "; ".join(
            f"{r.get('id')} {r.get('incident_type') or r.get('type')}"
            for r in rows[:4]
            if isinstance(r, dict)
        )
        return ToolResult(
            name="list_incidents",
            ok=True,
            data=rows,
            arguments={"limit": limit},
            summary=f"{len(rows)} incident(s): {described or 'none'}",
        )

    def get_config(self, **_: Any) -> ToolResult:
        """The running effect registry, and any standing config refusal."""
        reply = self.federation.throughline.config()
        if not reply.ok:
            return ToolResult(
                name="get_config",
                ok=False,
                error=reply.error or "throughline unreachable",
                summary="throughline unreachable — cannot read the config",
            )
        data = reply.data if isinstance(reply.data, dict) else {}
        refusal = data.get("refusal")
        config = data.get("config") or {}
        rules = config.get("effects") or config.get("rules") or []
        return ToolResult(
            name="get_config",
            ok=True,
            data=data,
            summary=(
                f"{len(rules)} effect rule(s) loaded; "
                + (
                    f"a config is REFUSED: {refusal.get('rule')} in {refusal.get('file')}"
                    if refusal
                    else "no standing refusal"
                )
            ),
        )

    def list_roles(self, **_: Any) -> ToolResult:
        """signet's role table — who may approve, and who may never."""
        rows = self.console.signet.role_table()
        approvers = [r["subject"] for r in rows if r["role"] in {"admin", "operator"}]
        return ToolResult(
            name="list_roles",
            ok=True,
            data=rows,
            summary=(
                f"{len(rows)} subject(s); may approve: "
                f"{', '.join(approvers) or 'none'}"
            ),
        )

    # ----------------------------------------------------------- reload
    def reload_signal_class(self, name: str = "incident", **_: Any) -> ToolResult:
        """Reload a class, and report which of THREE things happened.

        accepted / refused / HELD. The tool used to report the first for all
        three, so NemoClerk would tell an operator a class was flowing while
        the swap sat at the gate waiting for that same operator.
        """
        outcome = self.console.reload_class(name)
        return ToolResult(
            name="reload_signal_class",
            ok=outcome.performed,
            data=outcome.to_dict(),
            arguments={"name": name},
            error="" if outcome.performed else outcome.detail,
            summary=outcome.summary,
        )

    # ----------------------------------------------------------- effect
    def approve_effect(
        self,
        id: str = "",  # noqa: A002 - the MCP tool's declared argument name
        decision: str = "approve",
        *,
        principal: str = "",
        role: str = "agent",
        approver: str = "",
        authenticated: bool = False,
        **_: Any,
    ) -> ToolResult:
        """The one thing the agent cannot do.

        The refusal is enforced here by ROLE and then ledgered by the
        substrate, carrying the principal into the chain.
        """
        if not id:
            # "approve it" with no id means the thing that is waiting.
            pending = self.list_approvals(state="pending").data or []
            irreversible = [
                a for a in pending if a.get("reversibility_class") == "irreversible"
            ]
            chosen = irreversible or pending
            id = chosen[0]["id"] if chosen else ""  # noqa: A001
        # The approver is what helm derived, never a tool argument. An MCP
        # caller cannot name its own approver any more than an HTTP one can.
        outcome = self.console.decide(
            approval_id=id,
            decision=decision,
            approver=approver or (principal if role == "agent" else ""),
            role=role,
            principal=principal or approver,
            # Authenticity comes from the session. An agent role is the least
            # authenticated caller there is and must not manufacture the flag.
            authenticated=authenticated,
        )
        if outcome.get("refused"):
            # The principal named here is the one the refusal was RECORDED
            # under, so it is read back out of the outcome rather than
            # re-derived. (This f-string used to reference an undefined
            # `decided_by`: a NameError one empty principal away, on the
            # refusal path the demo closes with.)
            ledgered_as = principal or str(outcome.get("principal") or "") or "anonymous"
            return ToolResult(
                name="approve_effect",
                ok=False,
                refused=True,
                data=outcome,
                arguments={"id": id, "decision": decision},
                error=outcome.get("reason", "refused"),
                summary=(
                    f"REFUSED · ledgered as {ledgered_as} · "
                    + outcome.get("reason", "")
                ),
            )
        return ToolResult(
            name="approve_effect",
            ok=bool(outcome.get("ok")),
            data=outcome,
            arguments={"id": id, "decision": decision},
            error=outcome.get("reason", "") if not outcome.get("ok") else "",
            summary=outcome.get("summary", "decision recorded"),
        )

    # ---------------------------------------------------- dataset discovery
    @property
    def datasets(self) -> "FederatedDatasets":
        # Built lazily so constructing a ToolRegistry stays free of file IO.
        if self._datasets is None:
            from helm.datasets import FederatedDatasets

            self._datasets = FederatedDatasets(self.federation)
        return self._datasets

    def list_datasets(self, component: str | None = None, **_: Any) -> ToolResult:
        summary = self.datasets.summary()
        blocks = summary["components"]
        if component:
            blocks = [b for b in blocks if b["component"] == component]
            if not blocks:
                return ToolResult(
                    name="list_datasets", ok=False,
                    arguments={"component": component},
                    error=f"unknown component {component!r}",
                    summary=f"unknown component {component!r}")
            summary = {**summary, "components": blocks}

        unknown = summary.get("components_unknown") or []
        parts = [f"{summary['total']} dataset(s) declared across "
                 f"{len(blocks)} component(s)"]
        if summary["unavailable"]:
            parts.append(f"{summary['unavailable']} unavailable "
                         "(declared, not built)")
        if summary["licence_unknown"]:
            parts.append(f"{summary['licence_unknown']} with licence unknown")
        if unknown:
            # Not "zero datasets" — we genuinely do not know.
            parts.append("no answer and no declaration from "
                         + ", ".join(unknown))
        return ToolResult(
            name="list_datasets", ok=True, data=summary,
            arguments={"component": component} if component else {},
            summary="; ".join(parts))

    def get_dataset(self, id: str, **_: Any) -> ToolResult:
        lookup = self.datasets.locate(id)
        entry = lookup.entry
        if entry is None:
            # "No such dataset" and "a registry went unread" are different
            # facts. The summary is the sentence the model repeats to a
            # person, so it must not assert the first when it means the second.
            return ToolResult(
                name="get_dataset", ok=False, arguments={"id": id},
                data=lookup.as_dict(),
                error=(f"unknown dataset {id!r}" if lookup.known
                       else f"cannot tell whether {id!r} exists"),
                summary=lookup.message_ui)
        bits = [f"{entry['id']} ({entry.get('mode')})",
                f"licence {entry.get('licence')}",
                f"provenance: {entry.get('provenance')}"]
        if entry.get("as_of"):
            bits.append(f"as of {entry['as_of']}")
        if entry.get("availability") == "unavailable":
            bits.append(DESIGNED_NOT_BUILT)
        return ToolResult(
            name="get_dataset", ok=True, data=entry,
            arguments={"id": id}, summary="; ".join(bits))

    # --------------------------------------------------------- dispatch
    @property
    def dispatch(self) -> dict[str, Callable[..., ToolResult]]:
        return {
            "get_overview": self.get_overview,
            "list_approvals": self.list_approvals,
            "read_ledger": self.read_ledger,
            "verify_ledger": self.verify_ledger,
            "walk_effect": self.walk_effect,
            "list_feeds": self.list_feeds,
            "get_composed": self.get_composed,
            "get_permit": self.get_permit,
            "list_incidents": self.list_incidents,
            "get_config": self.get_config,
            "list_roles": self.list_roles,
            "list_datasets": self.list_datasets,
            "get_dataset": self.get_dataset,
            "reload_signal_class": self.reload_signal_class,
            "approve_effect": self.approve_effect,
        }

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        fn = self.dispatch.get(name)
        if fn is None:
            return ToolResult(
                name=name, ok=False, error=f"unknown tool {name!r}", summary="unknown tool"
            )
        return fn(**(arguments or {}))

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-compatible tool schemas for the tool-calling loop."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_overview",
                    "description": "Federation overview: feed health, queue depth, ledger status.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_approvals",
                    "description": "List approvals waiting at the gate.",
                    "parameters": {
                        "type": "object",
                        "properties": {"state": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_ledger",
                    "description": "Read the tail of the provenance ledger.",
                    "parameters": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "verify_ledger",
                    "description": "Recompute and verify the whole hash chain.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "walk_effect",
                    "description": "Walk an effect back to its cause: effect → judgment → signal.",
                    "parameters": {
                        "type": "object",
                        "properties": {"effect_id": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_feeds",
                    "description": "The signal feeds and which of them are live.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_composed",
                    "description": "The composed state, assembled when an effect is held at the gate.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_permit",
                    "description": (
                        "Get a permit from docket, including the exact text a "
                        "judgment quotes. Omit permitnum for the newest."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"permitnum": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_incidents",
                    "description": "List live 911 incidents from siren.",
                    "parameters": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_config",
                    "description": (
                        "The running effect registry and any standing config "
                        "refusal, naming file, rule and line."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_roles",
                    "description": (
                        "The signet role table: who may approve an irreversible "
                        "effect, and who never may."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_datasets",
                    "description": (
                        "Enumerate every dataset the federation consumes, with "
                        "licence, provenance, mode (live/cached/fixture/"
                        "declared-unavailable) and as-of time. A component that "
                        "is not serving is reported offline with its declared "
                        "entries, never as zero. READ ONLY: there is no tool to "
                        "change a dataset's source or mode."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"component": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_dataset",
                    "description": (
                        "Inspect one dataset: where it comes from, its licence, "
                        "how it was obtained, and whether it is live, cached, a "
                        "fixture, or declared but not built."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "reload_signal_class",
                    "description": "Hot-reload a signal class without a redeploy.",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "approve_effect",
                    "description": (
                        "Decide a pending approval. An agent principal is REFUSED "
                        "on an irreversible effect."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "decision": {"type": "string"},
                        },
                    },
                },
            },
        ]
