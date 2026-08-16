"""The dataset registry: every dataset a component consumes, declared.

This is the same loader idiom as :mod:`throughline.config`, applied to data
provenance instead of effect reversibility, and it reuses that module's
machinery rather than inventing a second config mechanism: the same
``_LineLoader`` for line numbers, the same :class:`~throughline.config.ConfigRefusal`
for refusals, and the same ledger events (``config.refused`` / ``config.loaded``)
so a bad dataset config is refused and ledgered exactly like a bad effect rule.

The refusal is the same pitch, pointed at provenance. A registry entry that
claims more than it can evidence is not warned about and not dropped — the
**whole file** is refused, naming file, entry id and line:

* no entry may omit its **licence** or its **provenance**;
* a ``cached`` entry must carry an ``as_of`` timestamp, because a snapshot
  without an as-of time is a fixture pretending to be current;
* a ``fixture`` entry may not be labelled ``real`` — a generated fixture is
  ``synthetic``, always;
* a ``declared-unavailable`` entry may not be labelled available, and an
  entry with no reachable source may not claim ``availability: available``.

Reload is atomic, via the shared :class:`~throughline.config.ConfigStore`
contract: the running registry is only swapped in after the candidate is
fully accepted, so an invalid reload leaves the previous registry untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .config import (
    ConfigRefusal,
    _KEY_LINES,
    _LINE_KEY,
    _LineLoader,
    _strip_lines,
    resolve_source,
)

#: How a dataset is being obtained right now.
#:
#: ``live``                  fetched from its source at request time
#: ``cached``                served from an on-disk snapshot taken at ``as_of``
#: ``fixture``               generated or hand-authored; never claimed as real
#: ``declared-unavailable``  named in a spec, deliberately not built
VALID_MODES = ("live", "cached", "fixture", "declared-unavailable")

#: Reuses the Signal schema's vocabulary, so a dataset and the signals it
#: produces describe their reality with the same two words.
VALID_REAL_OR_SYNTHETIC = ("real", "synthetic")

VALID_AVAILABILITY = ("available", "unavailable", "degraded")

#: Policy identifiers reported in refusals, so an operator can look them up.
POLICY_DATASET_SHAPE = "dataset-entry-shape"
POLICY_DATASET_LICENCE = "dataset-licence-required"
POLICY_DATASET_PROVENANCE = "dataset-provenance-required"
POLICY_DATASET_MODE = "dataset-mode-must-be-known"
POLICY_DATASET_AS_OF = "cached-dataset-must-carry-as-of"
POLICY_DATASET_HONESTY = "no-availability-claim-without-evidence"

#: The honesty idiom, verbatim, for anything designed but never built.
DESIGNED_NOT_BUILT = "designed, not built — no figure was invented to fill the gap"


def _refuse(file: str, entry: Optional[str], line: Optional[int], policy: str, msg: str):
    raise ConfigRefusal(file=file, rule=entry, line=line, policy=policy, message=msg)


class DatasetEntry:
    """One registry row: a dataset, and everything we can evidence about it."""

    def __init__(
        self,
        dataset_id: str,
        name: str,
        component: str,
        mode: str,
        source: str,
        licence: str,
        provenance: str,
        real_or_synthetic: str,
        availability: str,
        dataset_identifier: Optional[str] = None,
        refresh: Optional[str] = None,
        as_of: Optional[str] = None,
        offline_cache: Optional[str] = None,
        record_count: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> None:
        self.id = dataset_id
        self.name = name
        self.component = component
        self.mode = mode
        self.source = source
        self.licence = licence
        self.provenance = provenance
        self.real_or_synthetic = real_or_synthetic
        self.availability = availability
        self.dataset_identifier = dataset_identifier
        self.refresh = refresh
        self.as_of = as_of
        self.offline_cache = offline_cache
        self.record_count = record_count
        self.notes = notes

    @property
    def unavailable(self) -> bool:
        return self.availability == "unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "component": self.component,
            "mode": self.mode,
            "source": self.source,
            "dataset_identifier": self.dataset_identifier,
            "licence": self.licence,
            "provenance": self.provenance,
            "real_or_synthetic": self.real_or_synthetic,
            "availability": self.availability,
            "refresh": self.refresh,
            "as_of": self.as_of,
            "offline_cache": self.offline_cache,
            "record_count": self.record_count,
            "notes": self.notes,
        }


class DatasetRegistry:
    """An accepted registry. Existing instances are never mutated."""

    def __init__(self, source: str, version: int, component: str,
                 entries: list[DatasetEntry]) -> None:
        self.source = source
        self.version = version
        self.component = component
        self.entries = entries
        self.by_id = {entry.id: entry for entry in entries}

    def get(self, dataset_id: str) -> Optional[DatasetEntry]:
        return self.by_id.get(dataset_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "component": self.component,
            "count": len(self.entries),
            "datasets": [entry.as_dict() for entry in self.entries],
        }

    @staticmethod
    def empty(component: str = "unknown") -> "DatasetRegistry":
        return DatasetRegistry(source="<default>", version=0,
                               component=component, entries=[])


def _require_text(raw: Any, field: str, source: str, entry_id: str,
                  line: Optional[int], policy: str) -> str:
    """A required field is present, a string, and not a placeholder.

    ``"unknown"`` is an accepted *value* for a licence we genuinely could not
    establish — but it must be written down deliberately, not left blank.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        _refuse(source, entry_id, line, policy, f"missing '{field}'")
    if not isinstance(raw, str):
        _refuse(source, entry_id, line, policy, f"'{field}' must be a string")
    return raw.strip()


