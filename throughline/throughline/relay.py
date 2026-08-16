"""NeMo Relay 0.7.3 gate mirror.

Effects execute as Relay tool calls. A conditional-execution guardrail
registered via ``nemo_relay.guardrails.register_tool_conditional_execution``
runs *before* the tool callback, and its decision —
``{allowed, rejected, rejection_reason}`` — is mirrored into our ledger as
corroborating evidence.

Honesty copy, stated once and enforced in the code below: Relay ships
lineage, not integrity. The hash chain in ``ledger.py`` is ours; the Relay
record is corroboration, never the source of truth. When the ``nemo_relay``
package is unavailable this module runs in ``mock`` mode, and every mirrored
record carries ``mode: "mock"`` so no screen can claim Relay where none ran.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Optional

GUARDRAIL_NAME = "throughline.gate"
GUARDRAIL_PRIORITY = 100

Decider = Callable[[str, dict[str, Any]], Optional[str]]

try:  # pragma: no cover - import shape depends on the environment
    import nemo_relay
    from nemo_relay.guardrails import (
        deregister_tool_conditional_execution,
        register_tool_conditional_execution,
    )

    RELAY_AVAILABLE = True
    RELAY_VERSION = getattr(nemo_relay, "__version__", "0.7.3")
except Exception:  # pragma: no cover - exercised only where relay is absent
    nemo_relay = None
    RELAY_AVAILABLE = False
    RELAY_VERSION = None


def _default_decider(tool_name: str, args: dict[str, Any]) -> Optional[str]:
    """Reject any irreversible effect that is not already human-approved."""
    if args.get("reversibility_class") == "irreversible" and not args.get("approved"):
        return (
            "irreversible effect held for human approval "
            f"(effect={args.get('effect_id')})"
        )
    return None


class RelayGateMirror:
    """Runs effects through Relay's tool pipeline and mirrors the verdict."""

    def __init__(self, ledger=None, decider: Optional[Decider] = None, name: str = GUARDRAIL_NAME):
        self.ledger = ledger
        self.decider = decider or _default_decider
        self.name = name
        self.mode = "nemo-relay" if RELAY_AVAILABLE else "mock"
        self.version = RELAY_VERSION
        self._registered = False

    # ------------------------------------------------------------- lifecycle

    def register(self) -> None:
        """Register the guardrail, replacing any same-named predecessor.

        Relay's registry rejects a duplicate name outright, which would crash
        the effect path for the second substrate built in one process. Replace
        rather than fail: the newest substrate owns the guardrail.
        """
        if not RELAY_AVAILABLE or self._registered:
            return
        try:
            register_tool_conditional_execution(self.name, GUARDRAIL_PRIORITY, self.decider)
        except RuntimeError:
            deregister_tool_conditional_execution(self.name)
            register_tool_conditional_execution(self.name, GUARDRAIL_PRIORITY, self.decider)
        self._registered = True

    def deregister(self) -> None:
        if RELAY_AVAILABLE and self._registered:
            deregister_tool_conditional_execution(self.name)
            self._registered = False

    # --------------------------------------------------------------- execute

    async def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        func: Callable[[dict[str, Any]], Any | Awaitable[Any]],
    ) -> tuple[Optional[Any], dict[str, Any]]:
        """Run ``func`` through the guardrail chain.

        Returns ``(result, record)``. ``result`` is ``None`` when the
        guardrail rejected the call. ``record`` is the mirrored guardrail
        decision, already appended to the ledger.
        """
        self.register()
        result: Optional[Any] = None
        rejection: Optional[str] = None

        if RELAY_AVAILABLE:
            try:
                pending = nemo_relay.tools.execute(tool_name, args, func)
                result = await pending if inspect.isawaitable(pending) else pending
            except RuntimeError as exc:
                rejection = str(exc)
        else:
            rejection = self.decider(tool_name, args)
            if rejection is None:
                pending = func(args)
                result = await pending if inspect.isawaitable(pending) else pending

        record = {
            "tool": tool_name,
            "allowed": rejection is None,
            "rejected": rejection is not None,
            "rejection_reason": rejection,
            "mode": self.mode,
            "relay_version": self.version,
            "guardrail": self.name,
            "effect_id": args.get("effect_id"),
            "reversibility_class": args.get("reversibility_class"),
        }
        if self.ledger is not None:
            self.ledger.append("relay.guardrail", record)
        return result, record

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "relay_version": self.version,
            "guardrail": self.name,
            "registered": self._registered,
            "note": (
                "Relay ships lineage, not integrity; the hash chain is ours."
                if RELAY_AVAILABLE
                else "MOCK: nemo-relay is not installed; no Relay guardrail ran."
            ),
        }
