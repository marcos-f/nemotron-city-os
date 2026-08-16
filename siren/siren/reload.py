"""The hot-reload beat, from siren's side.

throughline owns the loader and the atomic swap. siren owns the artifact
being dropped, the pre-flight that lets the refusal render with zero network,
and the timeline the pane draws: validated -> registered -> flowing.

The pre-flight deliberately re-implements throughline's published policy
rather than importing it: siren must be able to show the refusal in
OFFLINE_MODE, where there is no throughline to ask.

When throughline IS reachable, the pre-flight does NOT short-circuit the drop.
The refusal counter-beat is throughline's to perform: it refuses the file, it
names the rule, and it appends the refusal to its own ledger. A siren that
quietly withheld the bad file would leave that ledger entry unwritten and put
siren's opinion on screen where the substrate's answer belongs. So the file is
handed over, and what siren renders is what throughline said.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

VALID_CLASSES = ("reversible", "irreversible")

POLICY_IRREVERSIBLE_AUTO_EXECUTE = "no-auto-execute-on-irreversible"
POLICY_UNKNOWN_CLASS = "reversibility-class-must-be-known"
POLICY_RULE_SHAPE = "effect-rule-shape"

DEFAULT_INCIDENT_CONFIG = "config/incident.yaml"
INCIDENT_SIGNAL_CLASS = "fire.incident"

_LINE_KEY = "__line__"
_KEY_LINES = "__key_lines__"


class _LineLoader(yaml.SafeLoader):
    """SafeLoader that records the source line of every mapping and key, so a
    refusal can name the offending line and not merely the offending file."""


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


class ConfigRefusal(Exception):
    """A refused configuration, named by file, rule and line."""

    def __init__(self, *, file: str, rule: Optional[str], line: Optional[int],
                 policy: str, message: str) -> None:
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


def _refuse(file: str, rule: Optional[str], line: Optional[int], policy: str, msg: str):
    raise ConfigRefusal(file=file, rule=rule, line=line, policy=policy, message=msg)


def validate_config_text(text: str, source: str) -> dict[str, Any]:
    """Validate a candidate registry. Raises ``ConfigRefusal``; never returns
    a partially accepted document."""
    try:
        raw = yaml.load(text, Loader=_LineLoader) or {}
    except yaml.YAMLError as exc:
        _refuse(source, None, None, POLICY_RULE_SHAPE, f"not valid YAML: {exc}")

    if not isinstance(raw, dict):
        _refuse(source, None, None, POLICY_RULE_SHAPE, "top level must be a mapping")

    entries = raw.get("effects")
    if entries is None:
        _refuse(source, None, None, POLICY_RULE_SHAPE, "missing 'effects'")
    if not isinstance(entries, list):
        _refuse(source, None, raw.get(_KEY_LINES, {}).get("effects"),
                POLICY_RULE_SHAPE, "'effects' must be a list of rules")

    rules: list[dict[str, Any]] = []
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
            _refuse(source, rule_id, key_lines.get("reversibility_class", line),
                    POLICY_UNKNOWN_CLASS,
                    f"reversibility_class must be one of {VALID_CLASSES}, got {klass!r}")
        if klass == "irreversible" and auto_execute:
            _refuse(source, rule_id, key_lines.get("auto_execute", line),
                    POLICY_IRREVERSIBLE_AUTO_EXECUTE,
                    "an irreversible effect may not be marked auto_execute: true; "
                    "irreversible effects are held for a human, always")

        rules.append({
            "id": rule_id,
            "effect_type": effect_type,
            "reversibility_class": klass,
            "auto_execute": auto_execute,
            "description": entry.get("description"),
        })

    version = raw.get("version", 1)
    if not isinstance(version, int):
        _refuse(source, None, raw.get(_KEY_LINES, {}).get("version"),
                POLICY_RULE_SHAPE, "'version' must be an integer")

    classes = raw.get("signal_classes") or []
    return {
        "source": source,
        "version": version,
        "effects": rules,
        "signal_classes": list(classes) if isinstance(classes, list) else [],
    }


def validate_config_file(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _refuse(str(path), None, None, POLICY_RULE_SHAPE, f"unreadable config: {exc}")
    return validate_config_text(text, source=str(path))


def _step(name: str, state: str, detail: str) -> dict[str, str]:
    return {"step": name, "state": state, "detail": detail}


class HotReload:
    """Runs the drop and records the timeline the pane renders."""

    STEPS = ("validated", "registered", "flowing")

    def __init__(self) -> None:
        self.last: Optional[dict[str, Any]] = None
        self.registered_classes: list[str] = []
        self.active_config: Optional[dict[str, Any]] = None
        self.refusal: Optional[dict[str, Any]] = None

    def timeline(self) -> dict[str, Any]:
        if self.last is None:
            return {
                "state": "idle",
                "steps": [_step(s, "pending", "awaiting the drop") for s in self.STEPS],
                "registered_classes": self.registered_classes,
                "refusal": self.refusal,
                "redeploy_required": False,
            }
        return self.last

    @staticmethod
    def _held_swap(accepted: Any) -> Optional[dict[str, Any]]:
        """Did the substrate hold the swap instead of applying it?

        throughline treats a reversibility downgrade as an irreversible
        effect, so the config swap itself waits at the gate. It answers
        ``refused: false, held: true`` with an approval id — accepted, and
        not applied. Anything that reads only ``refused`` sees a success.
        """
        if not isinstance(accepted, dict):
            return None
        if not accepted.get("held"):
            return None
        return {
            "held": True,
            "policy": accepted.get("policy"),
            "approval_id": accepted.get("approval_id"),
            "effect_id": accepted.get("effect_id"),
            "downgrades": accepted.get("downgrades"),
            "held_by": "throughline",
            "message": (
                "the swap is an irreversible effect and is held for a human; "
                "the previously registered config keeps running until it is "
                "approved"
            ),
        }

    def drop(self, path: str, substrate: Any, emitted: int = 0) -> dict[str, Any]:
        """Validate, hand to the substrate, report what actually happened.

        No step is reported green on intent. 'registered' means the substrate
        accepted the swap; 'flowing' means signals of the newly registered
        class went out after it did.
        """
        steps: list[dict[str, str]] = []
        candidate: Optional[dict[str, Any]] = None
        preflight: Optional[ConfigRefusal] = None

        try:
            candidate = validate_config_file(path)
        except ConfigRefusal as refusal:
            preflight = refusal

        # Whether the substrate can rule on a config itself. A real
        # throughline can and does; the mock cannot, so offline the
        # pre-flight is the only verdict available.
        substrate_decides = getattr(substrate, "validates_config", False)

        if preflight is not None and not substrate_decides:
            self.refusal = preflight.as_dict()
            self.refusal["refused_by"] = "siren pre-flight"
            steps = [
                _step("validated", "refused", preflight.message),
                _step("registered", "skipped", "config refused; previous config still running"),
                _step("flowing", "skipped", "no class registered"),
            ]
            self.last = {
                "state": "refused",
                "steps": steps,
                "registered_classes": self.registered_classes,
                "refusal": self.refusal,
                "redeploy_required": False,
                "config_path": path,
            }
            return self.last

        if preflight is not None:
            # Hand it over anyway: throughline owns this refusal and its
            # ledger is where the refusal has to land.
            steps.append(_step("validated", "refused",
                               f"{preflight.message} (pre-flight; throughline decides)"))
        else:
            steps.append(_step("validated", "ok",
                               f"{len(candidate['effects'])} rules, "
                               f"version {candidate['version']}"))

        from .substrate import SubstrateError  # local import: keeps the cycle out of module load

        try:
            accepted = substrate.reload_config(path)
        except SubstrateError as exc:
            body = exc.body if isinstance(exc.body, dict) else {"detail": str(exc)}
            if body.get("refused"):
                # The substrate refused. Its verdict is the authoritative one.
                self.refusal = dict(body)
                self.refusal["refused_by"] = "throughline"
                state = "refused"
                detail = body.get("detail") or body.get("message") or str(exc)
            else:
                self.refusal = {"refused": True, "file": path, "policy": "substrate-unreachable",
                                "message": str(exc), "detail": str(exc),
                                "refused_by": "throughline"}
                state = "error"
                detail = str(exc)
            steps.append(_step("registered", "refused", detail))
            steps.append(_step("flowing", "skipped", "class not registered"))
            self.last = {
                "state": state,
                "steps": steps,
                "registered_classes": self.registered_classes,
                "refusal": self.refusal,
                "redeploy_required": False,
                "config_path": path,
            }
            return self.last

        held = self._held_swap(accepted)
        if held is not None:
            # throughline ACCEPTED the drop and did not apply it: the swap
            # itself is an irreversible effect now (a reversibility downgrade
            # must pass the gate), so it waits for a human exactly as a
            # dispatch does. `refused: false` is true and is not the whole
            # truth — reading only that field made siren announce "the
            # substrate accepted the swap" while the running config was
            # untouched and an approval sat pending. Green on intent, which
            # is the one thing this timeline promises never to be.
            self.refusal = None
            steps.append(_step(
                "registered", "held",
                f"{substrate.name} substrate HELD the swap at the gate "
                f"({held.get('policy') or 'held for a human'}); approval "
                f"{held.get('approval_id') or 'pending'} — the previous config "
                f"is still running",
            ))
            steps.append(_step("flowing", "skipped",
                               "no class registered: the swap is waiting for a human"))
            self.last = {
                "state": "held",
                "steps": steps,
                # Unchanged on purpose: what is registered is what is RUNNING,
                # and the held candidate is not running.
                "registered_classes": self.registered_classes,
                "refusal": None,
                "held": held,
                "redeploy_required": False,
                "substrate": substrate.name,
                "substrate_response": accepted,
                "config_path": path,
                "emitted": 0,
            }
            return self.last

        if preflight is not None:
            # The substrate accepted a config siren's pre-flight would have
            # refused. That is policy drift between the two, and it is
            # reported rather than smoothed over.
            steps.append(_step(
                "registered", "ok",
                f"{substrate.name} substrate accepted a config siren's pre-flight "
                f"refused ({preflight.policy}); policy drift — siren's copy of the "
                f"policy is stale or the substrate's has moved",
            ))
            self.refusal = None
            self.active_config = candidate
            classes = (candidate or {}).get("signal_classes") or [INCIDENT_SIGNAL_CLASS]
            self.registered_classes = list(classes)
            steps.append(_step("flowing", "ok" if emitted else "pending",
                               f"{emitted} incident signals emitted after the swap"
                               if emitted else "awaiting the next poll"))
            self.last = {
                "state": "registered",
                "steps": steps,
                "registered_classes": self.registered_classes,
                "refusal": None,
                "policy_drift": preflight.as_dict(),
                "redeploy_required": False,
                "substrate": substrate.name,
                "substrate_response": accepted,
                "config_path": path,
                "emitted": emitted,
            }
            return self.last

        self.refusal = None
        self.active_config = candidate
        classes = candidate.get("signal_classes") or [INCIDENT_SIGNAL_CLASS]
        self.registered_classes = list(classes)
        steps.append(_step("registered", "ok",
                           f"{substrate.name} substrate accepted the swap; "
                           f"classes: {', '.join(self.registered_classes)}"))
        steps.append(
            _step("flowing", "ok" if emitted else "pending",
                  f"{emitted} incident signals emitted after the swap" if emitted
                  else "awaiting the next poll")
        )
        self.last = {
            "state": "registered",
            "steps": steps,
            "registered_classes": self.registered_classes,
            "refusal": None,
            "redeploy_required": False,
            "substrate": substrate.name,
            "substrate_response": accepted,
            "config_path": path,
            "emitted": emitted,
        }
        return self.last
