"""Composition root: settings, substrate selection, the object graph.

Substrate selection is the mock→real switch the federation's conventions ask
for. In ``auto`` (the default) breaker probes throughline once at boot: if the
real substrate answers, breaker uses it; if it does not, breaker runs against
the mock and **says so** in /healthz and in every proposal it produces. It
never silently claims a real gate.

Boot-time selection is not the whole story. A gate that answered at boot can
die at 03:00, and ``mode`` alone would go on saying ``real`` forever:
"configured real" is not "reachable now", and /healthz is the entire
monitoring surface for this service. So the gate block also carries a
**cached reachability probe**, built the way helm builds sibling reachability
for its five siblings — ``helm/clients.py::SiblingClient.status`` (probe
/healthz; report ``online`` with a ``label`` and a ``detail``) plus the
``max_age`` cache in ``helm/console.py::service_statuses`` (reuse the answer
for a few seconds so a hot read path does not become a probe storm).

The probe never raises. A health endpoint that can 500 is not a health
endpoint.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .engine import GridWatch
from .substrates import load_registry
from .throughline import (
    DEFAULT_URL,
    HttpThroughlineClient,
    MockThroughlineClient,
    SubstrateUnreachable,
    ThroughlineClient,
)

DEFAULT_PORT = 8602

#: Seconds a gate reachability probe is reused before another is taken. Same
#: idiom and same order of magnitude as helm's ``service_statuses(max_age=3.0)``.
GATE_PROBE_MAX_AGE = float(os.environ.get("BREAKER_GATE_PROBE_MAX_AGE", "3.0"))

#: The name of the one dependency breaker has. Named in every 503 body so an
#: operator reading the failure does not have to guess which substrate died.
GATE_DEPENDENCY = "throughline"


@dataclass
class Settings:
    port: int = int(os.environ.get("BREAKER_PORT", DEFAULT_PORT))
    host: str = os.environ.get("BREAKER_HOST", "127.0.0.1")
    throughline_url: str = os.environ.get("THROUGHLINE_URL", DEFAULT_URL)
    substrate_mode: str = os.environ.get("BREAKER_SUBSTRATE", "auto")  # auto|real|mock
    substrates_config: Path = Path(
        os.environ.get("BREAKER_SUBSTRATES", str(Path(__file__).resolve().parents[1]
                                                 / "config" / "substrates.yaml"))
    )


def choose_client(settings: Settings) -> tuple[ThroughlineClient, Optional[str]]:
    """Return the substrate client and, when mocked, the reason why."""
    if settings.substrate_mode == "mock":
        return MockThroughlineClient(), "BREAKER_SUBSTRATE=mock"

    client = HttpThroughlineClient(settings.throughline_url)
    try:
        health = client.health()
    except SubstrateUnreachable as exc:
        if settings.substrate_mode == "real":
            # Explicitly asked for the real gate and it is not there: hold,
            # do not quietly degrade into a mock that always says yes.
            raise
        return MockThroughlineClient(), f"throughline unreachable ({exc})"
    if health.get("component") != "throughline":
        if settings.substrate_mode == "real":
            raise SubstrateUnreachable(
                f"{settings.throughline_url} did not identify as throughline")
        return MockThroughlineClient(), "endpoint did not identify as throughline"
    return client, None


class BreakerService:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()
        self.registry = load_registry(self.settings.substrates_config)
        self.client, self.mock_reason = choose_client(self.settings)
        self.watch = GridWatch(client=self.client, registry=self.registry)
        self._probe_cache: Optional[dict[str, Any]] = None
        self._probe_at: float = 0.0
        self._model_probe_cache: Optional[dict[str, Any]] = None
        self._model_probe_at: float = 0.0

    @property
    def gate_mode(self) -> str:
        return getattr(self.client, "mode", "unknown")

    # ------------------------------------------------------- reachability

    def _probe_gate(self) -> dict[str, Any]:
        """One reachability probe. Returns a verdict; never raises."""
        checked_at = datetime.now(timezone.utc).isoformat()
        if self.gate_mode == "mock":
            # The mock runs in this process. It is reachable by construction,
            # and saying so is not a claim that a real gate exists — the mode
            # and mock_reason fields right beside it say it does not.
            return {
                "reachable": True,
                "label": "in-process mock",
                "detail": "in-process mock gate; no substrate was contacted",
                "checked_at": checked_at,
            }
        try:
            health = self.client.health()
        except SubstrateUnreachable as exc:
            return {
                "reachable": False,
                "label": "gate offline",
                "detail": f"{GATE_DEPENDENCY} unreachable: {exc}",
                "checked_at": checked_at,
            }
        except Exception as exc:  # a probe must not be able to take /healthz down
            return {
                "reachable": False,
                "label": "gate offline",
                "detail": f"{GATE_DEPENDENCY} probe failed: {type(exc).__name__}: {exc}",
                "checked_at": checked_at,
            }
        component = (health or {}).get("component")
        if component != GATE_DEPENDENCY:
            return {
                "reachable": False,
                "label": "gate offline",
                "detail": (f"{self.settings.throughline_url} answered but did not "
                           f"identify as {GATE_DEPENDENCY} (component={component!r})"),
                "checked_at": checked_at,
            }
        return {
            "reachable": True,
            "label": "online",
            "detail": "",
            "checked_at": checked_at,
        }

    def gate_reachability(
        self, max_age: float = GATE_PROBE_MAX_AGE, force: bool = False
    ) -> dict[str, Any]:
        """Cached reachability for the one dependency breaker has.

        ``force=True`` skips the cache; the 503 path uses it so an operator
        reading a failure gets a probe taken after the failure, not before.
        """
        now = time.time()
        cached = self._probe_cache is not None and not force and (
            now - self._probe_at < max_age)
        if not cached:
            self._probe_cache = self._probe_gate()
            self._probe_at = now
            now = time.time()
        verdict = dict(self._probe_cache or {})
        verdict["cached"] = cached
        verdict["cache_age_seconds"] = round(max(0.0, now - self._probe_at), 3)
        verdict["max_age_seconds"] = max_age
        return verdict

    def gate_is_reachable(self) -> bool:
        return bool(self.gate_reachability().get("reachable"))

    def substrate_reachability(self) -> dict[str, dict[str, Any]]:
        """``available`` vs ``reachable`` for the judgment substrates.

        Audit finding from the same review that produced the gate defect:
        ``available`` in config/substrates.yaml is a *claim* — it says the
        substrate is registered and permitted to run here. For the
        model-endpoint substrate it says nothing at all about whether the DGX
        Spark answered, so /healthz could show ``available: true`` for an
        endpoint that had been down for a week.

        Only the ACTIVE model endpoint is probed, and behind the same cache:
        /healthz must stay cheap, and reaching across the network to a box
        nobody selected is not a health check, it is a side effect. An
        unprobed substrate reports ``reachable: null`` and says how to probe
        it, which is honest; ``true`` would not be.
        """
        out: dict[str, dict[str, Any]] = {}
        for substrate in self.registry.all():
            entry: dict[str, Any] = {"available": substrate.available}
            if not substrate.available:
                entry["reachable"] = False
                entry["detail"] = (
                    f"registered but not available here: {substrate.label}")
            elif substrate.kind == "deterministic-rule":
                entry["reachable"] = True
                entry["detail"] = "in-process rule; nothing to reach"
            elif substrate.kind == "model-endpoint":
                if substrate.id != self.registry.active.id:
                    entry["reachable"] = None
                    entry["detail"] = (
                        "not probed — not the active substrate; "
                        f"GET /substrates/{substrate.id}/health to probe it")
                else:
                    health = self._probe_model_endpoint()
                    entry["reachable"] = bool(health.get("reachable"))
                    entry["detail"] = health.get("detail", "")
                    entry["model_served"] = health.get("model_served")
            else:
                entry["reachable"] = None
                entry["detail"] = "no probe defined for this substrate kind"
            out[substrate.id] = entry
        return out

    def _probe_model_endpoint(self) -> dict[str, Any]:
        """Cached probe of the active model endpoint. Never raises."""
        now = time.time()
        if (self._model_probe_cache is not None
                and now - self._model_probe_at < GATE_PROBE_MAX_AGE):
            return self._model_probe_cache
        try:
            health = self.watch.judge.health()
        except Exception as exc:
            health = {"reachable": False,
                      "detail": f"{type(exc).__name__}: {exc}",
                      "model_served": False}
        self._model_probe_cache = health
        self._model_probe_at = time.time()
        return health

    # ------------------------------------------------------------- status

    def gate_status(self, probe: bool = True) -> dict:
        status = {
            "mode": self.gate_mode,
            "url": self.settings.throughline_url,
            "mock_reason": self.mock_reason,
            "dependency": GATE_DEPENDENCY,
        }
        if self.gate_mode == "mock":
            status["note"] = (
                "MOCK GATE — no real throughline held this. Hold semantics are "
                "simulated; nothing here proves the real gate."
            )
        else:
            status["note"] = "Real throughline gate."
        if not probe:
            return status
        reachability = self.gate_reachability()
        # `mode` says how the gate is CONFIGURED; `reachable` says whether it
        # answered just now. Reporting only the first is how a health check
        # comes back green while its only dependency is dead.
        status["reachable"] = reachability["reachable"]
        status["reachability"] = reachability
        if not reachability["reachable"]:
            status["note"] = (
                f"Gate configured as {self.gate_mode} at "
                f"{self.settings.throughline_url} but NOT REACHABLE — "
                f"{reachability['detail']}. breaker is fail-closed: nothing "
                f"dispatches while the gate cannot be reached."
            )
        return status
