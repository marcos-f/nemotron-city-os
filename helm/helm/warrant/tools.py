"""warrant's MCP tools — the same answers, over the third surface.

``get_dataset_authority``, ``get_dataset_breakglass`` and
``list_authority_rules`` are READS. They call the projection, which has no
writer behind it: an agent asking who can do what cannot change who can do
what.

Everything else is an effect tool. Two things stop an agent quietly acquiring
authority, and only one of them is ours:

* warrant refuses any grant from a subject that is not an administrator, and
  names the rule;
* and break-glass entry is irreversible, so throughline holds it for a human
  and refuses an agent caller under ``agent-may-not-approve-irreversible``.
  That refusal was already there. We did not add a rule for it.
"""

from __future__ import annotations

from typing import Any, Callable

from helm.nemoclerk.tools import ToolRegistry, ToolResult
from helm.warrant.model import (
    BREAKGLASS_DEFAULT_MINUTES,
    ROLES,
    Refused,
    Unknown,
    unknown_reason_text,
    utcnow,
)
from helm.warrant.service import Actor, Warrant

#: Naming is load-bearing here, not cosmetic. The registry's invariant is
#: that nothing named ``dataset_*`` may be anything but a read, so that
#: discovery can never become a side door into a dataset's configuration.
#: warrant's effect tools act on AUTHORITY, not on dataset configuration, and
#: they are named for what they touch — which keeps that invariant true in
#: substance as well as in string form.
WARRANT_READ_TOOLS = ("get_dataset_authority", "get_dataset_breakglass", "list_authority_rules")
WARRANT_EFFECT_TOOLS = (
    "authority_claim",
    "authority_grant",
    "authority_revoke",
    "authority_delegate",
    "breakglass_request",
    "breakglass_vote",
    "breakglass_exit",
)


def _actor(principal: str, warrant: Warrant) -> Actor:
    """An MCP principal is an agent subject unless helm says otherwise.

    The subject is carried verbatim into the effect, so throughline's own
    ``agent:``/``bot:``/``svc:`` prefix check still applies at the gate. We do
    not launder a principal into a human on the way through.
    """
    return Actor(
        subject=principal or "agent:nemoclerk",
        auth_mode="agent",
        global_role=warrant.global_role_for(principal),
    )


def _refusal(name: str, exc: Refused, arguments: dict[str, Any]) -> ToolResult:
    kind = "UNKNOWN" if isinstance(exc, Unknown) else "REFUSED"
    return ToolResult(
        name=name,
        ok=False,
        refused=not isinstance(exc, Unknown),
        error=str(exc),
        data=exc.as_dict(),
        summary=(
            f"{kind} — {exc.rule}: {exc.detail or exc.reason}"
        ),
        arguments=arguments,
    )


