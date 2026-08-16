"""The judgment substrate registry.

One contract, more than one possible implementation behind it. Today the
deterministic rule is the only *available* substrate; cuOpt is registered as an
alternate and is honestly labelled unavailable-without-GPU everywhere it is
shown.

Two rules this module exists to enforce:

1. **Never claim a solver where a rule runs.** Every judgment carries the id
   and label of the substrate that actually produced it, so a screen cannot
   attribute the rule's verdict to cuOpt.
2. **Never install cuOpt.** It is GPU-only and ~10GB, and the PyPI name
   ``cuopt`` is a squatted stub. Selecting an unavailable substrate raises;
   it does not silently fall back and it does not attempt an install.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "substrates.yaml"


class SubstrateUnavailable(RuntimeError):
    """A registered substrate was selected that cannot run here."""


@dataclass(frozen=True)
class Substrate:
    id: str
    kind: str
    available: bool
    label: str
    description: str = ""
    requires: Optional[dict[str, Any]] = None
    endpoint: Optional[str] = None
    model: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "available": self.available,
            "label": self.label,
            "description": self.description.strip(),
            "requires": self.requires,
            "endpoint": self.endpoint,
            "model": self.model,
        }


class SubstrateRegistry:
    def __init__(self, substrates: list[Substrate], active: str) -> None:
        self._substrates = {substrate.id: substrate for substrate in substrates}
        if active not in self._substrates:
            raise ValueError(f"active substrate {active!r} is not registered")
        active_substrate = self._substrates[active]
        if not active_substrate.available:
            raise SubstrateUnavailable(
                f"{active_substrate.id} is registered but unavailable "
                f"({active_substrate.label}); breaker will not pretend otherwise"
            )
        self._active = active

    @property
    def active(self) -> Substrate:
        return self._substrates[self._active]

    def get(self, substrate_id: str) -> Substrate:
        if substrate_id not in self._substrates:
            raise KeyError(substrate_id)
        return self._substrates[substrate_id]

    def select(self, substrate_id: str) -> Substrate:
        """Choose a substrate. Refuses an unavailable one, loudly."""
        substrate = self.get(substrate_id)
        if not substrate.available:
            raise SubstrateUnavailable(
                f"{substrate.id} is a registered alternate and is not available "
                f"here: {substrate.label}"
            )
        self._active = substrate_id
        return substrate

    def all(self) -> list[Substrate]:
        return list(self._substrates.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active.id,
            "active_label": self.active.label,
            "substrates": [s.as_dict() for s in self.all()],
        }


def load_registry(path: str | Path = DEFAULT_CONFIG) -> SubstrateRegistry:
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    substrates = [
        Substrate(
            id=entry["id"],
            kind=entry.get("kind", "unknown"),
            available=bool(entry.get("available", False)),
            label=entry.get("label", entry["id"]),
            description=entry.get("description", ""),
            requires=entry.get("requires"),
            endpoint=entry.get("endpoint"),
            model=entry.get("model"),
        )
        for entry in document.get("substrates", [])
    ]
    return SubstrateRegistry(substrates, active=document.get("active", "rule"))
