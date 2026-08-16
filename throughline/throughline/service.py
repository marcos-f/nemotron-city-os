"""Composition root: settings and the object graph they build."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .approvals import ApprovalQueue
from .config import (
    Config,
    ConfigRefusal,
    ConfigStore,
    downgrades as config_downgrades,
)
from .gate import CONFIG_DOWNGRADE_EFFECT_TYPE, Gate
from .gate import attested_auth_modes as gate_attested_auth_modes
from .ledger import Ledger
from .models import Effect, EffectProposal, Signal
from .relay import RelayGateMirror

DEFAULT_PORT = 8600


def _env_paths(name: str) -> list[Path]:
    """Parse a ``:``-separated path list from the environment."""
    raw = os.environ.get(name, "")
    return [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]


@dataclass
class Settings:
    data_dir: Path = Path(os.environ.get("THROUGHLINE_DATA_DIR", "data"))
    config_path: Path = Path(os.environ.get("THROUGHLINE_CONFIG", "config/effects.yaml"))
    port: int = int(os.environ.get("THROUGHLINE_PORT", DEFAULT_PORT))
    host: str = os.environ.get("THROUGHLINE_HOST", "127.0.0.1")

    #: Explicit ledger file. Overrides ``data_dir/ledger.jsonl`` when set, so a
    #: demo run can be pointed at a pinned, known-good chain without moving the
    #: rest of the data directory. ``--ledger`` on ``throughline serve`` and
    #: ``THROUGHLINE_LEDGER`` both land here. Unset (the default) is the live
    #: behaviour, unchanged.
    ledger_file: Optional[Path] = field(
        default_factory=lambda: (
            Path(os.environ["THROUGHLINE_LEDGER"])
            if os.environ.get("THROUGHLINE_LEDGER")
            else None
        )
    )

    #: Shared secret authenticating the CALLER on the privileged write
    #: surface — ``POST /approvals/{id}/decide`` and the ``authz.*`` /
    #: ``warrant.*`` records the permission graph is built from.
    #:
    #: There is no off switch, because the hole this closes has no safe
    #: setting. What there is instead is a bootstrap: unset, the substrate
    #: mints a random token into ``<data_dir>/caller-token`` with mode 0600
    #: at first boot. A local caller that can read the substrate's own data
    #: directory can authenticate; one that can only reach the port cannot.
    #: An operator wiring several hosts (or wanting one secret across the
    #: federation) sets ``THROUGHLINE_CALLER_TOKEN`` in every component's
    #: environment instead, and that wins.
    caller_token: Optional[str] = field(
        default_factory=lambda: os.environ.get("THROUGHLINE_CALLER_TOKEN") or None
    )

    #: Auth modes accepted as an attestation on a decision that would release
    #: an irreversible effect. ``THROUGHLINE_ATTESTED_AUTH_MODES``, ``,``-separated.
    attested_auth_modes: tuple[str, ...] = field(
        default_factory=lambda: gate_attested_auth_modes()
    )

    #: Directories ``POST /config/reload`` may load from. Defaults to the
    #: directory holding ``config_path``; ``THROUGHLINE_CONFIG_DIRS`` adds
    #: more, ``:``-separated. A reload from anywhere else is refused and
    #: ledgered — see ``config.resolve_source``.
    extra_config_dirs: list[Path] = field(
        default_factory=lambda: _env_paths("THROUGHLINE_CONFIG_DIRS")
    )

    @property
    def ledger_path(self) -> Path:
        return self.ledger_file if self.ledger_file is not None else self.data_dir / "ledger.jsonl"

    @property
    def approvals_path(self) -> Path:
        return self.data_dir / "approvals.json"

    @property
    def caller_token_path(self) -> Path:
        """Where a self-minted caller token lives. Local clients read it."""
        return self.data_dir / "caller-token"

    @property
    def caller_token_source(self) -> str:
        return "environment" if self.caller_token else "file"

    @property
    def resolved_caller_token(self) -> str:
        """The token this substrate accepts, minting one if it has none.

        Env wins. Otherwise the file is read, and created on first use with
        an unguessable value and mode 0600. Never returns empty: an empty
        required-secret is a check that passes for everybody, which is the
        failure mode this whole change exists to remove.
        """
        if self.caller_token:
            return self.caller_token
        path = self.caller_token_path
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write private from the first byte: create it 0600 rather than
        # writing it world-readable and narrowing afterwards.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        return token

    @property
    def config_allowlist(self) -> list[Path]:
        roots = [self.config_path.expanduser().parent, *self.extra_config_dirs]
        seen: list[Path] = []
        for root in roots:
            # Absolute, so an operator reading /healthz sees the real
            # directory rather than something relative to a cwd they cannot
            # see. resolve() is what the allowlist check compares against too.
            resolved = Path(root).expanduser().resolve()
            if resolved not in seen:
                seen.append(resolved)
        return seen


class Substrate:
    """Ledger + config + queue + gate, wired together."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        # Mint the caller token at boot rather than on the first privileged
        # request, so a local caller can find it the moment the port answers
        # and an operator can see it in the data directory.
        self.settings.resolved_caller_token
        self.ledger = Ledger(self.settings.ledger_path)
        self.boot_verification = self.ledger.verify()
        if not self.boot_verification["valid"]:
            # Do not repair and do not hide: the break stays in the chain, and
            # the alarm is appended so the console shows red from boot onward.
            self.ledger.append("ledger.alarm", {
                "reason": "chain failed verification at boot",
                **self.boot_verification,
            })
        self.config_store = ConfigStore(ledger=self.ledger)
        self.boot_refusal: Optional[dict[str, Any]] = None
        self._load_boot_config()
        # The queue's JSON file is a cache; the ledger is the durable record.
        self.queue = ApprovalQueue(self.settings.approvals_path, ledger=self.ledger)
        self.mirror = RelayGateMirror(ledger=self.ledger)
        #: Validated config candidates waiting on a human, keyed by the id of
        #: the effect holding them at the gate.
        self._staged_configs: dict[str, dict[str, Any]] = {}
        self.gate = Gate(
            ledger=self.ledger,
            config_store=self.config_store,
            queue=self.queue,
            mirror=self.mirror,
            executor=self._execute_effect,
            attested_modes=self.settings.attested_auth_modes,
        )

    def _load_boot_config(self) -> None:
        path = self.settings.config_path
        if not path.exists():
            return
        try:
            # Boot establishes the baseline registry, so the downgrade check
            # does not apply here: there is nothing yet to downgrade *from*.
            # The allowlist does not apply either — this path comes from the
            # operator's own environment, not from an HTTP body.
            self.config_store.reload(path)
        except ConfigRefusal as refusal:
            # Refused at boot: run with NO rules. Every effect type is then
            # unregistered, which the gate treats as irreversible. Fail closed.
            self.boot_refusal = refusal.as_dict()

    @property
    def config(self) -> Config:
        return self.config_store.current

    # ------------------------------------------------------- config downgrade

    def stage_config(self, path: str | Path) -> Config:
        """Validate a reload candidate against the allowlist. Raises ``ConfigRefusal``."""
        return self.config_store.stage(path, self.settings.config_allowlist)

    def downgrades_against_running(self, candidate: Config) -> list[dict[str, Any]]:
        return config_downgrades(self.config, candidate)

    async def hold_config_downgrade(
        self, candidate: Config, found: list[dict[str, Any]]
    ) -> Effect:
        """Propose the downgrade as an effect and let the gate hold it.

        Loosening a reversibility class is exactly the sort of act the gate
        exists for, so it goes through the gate rather than around it. The
        candidate config is held in memory and only applied if and when a
        human releases the effect.
        """
        types = ", ".join(item["effect_type"] for item in found)
        # The gate now refuses a queued effect that names no signal — this
        # one's cause is the reload candidate itself, so it is ingested as a
        # signal before the effect is proposed, exactly like any other
        # effect's cause. Neither id is invented after the fact: both are
        # derived, deterministically, from the candidate being held. The
        # digest (not the raw source path, which may contain ``/``) keeps
        # both ids safe as a single URL path segment.
        digest = hashlib.sha256(
            f"{candidate.source}:{candidate.version}".encode()
        ).hexdigest()[:16]
        signal_id = f"sig-config-reload-{digest}"
        self.ledger.append("signal.ingested", Signal(
            id=signal_id,
            **{"class": "config.reload_candidate"},
            source=candidate.source,
            real_or_synthetic="real",
            payload_ref=f"config reload candidate, version {candidate.version}",
        ).model_dump(mode="json", by_alias=True))
        effect = await self.gate.submit(EffectProposal(
            id=f"eff-config-downgrade-{digest}",
            effect_type=CONFIG_DOWNGRADE_EFFECT_TYPE,
            reversibility="irreversible",
            signal_id=signal_id,
            description=(
                f"reclassify {types} to reversible from {candidate.source} "
                "— a reversibility downgrade, held for a human"
            ),
        ))
        self._staged_configs[effect.id] = {"candidate": candidate, "downgrades": found}
        self.ledger.append("config.downgrade_held", {
            "effect_id": effect.id,
            "approval_id": effect.approval_id,
            "source": candidate.source,
            "version": candidate.version,
            "downgrades": found,
            "policy": "reversibility-downgrade-must-pass-the-gate",
            "active_source": self.config.source,
        })
        return effect

    async def _execute_effect(self, effect: Effect) -> dict[str, Any]:
        """Gate executor. Applies a released config downgrade; else a no-op."""
        staged = self._staged_configs.pop(effect.id, None)
        if staged is None:
            return {"effect_id": effect.id, "executed": True, "executor": "noop"}
        applied = self.config_store.apply(staged["candidate"])
        self.boot_refusal = None
        return {
            "effect_id": effect.id,
            "executed": True,
            "executor": "config.apply",
            "source": applied.source,
            "version": applied.version,
            "downgrades": staged["downgrades"],
        }