class WarrantToolRegistry(ToolRegistry):
    """The host registry plus warrant's tools. Additive: nothing is removed."""

    def __init__(self, federation: Any, console: Any, warrant: Warrant | None = None) -> None:
        super().__init__(federation, console)
        self.warrant = warrant or Warrant(federation, getattr(console, "signet", None))

    # ------------------------------------------------------------------ read
    def get_dataset_authority(self, dataset_id: str = "", subject: str = "",
                              **_: Any) -> ToolResult:
        """Who may do what to a dataset, and who gave them the power to."""
        args = {"dataset_id": dataset_id, "subject": subject}
        if not dataset_id:
            return ToolResult(name="get_dataset_authority", ok=False,
                              error="dataset_id is required",
                              summary="dataset_id is required", arguments=args)
        snap = self.warrant.snapshot()
        authority = snap.authority(dataset_id)
        now = utcnow()
        data = authority.to_dict(now)
        if not authority.known:
            # Not a denial and not an empty list. The tool says so in the
            # summary too, because the summary is what the model repeats.
            return ToolResult(
                name="get_dataset_authority", ok=False, data=data,
                error=data.get("detail", ""),
                summary=(
                    f"UNKNOWN — who may do what to {dataset_id} cannot be determined "
                    f"({data.get('reason')}). This is not a statement that nobody "
                    "has access."
                ),
                arguments=args,
            )
        if subject:
            held = authority.capability(subject, now)
            data["decision"] = {"subject": subject,
                                "decision": "allow" if held else "deny", "held": held}
            summary = (
                f"{subject} holds {held['role']} on {dataset_id} by {held['source']}"
                if held else f"{subject} holds nothing on {dataset_id}"
            )
        elif not authority.claimed:
            summary = f"{dataset_id} has no administrator: nobody has claimed it"
        else:
            owner = authority.owner
            summary = (
                f"{dataset_id}: {len(data['people'])} person(s), "
                f"{data['admin_count']} direct administrator(s)"
                + (f", onboarded by {owner.subject}" if owner else "")
                + (f", {len(data['breakglass_active'])} BREAK-GLASS WINDOW OPEN"
                   if data["breakglass_active"] else "")
            )
        return ToolResult(name="get_dataset_authority", ok=True, data=data,
                          summary=summary, arguments=args)

    def get_dataset_breakglass(self, dataset_id: str = "", **_: Any) -> ToolResult:
        """Break-glass windows on a dataset, active and historical."""
        args = {"dataset_id": dataset_id}
        snap = self.warrant.snapshot()
        authority = snap.authority(dataset_id)
        if not authority.known:
            # Names THIS cause. "Cannot reach the substrate" was printed even
            # when the substrate had answered and only the read fell short —
            # and the summary is the sentence a model repeats to a person.
            return ToolResult(name="get_dataset_breakglass", ok=False,
                              data=authority.to_dict(),
                              summary=(
                                  f"UNKNOWN — whether a window is open on "
                                  f"{dataset_id} cannot be determined: "
                                  f"{unknown_reason_text(authority.reason)}"
                              ),
                              arguments=args)
        now = utcnow()
        windows = [w.to_dict(now) for w in authority.breakglass.values()]
        active = [w for w in windows if w["open"]]
        return ToolResult(
            name="get_dataset_breakglass", ok=True,
            data={"dataset_id": dataset_id, "windows": windows, "active": active,
                  "eligible_voters": sorted(self.warrant.eligible_voters(dataset_id, snap))},
            summary=(f"{len(active)} BREAK-GLASS WINDOW(S) OPEN on {dataset_id}"
                     if active else f"no break-glass window is open on {dataset_id}"),
            arguments=args,
        )

    def list_authority_rules(self, **_: Any) -> ToolResult:
        """Every refusal warrant can name, so an agent can quote the real one."""
        from helm.warrant.model import RULES

        rows = [{"rule": rule, "reason": reason} for rule, reason in sorted(RULES.items())]
        return ToolResult(name="list_authority_rules", ok=True, data=rows,
                          summary=f"{len(rows)} named refusal rules; roles are "
                                  f"{', '.join(reversed(ROLES))}")

    # ---------------------------------------------------------------- effect
    def _effect(self, name: str, fn: Callable[[Actor], dict[str, Any]],
                arguments: dict[str, Any], principal: str) -> ToolResult:
        try:
            data = fn(_actor(principal, self.warrant))
        except Refused as exc:
            return _refusal(name, exc, arguments)
        return ToolResult(name=name, ok=True, data=data,
                          summary=data.get("summary") or f"{name} ok",
                          arguments=arguments)

    def authority_claim(self, dataset_id: str = "", principal: str = "", **_: Any) -> ToolResult:
        args = {"dataset_id": dataset_id}
        return self._effect("authority_claim",
                            lambda actor: {**self.warrant.claim(dataset_id, actor),
                                           "summary": f"{principal or 'the caller'} is now the "
                                                      f"first administrator of {dataset_id}"},
                            args, principal)

    def authority_grant(self, dataset_id: str = "", subject: str = "", role: str = "reader",
                      principal: str = "", **_: Any) -> ToolResult:
        args = {"dataset_id": dataset_id, "subject": subject, "role": role}
        return self._effect("authority_grant",
                            lambda actor: {**self.warrant.grant(dataset_id, subject, role, actor),
                                           "summary": f"{subject} now holds {role} on {dataset_id}"},
                            args, principal)

    def authority_revoke(self, dataset_id: str = "", subject: str = "", rationale: str = "",
                       principal: str = "", **_: Any) -> ToolResult:
        args = {"dataset_id": dataset_id, "subject": subject}
        return self._effect("authority_revoke",
                            lambda actor: {**self.warrant.revoke(dataset_id, subject, actor,
                                                                 rationale),
                                           "summary": f"{subject} no longer holds a role on "
                                                      f"{dataset_id}"},
                            args, principal)

    def authority_delegate(self, dataset_id: str = "", subject: str = "", role: str = "admin",
                         expires_at: str = "", principal: str = "", **_: Any) -> ToolResult:
        args = {"dataset_id": dataset_id, "subject": subject, "role": role,
                "expires_at": expires_at}
        return self._effect("authority_delegate",
                            lambda actor: {**self.warrant.delegate(dataset_id, subject, role,
                                                                   expires_at, actor),
                                           "summary": f"{subject} holds {role} on {dataset_id} "
                                                      f"until {expires_at} — delegated, expiring"},
                            args, principal)

    def breakglass_request(self, dataset_id: str = "", reason: str = "",
                           window_minutes: int = BREAKGLASS_DEFAULT_MINUTES,
                           principal: str = "", **_: Any) -> ToolResult:
        args = {"dataset_id": dataset_id, "reason": reason, "window_minutes": window_minutes}
        return self._effect("breakglass_request",
                            lambda actor: {**self.warrant.request_breakglass(
                                dataset_id, reason, actor, window_minutes),
                                "summary": f"break-glass requested on {dataset_id}; it needs a "
                                           f"quorum of other datasets' administrators"},
                            args, principal)

    def breakglass_vote(self, dataset_id: str = "", request_id: str = "",
                        principal: str = "", **_: Any) -> ToolResult:
        args = {"dataset_id": dataset_id, "request_id": request_id}
        return self._effect("breakglass_vote",
                            lambda actor: {**self.warrant.vote_breakglass(dataset_id, request_id,
                                                                          actor),
                                           "summary": f"vote recorded on {request_id}"},
                            args, principal)

    def breakglass_exit(self, dataset_id: str = "", request_id: str = "",
                        reason: str = "closed by hand", principal: str = "",
                        **_: Any) -> ToolResult:
        args = {"dataset_id": dataset_id, "request_id": request_id, "reason": reason}
        return self._effect("breakglass_exit",
                            lambda actor: {**self.warrant.exit_breakglass(dataset_id, request_id,
                                                                          actor, reason),
                                           "summary": f"break-glass {request_id} closed"},
                            args, principal)

    # -------------------------------------------------------------- registry
    @property
    def dispatch(self) -> dict[str, Callable[..., ToolResult]]:
        return {
            **super().dispatch,
            "get_dataset_authority": self.get_dataset_authority,
            "get_dataset_breakglass": self.get_dataset_breakglass,
            "list_authority_rules": self.list_authority_rules,
            "authority_claim": self.authority_claim,
            "authority_grant": self.authority_grant,
            "authority_revoke": self.authority_revoke,
            "authority_delegate": self.authority_delegate,
            "breakglass_request": self.breakglass_request,
            "breakglass_vote": self.breakglass_vote,
            "breakglass_exit": self.breakglass_exit,
        }

    def schemas(self) -> list[dict[str, Any]]:
        dataset = {"type": "string", "description": "dataset id"}
        subject = {"type": "string", "description": "OIDC subject"}
        return super().schemas() + [
            _fn("get_dataset_authority",
                "Who may do what to a dataset, and who gave them the power to. A READ: "
                "it cannot change anything. Returns UNKNOWN, never an empty list, when "
                "the chain cannot be read; the reason names which failure it was.",
                {"dataset_id": dataset, "subject": subject}, ["dataset_id"]),
            _fn("get_dataset_breakglass",
                "Break-glass windows on a dataset, active and historical. A READ.",
                {"dataset_id": dataset}, ["dataset_id"]),
            _fn("list_authority_rules",
                "Every refusal warrant can name, and what each one means.", {}, []),
            _fn("authority_claim",
                "Onboard a dataset: the calling subject becomes its first administrator. "
                "Refused if it already has one.",
                {"dataset_id": dataset}, ["dataset_id"]),
            _fn("authority_grant",
                "Grant a role on a dataset. Administrators only; a non-administrator is "
                "refused by name and the refusal is ledgered. A grant can also REDUCE "
                "authority — granting an existing administrator a lesser role is a "
                "downgrade — so it is refused by WARRANT-SELF-REVOKE-LAST or "
                "WARRANT-LAST-ADMIN if it would leave the dataset with no direct "
                "administrator.",
                {"dataset_id": dataset, "subject": subject,
                 "role": {"type": "string", "enum": list(ROLES)}},
                ["dataset_id", "subject", "role"]),
            _fn("authority_revoke",
                "Withdraw a role. Refused by WARRANT-LAST-ADMIN if it would leave the "
                "dataset with no administrator.",
                {"dataset_id": dataset, "subject": subject,
                 "rationale": {"type": "string"}}, ["dataset_id", "subject"]),
            _fn("authority_delegate",
                "Lend administrative authority with a MANDATORY expiry. A delegate may "
                "not delegate onward and does not satisfy the last-administrator guard.",
                {"dataset_id": dataset, "subject": subject,
                 "role": {"type": "string", "enum": list(ROLES)},
                 "expires_at": {"type": "string", "description": "ISO-8601, required"}},
                ["dataset_id", "subject", "expires_at"]),
            _fn("breakglass_request",
                "Ask a quorum of OTHER datasets' administrators to open a time-boxed "
                "window on a dataset whose administrators are unreachable.",
                {"dataset_id": dataset, "reason": {"type": "string"},
                 "window_minutes": {"type": "integer"}}, ["dataset_id", "reason"]),
            _fn("breakglass_vote",
                "Vote to open a break-glass window. The final vote releases an "
                "IRREVERSIBLE effect through throughline's gate, which refuses an agent "
                "caller — an agent cannot break the glass.",
                {"dataset_id": dataset, "request_id": {"type": "string"}},
                ["dataset_id", "request_id"]),
            _fn("breakglass_exit",
                "Close a break-glass window. Never blocked, always ledgered.",
                {"dataset_id": dataset, "request_id": {"type": "string"},
                 "reason": {"type": "string"}}, ["dataset_id", "request_id"]),
        ]


def _fn(name: str, description: str, properties: dict[str, Any],
        required: list[str]) -> dict[str, Any]:
    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    return {"type": "function",
            "function": {"name": name, "description": description, "parameters": parameters}}


__all__ = ["WarrantToolRegistry", "WARRANT_READ_TOOLS", "WARRANT_EFFECT_TOOLS"]
