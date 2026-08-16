"""YAML config loader that refuses unsafe configurations.

The refusal is the pitch: a config that marks an irreversible effect
``auto_execute: true`` is not partially applied, not warned about, and not
applied with the offending rule dropped. The **whole config** is refused and
the offending rule is named by file, rule id and line.

Reload is atomic. Validation happens entirely against the candidate document;
the running config is only swapped in after the candidate is fully accepted,
so an invalid reload leaves the previous config untouched.

Two further refusals guard the reload surface itself, because the registry
decides what the gate holds and is therefore part of the gate:

* the candidate must live inside the operator's allowlisted config directory
  (``POLICY_SOURCE_ALLOWLIST``) — an arbitrary path is refused by name;
* a candidate that would reclassify any effect type *down* to ``reversible``
  (``POLICY_REVERSIBILITY_DOWNGRADE``) is not applied on the spot. It is held
  at the gate as an irreversible effect, which is what loosening a safety
  classification is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

VALID_CLASSES = ("reversible", "irreversible")

#: Policy identifiers reported in refusals, so an operator can look them up.
POLICY_IRREVERSIBLE_AUTO_EXECUTE = "no-auto-execute-on-irreversible"
POLICY_UNKNOWN_CLASS = "reversibility-class-must-be-known"
POLICY_RULE_SHAPE = "effect-rule-shape"
POLICY_SOURCE_ALLOWLIST = "config-source-must-be-allowlisted"
POLICY_REVERSIBILITY_DOWNGRADE = "reversibility-downgrade-must-pass-the-gate"

_LINE_KEY = "__line__"
_KEY_LINES = "__key_lines__"


class ConfigRefusal(Exception):
    """Raised when a configuration is refused. Names file, rule and line."""

    def __init__(
        self,
        *,
        file: str,
        rule: Optional[str],
        line: Optional[int],
        policy: str,
        message: str,
    ) -> None:
        self.file = file
        self.rule = rule
        self.line = line
        self.policy = policy
        self.message = message
        located = f"{file}:{line}" if line else file
        super().__init__(f"{located}: rule {rule!r} violates {policy}: {message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "refused": True,
            "file": self.file,
            "rule": self.rule,
            "line": self.line,
            "policy": self.policy,
            "message": self.message,
            "detail": str(self),
        }


class _LineLoader(yaml.SafeLoader):
    """SafeLoader that records the source line of every mapping and key."""


def _construct_mapping(loader: _LineLoader, node: yaml.MappingNode) -> dict[str, Any]:
    mapping = yaml.SafeLoader.construct_mapping(loader, node, deep=True)
    mapping[_LINE_KEY] = node.start_mark.line + 1
    mapping[_KEY_LINES] = {
        key.value: value.start_mark.line + 1
        for key, value in node.value
        if isinstance(key, yaml.ScalarNode)
    }
    return mapping


_LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _strip_lines(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_lines(v) for k, v in value.items() if k not in (_LINE_KEY, _KEY_LINES)
        }
    if isinstance(value, list):
        return [_strip_lines(v) for v in value]
    return value


class EffectRule:
    """One registry row: an effect type and the class the gate derives from."""

    def __init__(
        self,
        rule_id: str,
        effect_type: str,
        reversibility_class: str,
        auto_execute: bool,
        description: Optional[str] = None,
    ) -> None:
        self.id = rule_id
        self.effect_type = effect_type
        self.reversibility_class = reversibility_class
        self.auto_execute = auto_execute
        self.description = description

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "effect_type": self.effect_type,
            "reversibility_class": self.reversibility_class,
            "auto_execute": self.auto_execute,
            "description": self.description,
        }


class Config:
    """An accepted configuration. Existing instances are never mutated."""

    def __init__(self, source: str, version: int, rules: list[EffectRule]) -> None:
        self.source = source
        self.version = version
        self.rules = rules
        self.by_effect_type = {rule.effect_type: rule for rule in rules}

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "effects": [rule.as_dict() for rule in self.rules],
        }

    @staticmethod
    def empty() -> "Config":
        return Config(source="<default>", version=0, rules=[])


def _refuse(file: str, rule: Optional[str], line: Optional[int], policy: str, msg: str):
    raise ConfigRefusal(file=file, rule=rule, line=line, policy=policy, message=msg)


def parse_config(text: str, source: str) -> Config:
    """Parse and validate a config document. Refuses, never half-applies."""
    try:
        raw = yaml.load(text, Loader=_LineLoader) or {}
    except yaml.YAMLError as exc:
        _refuse(source, None, None, POLICY_RULE_SHAPE, f"not valid YAML: {exc}")

    if not isinstance(raw, dict):
        _refuse(source, None, None, POLICY_RULE_SHAPE, "top level must be a mapping")

    entries = raw.get("effects") or []
    if not isinstance(entries, list):
        _refuse(
            source,
            None,
            raw.get(_KEY_LINES, {}).get("effects"),
            POLICY_RULE_SHAPE,
            "'effects' must be a list of rules",
        )

    rules: list[EffectRule] = []
    for index, entry in enumerate(entries):
        line = entry.get(_LINE_KEY) if isinstance(entry, dict) else None
        if not isinstance(entry, dict):
            _refuse(source, None, line, POLICY_RULE_SHAPE, f"rule #{index} is not a mapping")

        key_lines = entry.get(_KEY_LINES, {})
        rule_id = entry.get("id") or f"effects[{index}]"
        effect_type = entry.get("effect_type")
        klass = entry.get("reversibility_class")
        auto_execute = bool(entry.get("auto_execute", False))

        if not effect_type:
            _refuse(source, rule_id, line, POLICY_RULE_SHAPE, "missing 'effect_type'")
        if klass not in VALID_CLASSES:
            _refuse(
                source,
                rule_id,
                key_lines.get("reversibility_class", line),
                POLICY_UNKNOWN_CLASS,
                f"reversibility_class must be one of {VALID_CLASSES}, got {klass!r}",
            )

        # The refusal. It runs before any rule becomes active.
        if klass == "irreversible" and auto_execute:
            _refuse(
                source,
                rule_id,
                key_lines.get("auto_execute", line),
                POLICY_IRREVERSIBLE_AUTO_EXECUTE,
                "an irreversible effect may not be marked auto_execute: true; "
                "irreversible effects are held for a human, always",
            )

        rules.append(
            EffectRule(
                rule_id=rule_id,
                effect_type=effect_type,
                reversibility_class=klass,
                auto_execute=auto_execute,
                description=_strip_lines(entry.get("description")),
            )
        )

    version = raw.get("version", 1)
    if not isinstance(version, int):
        _refuse(source, None, raw.get(_KEY_LINES, {}).get("version"), POLICY_RULE_SHAPE,
                "'version' must be an integer")

    return Config(source=source, version=version, rules=rules)


def load_config(path: str | Path) -> Config:
    """Load and validate a config file. Raises ``ConfigRefusal``."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _refuse(str(path), None, None, POLICY_RULE_SHAPE, f"unreadable config: {exc}")
    return parse_config(text, source=str(path))