def parse_registry(text: str, source: str) -> DatasetRegistry:
    """Parse and validate a dataset registry. Refuses, never half-applies."""
    try:
        raw = yaml.load(text, Loader=_LineLoader) or {}
    except yaml.YAMLError as exc:
        _refuse(source, None, None, POLICY_DATASET_SHAPE, f"not valid YAML: {exc}")

    if not isinstance(raw, dict):
        _refuse(source, None, None, POLICY_DATASET_SHAPE, "top level must be a mapping")

    key_lines_top = raw.get(_KEY_LINES, {})

    component = raw.get("component")
    if not component or not isinstance(component, str):
        _refuse(source, None, key_lines_top.get("component"), POLICY_DATASET_SHAPE,
                "top level must name the owning 'component'")

    entries_raw = raw.get("datasets")
    if entries_raw is None:
        _refuse(source, None, key_lines_top.get("datasets"), POLICY_DATASET_SHAPE,
                "missing 'datasets'")
    if not isinstance(entries_raw, list):
        _refuse(source, None, key_lines_top.get("datasets"), POLICY_DATASET_SHAPE,
                "'datasets' must be a list of entries")

    entries: list[DatasetEntry] = []
    seen: dict[str, int] = {}

    for index, entry in enumerate(entries_raw):
        line = entry.get(_LINE_KEY) if isinstance(entry, dict) else None
        if not isinstance(entry, dict):
            _refuse(source, None, line, POLICY_DATASET_SHAPE,
                    f"dataset #{index} is not a mapping")

        key_lines = entry.get(_KEY_LINES, {})
        dataset_id = entry.get("id") or f"datasets[{index}]"

        if not entry.get("id"):
            _refuse(source, dataset_id, line, POLICY_DATASET_SHAPE, "missing 'id'")
        if dataset_id in seen:
            _refuse(source, dataset_id, line, POLICY_DATASET_SHAPE,
                    f"duplicate dataset id, first declared at line {seen[dataset_id]}")
        seen[dataset_id] = line or index

        name = _require_text(entry.get("name"), "name", source, dataset_id,
                             key_lines.get("name", line), POLICY_DATASET_SHAPE)
        owner = _require_text(entry.get("component"), "component", source, dataset_id,
                              key_lines.get("component", line), POLICY_DATASET_SHAPE)
        source_ref = _require_text(entry.get("source"), "source", source, dataset_id,
                                   key_lines.get("source", line), POLICY_DATASET_SHAPE)

        # Licence and provenance are not optional. "unknown" is allowed as an
        # honest answer; absence is not, because absence reads as "no licence
        # concern" when what it means is "nobody looked".
        licence = _require_text(entry.get("licence"), "licence", source, dataset_id,
                                key_lines.get("licence", line), POLICY_DATASET_LICENCE)
        provenance = _require_text(
            entry.get("provenance"), "provenance", source, dataset_id,
            key_lines.get("provenance", line), POLICY_DATASET_PROVENANCE)

        mode = entry.get("mode")
        if mode not in VALID_MODES:
            _refuse(source, dataset_id, key_lines.get("mode", line), POLICY_DATASET_MODE,
                    f"mode must be one of {VALID_MODES}, got {mode!r}")

        real_or_synthetic = entry.get("real_or_synthetic")
        if real_or_synthetic not in VALID_REAL_OR_SYNTHETIC:
            _refuse(source, dataset_id, key_lines.get("real_or_synthetic", line),
                    POLICY_DATASET_SHAPE,
                    "real_or_synthetic must be one of "
                    f"{VALID_REAL_OR_SYNTHETIC}, got {real_or_synthetic!r}")

        availability = entry.get("availability")
        if availability not in VALID_AVAILABILITY:
            _refuse(source, dataset_id, key_lines.get("availability", line),
                    POLICY_DATASET_SHAPE,
                    f"availability must be one of {VALID_AVAILABILITY}, "
                    f"got {availability!r}")

        as_of = entry.get("as_of")

        # --- the honesty refusals ------------------------------------------
        # A snapshot without its as-of time is a fixture wearing a live badge.
        if mode == "cached" and not as_of:
            _refuse(source, dataset_id, key_lines.get("as_of", line),
                    POLICY_DATASET_AS_OF,
                    "a cached dataset must carry 'as_of'; a snapshot without "
                    "its as-of time cannot be told apart from a live feed")

        # A generated fixture is synthetic. Always.
        if mode == "fixture" and real_or_synthetic != "synthetic":
            _refuse(source, dataset_id, key_lines.get("real_or_synthetic", line),
                    POLICY_DATASET_HONESTY,
                    "a fixture may not be labelled 'real'; a generated or "
                    "hand-authored fixture is synthetic, always")

        # Designed-but-unbuilt is never available, and never hidden.
        if mode == "declared-unavailable" and availability != "unavailable":
            _refuse(source, dataset_id, key_lines.get("availability", line),
                    POLICY_DATASET_HONESTY,
                    "a declared-unavailable dataset must report "
                    f"availability: unavailable — {DESIGNED_NOT_BUILT}")

        # An available cache must say where the cache is; otherwise "available"
        # is an assertion with nothing behind it.
        offline_cache = entry.get("offline_cache")
        if mode == "cached" and availability == "available" and not offline_cache:
            _refuse(source, dataset_id, key_lines.get("offline_cache", line),
                    POLICY_DATASET_HONESTY,
                    "a cached dataset reported available must name its "
                    "'offline_cache' location")

        record_count = entry.get("record_count")
        if record_count is not None and not isinstance(record_count, int):
            _refuse(source, dataset_id, key_lines.get("record_count", line),
                    POLICY_DATASET_SHAPE, "'record_count' must be an integer")

        entries.append(
            DatasetEntry(
                dataset_id=dataset_id,
                name=name,
                component=owner,
                mode=mode,
                source=source_ref,
                licence=licence,
                provenance=provenance,
                real_or_synthetic=real_or_synthetic,
                availability=availability,
                dataset_identifier=_strip_lines(entry.get("dataset_identifier")),
                refresh=_strip_lines(entry.get("refresh")),
                as_of=_strip_lines(as_of),
                offline_cache=_strip_lines(offline_cache),
                record_count=record_count,
                notes=_strip_lines(entry.get("notes")),
            )
        )

    version = raw.get("version", 1)
    if not isinstance(version, int):
        _refuse(source, None, key_lines_top.get("version"), POLICY_DATASET_SHAPE,
                "'version' must be an integer")

    return DatasetRegistry(source=source, version=version,
                           component=component, entries=entries)


