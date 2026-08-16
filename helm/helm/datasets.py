"""What data the federation is standing on, gathered from every component.

Each component serves its own declared dataset registry at ``GET /datasets``
(throughline's ``datasets_api``). helm asks all of them and composes one view,
so an operator can answer "what feeds are we actually using, and where did
they come from" without opening five terminals.

Three honesty rules, which are the only reason this module is more than a
loop over five URLs:

1. **A component that is not running is reported offline, not empty.** A
   sibling with connection refused contributes ``reachable: False`` and its
   declared entries from the offline declaration file — never a zero.
2. **A declared-but-unbuilt dataset is listed, labelled.** blindspot serves
   nothing on :8604 by design, so its datasets can only come from the
   declaration file. They are marked ``declared_only`` so the UI can say where
   the knowledge came from instead of implying a live read.
3. **Nothing is invented.** If a component is unreachable and has no declared
   fallback, its datasets are ``unknown`` — which is not the same as none, and
   is rendered as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

#: The wording the console already uses for anything designed but not built.
DESIGNED_NOT_BUILT = (
    "designed, not built — no figure was invented to fill the gap"
)

#: Components whose registries helm composes, in display order.
COMPONENTS = ("throughline", "docket", "breaker", "siren", "blindspot")

DEFAULT_DECLARATION = Path(__file__).resolve().parent / "declared_datasets.yaml"


@dataclass
class ComponentDatasets:
    """One component's contribution to the federated view.

    Deliberately shaped like :class:`helm.attest.Reading` — ``known``,
    ``detail``, ``source`` mean the same things here as they do there, and for
    the same reason. This is the house three-valued rule (``allow | deny |
    unknown`` in warrant's words) applied to provenance: a component we could
    not read is ``known=False``, which is not the same fact as a component
    that told us it has no datasets.
    """

    component: str
    reachable: bool
    datasets: list[dict[str, Any]] = field(default_factory=list)
    declared_only: bool = False
    detail: str = ""
    source: str = ""

    @property
    def known(self) -> bool:
        """False when we have neither a live read nor a declaration.

        The console must render this as "unknown", never as an empty list.
        """
        return self.reachable or self.declared_only

    @property
    def unavailable_count(self) -> int:
        return sum(1 for d in self.datasets
                   if d.get("availability") == "unavailable")

    def as_dict(self) -> dict[str, Any]:
        # THREE-VALUED, deliberately. An unknown component carries
        # ``datasets: null`` and ``count: null`` — NOT ``[]`` and not ``0``.
        # An empty list is a claim ("this component has no datasets"); null is
        # the absence of a claim ("we could not find out"). Collapsing the two
        # is the failure mode this project refuses: a caller must never be able
        # to read "the substrate is unreachable" as "there is nothing there".
        return {
            "component": self.component,
            "reachable": self.reachable,
            "declared_only": self.declared_only,
            "known": self.known,
            "detail": self.detail,
            "source": self.source,
            "count": len(self.datasets) if self.known else None,
            "unavailable": self.unavailable_count if self.known else None,
            "datasets": self.datasets if self.known else None,
        }


@dataclass
class DatasetLookup:
    """The answer to "is there a dataset with this id?", in three values.

    ``found`` and ``known`` are separate on purpose, and the pair is the whole
    point of this type:

    ====================  =========  =========  ==============================
    outcome               ``found``  ``known``  what a surface must say
    ====================  =========  =========  ==============================
    the dataset is here   True       True       200, with the entry
    no component has it   False      True       404, "no such dataset"
    a registry is silent  False      False      503, "cannot tell", never 404
    ====================  =========  =========  ==============================

    A 404 is an ASSERTION that the thing does not exist. Making it from an
    incomplete read asserts something we did not establish.
    """

    dataset_id: str
    entry: Optional[dict[str, Any]] = None
    known: bool = True
    searched: list[str] = field(default_factory=list)
    unknown_components: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.entry is not None

    @property
    def message_ui(self) -> str:
        if self.found:
            return f"{self.dataset_id} is declared by a component we could read."
        if self.known:
            return (f"There is no dataset {self.dataset_id!r}: every component "
                    "registry was read and none declares it.")
        return (
            f"Whether a dataset {self.dataset_id!r} exists is UNKNOWN — "
            + ", ".join(self.unknown_components)
            + " could not be read and declared nothing, so this is not a "
              "statement that no such dataset exists."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "found": self.found,
            "known": self.known,
            "searched": self.searched,
            "unknown_components": self.unknown_components,
            "detail": self.detail,
            "message_ui": self.message_ui,
        }


def load_declarations(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Read the offline declaration file.

    This file exists for components that cannot answer for themselves —
    principally blindspot, which by design serves nothing. Each block records
    which repository is the source of truth, so the copy here is traceable
    rather than authoritative.
    """
    path = path or DEFAULT_DECLARATION
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        block["component"]: block
        for block in doc.get("declarations", [])
        if block.get("component")
    }