def resolve_source(path: str | Path, allowed_dirs: Optional[Iterable[Path]]) -> Path:
    """Resolve ``path`` and refuse it if it falls outside the allowlist.

    ``POST /config/reload`` used to accept an arbitrary filesystem path with
    no authentication, which let anyone who could reach the port hand the
    service a registry of their own choosing. The reload surface is now
    confined to a directory the operator nominated at boot; a path outside it
    is refused by name, exactly like a malformed rule is.

    ``allowed_dirs`` of ``None`` means "no confinement configured" and keeps
    the old behaviour, so a library caller (``load_config``, the tests, the
    boot path) is unaffected. The HTTP surface always passes a list.
    """
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError as exc:  # pragma: no cover - resolve() rarely raises here
        _refuse(str(path), None, None, POLICY_SOURCE_ALLOWLIST,
                f"path cannot be resolved: {exc}")
    if allowed_dirs is None:
        return resolved

    roots = [Path(d).expanduser().resolve() for d in allowed_dirs]
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    _refuse(
        str(resolved),
        None,
        None,
        POLICY_SOURCE_ALLOWLIST,
        "config source is outside the reload allowlist "
        f"({', '.join(str(r) for r in roots) or 'empty'}); refusing to load "
        f"{resolved}. The running configuration is untouched.",
    )
    raise AssertionError("unreachable")  # pragma: no cover


def downgrades(current: Config, candidate: Config) -> list[dict[str, Any]]:
    """Effect types the candidate would make *easier* to execute.

    An effect type the registry has never heard of is treated as irreversible
    by the gate, so "unregistered -> reversible" is a downgrade too. That is
    the exact move the security review used: ``grid.load_shed`` was in no
    registry, and one reload made it reversible and auto-executing.
    """
    found: list[dict[str, Any]] = []
    for rule in candidate.rules:
        if rule.reversibility_class != "reversible":
            continue
        previous = current.by_effect_type.get(rule.effect_type)
        before = previous.reversibility_class if previous else "irreversible"
        if previous is not None and before == "reversible":
            continue  # already reversible; nothing is being loosened
        found.append({
            "effect_type": rule.effect_type,
            "rule": rule.id,
            "from": before,
            "from_registered": previous is not None,
            "to": rule.reversibility_class,
            "auto_execute": rule.auto_execute,
        })
    return found


class ConfigStore:
    """Holds the running config and swaps it atomically.

    A refused reload ledgers the refusal and leaves ``current`` untouched.

    ``stage`` and ``apply`` split what ``reload`` does in one step, so a
    caller can validate a candidate, notice that it would downgrade an
    effect type, and hold it at the gate before it becomes the running
    registry. ``reload`` still exists and still behaves exactly as it did.
    """

    def __init__(self, ledger=None, config: Optional[Config] = None) -> None:
        self._ledger = ledger
        self._current = config or Config.empty()

    @property
    def current(self) -> Config:
        return self._current

    def stage(
        self, path: str | Path, allowed_dirs: Optional[Iterable[Path]] = None
    ) -> Config:
        """Validate a candidate config without making it current."""
        previous = self._current
        try:
            resolved = resolve_source(path, allowed_dirs)
            candidate = load_config(resolved)
        except ConfigRefusal as refusal:
            # Ledger the refusal, then re-raise. The previous config keeps running.
            if self._ledger is not None:
                self._ledger.append("config.refused", refusal.as_dict())
            assert self._current is previous
            raise
        return candidate

    def apply(self, candidate: Config) -> Config:
        """Swap in an already-validated candidate. One rebind, nothing partial."""
        self._current = candidate
        if self._ledger is not None:
            self._ledger.append(
                "config.loaded",
                {"source": candidate.source, "version": candidate.version,
                 "rules": len(candidate.rules)},
            )
        return candidate

    def reload(
        self, path: str | Path, allowed_dirs: Optional[Iterable[Path]] = None
    ) -> Config:
        return self.apply(self.stage(path, allowed_dirs))