def load_registry(path: str | Path,
                  allowed_dirs: Optional[Iterable[Path]] = None) -> DatasetRegistry:
    """Load and validate a dataset registry file. Raises ``ConfigRefusal``.

    ``allowed_dirs`` is passed straight to
    :func:`throughline.config.resolve_source`, so the dataset reload surface is
    confined exactly as the effect-config reload surface is: a path outside the
    operator's nominated directories is refused by name under
    ``config-source-must-be-allowlisted``, and the running registry is
    untouched. ``None`` means no confinement, which is what a library caller,
    the boot path and the tests use.
    """
    path = resolve_source(path, allowed_dirs)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _refuse(str(path), None, None, POLICY_DATASET_SHAPE,
                f"unreadable dataset registry: {exc}")
    return parse_registry(text, source=str(path))


class DatasetRegistryStore:
    """Holds the running registry and swaps it atomically.

    Mirrors :class:`~throughline.config.ConfigStore` exactly, including the
    ledger event types, so a refused dataset config is indistinguishable in
    the ledger from a refused effect config — one refusal idiom, not two.
    """

    def __init__(self, ledger=None, registry: Optional[DatasetRegistry] = None,
                 component: str = "unknown") -> None:
        self._ledger = ledger
        self._current = registry or DatasetRegistry.empty(component)

    @property
    def current(self) -> DatasetRegistry:
        return self._current

    def reload(self, path: str | Path,
               allowed_dirs: Optional[Iterable[Path]] = None) -> DatasetRegistry:
        previous = self._current
        try:
            candidate = load_registry(path, allowed_dirs)
        except ConfigRefusal as refusal:
            # Ledger the refusal, then re-raise. The previous registry keeps running.
            if self._ledger is not None:
                self._ledger.append("config.refused",
                                    {**refusal.as_dict(), "subject": "datasets"})
            assert self._current is previous
            raise
        self._current = candidate  # atomic swap: one name rebind, nothing partial
        if self._ledger is not None:
            self._ledger.append(
                "config.loaded",
                {"subject": "datasets", "source": candidate.source,
                 "version": candidate.version, "datasets": len(candidate.entries)},
            )
        return candidate