class FederatedDatasets:
    """Compose every component's dataset registry into one read model."""

    def __init__(self, federation: Any,
                 declarations: Optional[dict[str, dict[str, Any]]] = None) -> None:
        self.federation = federation
        self.declarations = (
            declarations if declarations is not None else load_declarations()
        )

    def _declared(self, component: str, detail: str) -> ComponentDatasets:
        block = self.declarations.get(component)
        if not block:
            # No live read and nothing declared. Say unknown; do not say none.
            note = "no declaration on file, so this is unknown rather than none"
            return ComponentDatasets(
                component=component, reachable=False, declared_only=False,
                detail=f"{detail} — {note}" if detail else note,
                source="")
        return ComponentDatasets(
            component=component,
            reachable=False,
            declared_only=True,
            datasets=block.get("datasets", []),
            detail=detail,
            source=block.get("source_of_truth", ""),
        )

    def for_component(self, component: str) -> ComponentDatasets:
        client = self.federation.all.get(component)
        if client is None:
            return self._declared(component, "no client configured")

        reply = client.get("/datasets")
        if not reply.ok:
            # Offline, or serving no dataset surface yet. Either way we do not
            # have a live answer, so fall back to what was declared and label it.
            detail = reply.error or f"HTTP {reply.status}"
            return self._declared(component, detail)

        data = reply.data or {}
        return ComponentDatasets(
            component=component,
            reachable=True,
            datasets=data.get("datasets", []),
            detail="",
            source=data.get("source", ""),
        )

    def all(self) -> list[ComponentDatasets]:
        return [self.for_component(name) for name in COMPONENTS]

    def summary(self) -> dict[str, Any]:
        blocks = self.all()
        every: list[dict[str, Any]] = []
        for block in blocks:
            for entry in block.datasets:
                every.append({**entry, "_declared_only": block.declared_only,
                              "_reachable": block.reachable})

        licensed = [d for d in every if (d.get("licence") or "").strip()
                    and d.get("licence") != "unknown"]
        unknown = [b.component for b in blocks if not b.known]
        # ``complete`` when every component answered for itself or had a
        # declaration; ``partial`` when at least one could not; ``unknown``
        # when none could. The totals below count only what is KNOWN, and
        # ``complete`` is what says whether they are the whole story.
        if not unknown:
            status = "complete"
        elif len(unknown) == len(blocks):
            status = "unknown"
        else:
            status = "partial"
        return {
            "components": [block.as_dict() for block in blocks],
            "status": status,
            "complete": status == "complete",
            # Counts over the components we could actually read. Never a
            # figure that silently includes an unreachable component as zero.
            "total": len(every) if status != "unknown" else None,
            "counted_over": [b.component for b in blocks if b.known],
            "unavailable": sum(1 for d in every
                               if d.get("availability") == "unavailable"),
            "licence_unknown": sum(1 for d in every
                                   if d.get("licence") == "unknown"),
            "licence_known": len(licensed),
            "components_unreachable": [b.component for b in blocks
                                       if not b.reachable],
            "components_unknown": unknown,
            "honesty_note": DESIGNED_NOT_BUILT,
        }

    def locate(self, dataset_id: str) -> "DatasetLookup":
        """Three-valued lookup: **found**, **no such dataset**, or **cannot tell**.

        ``find`` below answers ``None`` for the last two alike, and its callers
        rendered that as a definite 404 — "no dataset X is declared by any
        component" — even when a component had not answered and had no
        declaration on file. That is the unknown-as-definite-negative failure
        this project has already corrected twice elsewhere, appearing in a
        third place: the honest answer to "is there a dataset X?" while one
        registry is silent is that we cannot tell, not that there is not.

        An absence is a fact only when every registry was actually read.
        """
        blocks = self.all()
        for block in blocks:
            for entry in block.datasets:
                if entry.get("id") == dataset_id:
                    return DatasetLookup(
                        dataset_id=dataset_id,
                        entry={**entry,
                               "_component_reachable": block.reachable,
                               "_declared_only": block.declared_only,
                               "_source": block.source},
                        known=True,
                        searched=[b.component for b in blocks if b.known],
                    )
        unknown = [b.component for b in blocks if not b.known]
        return DatasetLookup(
            dataset_id=dataset_id,
            entry=None,
            # KNOWN-absent only when every registry answered for itself or had
            # a declaration on file. One silent component and the absence is
            # unproven, so we do not assert it.
            known=not unknown,
            searched=[b.component for b in blocks if b.known],
            unknown_components=unknown,
            detail="; ".join(f"{b.component}: {b.detail}"
                             for b in blocks if not b.known and b.detail),
        )

    def find(self, dataset_id: str) -> Optional[dict[str, Any]]:
        """The entry, or ``None``. Kept for callers that only need the hit.

        ``None`` here is ambiguous by construction — it means both "no such
        dataset" and "we could not read every registry". Any caller that turns
        it into a verdict a person will read must use :meth:`locate` instead.
        """
        return self.locate(dataset_id).entry
