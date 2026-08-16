"""Grid Watch: telemetry in, evidence out, dispatch held at the gate.

The order of operations is the point of the whole component:

1. a reading arrives and joins its unit's history;
2. the **deterministic rule** evaluates that history and renders its working;
3. on divergence, breaker records signal → judgment → effect on throughline;
4. breaker then **verifies the gate held the effect** — it does not assume it;
5. nothing dispatches until a decision carrying a human subject comes back,
   and then it dispatches exactly once.

There is no deadline anywhere in this file. A proposal that is never decided
waits forever, which is the correct behaviour for an irreversible effect.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Optional

from dataclasses import replace

from .judge import DEFAULT_MODEL, ModelJudge
from .models import Judgment, Proposal, Signal, TelemetryReading
from .rule import Evaluation, compute_metrics, evaluate
from .substrates import SubstrateRegistry, load_registry
from .telemetry import Reading
from .throughline import (
    DEFAULT_CALLER_ROLE,
    GateViolation,
    MockThroughlineClient,
    ThroughlineClient,
    assert_held,
)

EFFECT_TYPE = "dispatch.load_shed"
SIGNAL_CLASS = "microgrid.telemetry"
SOURCE = "breaker.microgrid.feeder-7"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProposalNotFound(KeyError):
    pass


class GridWatch:
    """The component's state: histories, evaluations, proposals, dispatches."""

    def __init__(
        self,
        client: Optional[ThroughlineClient] = None,
        registry: Optional[SubstrateRegistry] = None,
        judge: Optional[ModelJudge] = None,
    ) -> None:
        self.client: ThroughlineClient = client or MockThroughlineClient()
        self.registry = registry or load_registry()
        self._judge = judge
        # A caller-supplied judge is assumed to already match whichever
        # substrate is active at construction time (every call site that
        # injects one builds it from the same registry). Recording that id
        # here means the lazy `judge` property only rebuilds when the ACTIVE
        # substrate actually changes, never on every access.
        self._judge_substrate_id: Optional[str] = (
            self.registry.active.id if judge is not None else None
        )
        self._lock = threading.RLock()
        self.history: dict[str, list[Reading]] = {}
        self.evaluations: dict[str, Evaluation] = {}
        self.proposals: dict[str, Proposal] = {}
        self.dispatches: list[dict[str, Any]] = []
        self.gate_violations: list[str] = []
        self.abstentions: list[dict[str, Any]] = []
        self.verdicts: dict[str, Any] = {}   # unit_id -> the substrate's own verdict

        #: breaker's OWN append-only record of what happened to a proposal
        #: once it existed: created, re-observed, withdrawn. It is not
        #: throughline's ledger — breaker still posts nothing of its own
        #: consequence there beyond signal/judgment/effect — but proposal
        #: closure has nowhere else to be recorded, since breaker cannot
        #: countersign a human decision that never arrives. Entries are
        #: appended, never edited or removed.
        self.ledger: list[dict[str, Any]] = []

    @property
    def judge(self) -> ModelJudge:
        """Built lazily, from whichever substrate is ACTIVE.

        The rule path must never touch a model endpoint. Just as important:
        this must never build a client pointed at a DIFFERENT model than the
        one currently active. An earlier version of this hardcoded "dsv4" —
        so selecting nemotron would still, silently, place calls to DeepSeek
        while the judgment record claimed nemotron judged. That is exactly
        the quiet-substitution defect this module exists to prevent.
        """
        substrate = self.registry.active
        if self._judge is None or self._judge_substrate_id != substrate.id:
            self._judge = ModelJudge(
                endpoint=substrate.endpoint or ModelJudge.__init__.__defaults__[0],
                model=substrate.model or DEFAULT_MODEL,
            )
            self._judge_substrate_id = substrate.id
        return self._judge

    # ---------------------------------------------------------------- intake

    def ingest(self, reading: TelemetryReading) -> dict[str, Any]:
        """Take one reading, evaluate the rule, propose if it diverged."""
        record = Reading(
            unit_id=reading.unit_id,
            tick=reading.tick,
            ts=(reading.ts or _now()).isoformat(),
            soc_pct=reading.soc_pct,
            temp_c=reading.temp_c,
            charge_current_a=reading.charge_current_a,
            voltage_v=reading.voltage_v if reading.voltage_v is not None else 48.0,
            feeder=reading.feeder or "feeder-7",
        )
        with self._lock:
            history = self.history.setdefault(record.unit_id, [])
            history.append(record)
            evaluation = self._judge_window(history)
            self.evaluations[record.unit_id] = evaluation

            proposal = None
            if evaluation.diverged:
                proposal = self._observe_divergence(evaluation)
            else:
                self._maybe_resolve(record.unit_id, evaluation)

        return {
            "reading": record.as_dict(),
            "evaluation": evaluation.as_dict(),
            "proposal": proposal.model_dump(mode="json") if proposal else None,
        }

    def _judge_window(self, history: list[Reading]) -> Evaluation:
        """Evaluate the window with whichever substrate is active.

        The rule always runs: its three numbers are the evidence either way, and
        they cost nothing. When a model substrate is active it is asked for the
        VERDICT — but only once the window is full and at least one check has
        tripped. Asking a remote model about nine healthy units every tick would
        be a demo that takes an hour and a bill for nothing.
        """
        evaluation = evaluate(history)
        substrate = self.registry.active
        if substrate.kind != "model-endpoint":
            return evaluation

        metrics = compute_metrics(history)
        if metrics is None or not any(check.passed for check in evaluation.checks):
            # Nothing interesting yet; the model is not consulted, and the
            # evidence says the rule's own verdict rather than implying one.
            return evaluation

        verdict = self.judge.judge(metrics)
        if verdict.abstained:
            self.abstentions.append({
                "unit_id": metrics.unit_id, "tick": metrics.tick,
                "substrate": substrate.id, "reason": verdict.rationale,
            })
            return replace(
                evaluation,
                diverged=False,
                attribution=f"{substrate.id} ABSTAINED — {verdict.rationale}",
                reason=f"{substrate.id} abstained: {verdict.rationale}",
            )

        self.verdicts[metrics.unit_id] = verdict
        source = "cached" if verdict.cached else "live"
        return replace(
            evaluation,
            diverged=verdict.diverged,
            attribution=(f"judged by {verdict.model} @ {verdict.endpoint} "
                         f"({source}, confidence {verdict.confidence})"),
            reason=verdict.rationale,
        )

    def _open_proposal_for(self, unit_id: str) -> Optional[Proposal]:
        """One open proposal per unit: a diverging battery is one event."""
        for proposal in self.proposals.values():
            if proposal.unit_id == unit_id and proposal.status in (
                "proposed", "waiting_at_gate"
            ):
                return proposal
        return None

    # ---------------------------------------------------------- idempotency

    @staticmethod
    def _idempotency_key(evaluation: Evaluation) -> str:
        """``unit:tick:kind`` — the identity of ONE divergence event.

        Re-observing the same anomaly for the same unit at the same tick
        (a retried POST, a re-driven fixture, a process that lost its
        in-memory proposal state and is seeing the same window again) derives
        this same key every time. That is deliberate: it is what makes a
        repeat observation update the existing proposal instead of minting a
        new signal/judgment/effect/proposal quadruple on every retry — which
        is exactly how a queue that never drains gets built, one near-
        identical "Load-shed dispatch for battery_4" at a time.
        """
        kind = evaluation.divergence_type or "unknown"
        return f"{evaluation.unit_id}:{evaluation.tick}:{kind}"

    @staticmethod
    def _stable_id(prefix: str, key: str) -> str:
        """A deterministic id from an idempotency key — no random stamp.

        The old scheme (``uuid.uuid4().hex[:8]``) minted a fresh id on every
        divergence event, even a repeat of one already seen. Two calls with
        the same key must produce the same id, so that re-posting a repeat
        observation lands on the SAME signal/judgment/effect/proposal rather
        than a new one throughline has never seen before.
        """
        return f"{prefix}-{key.replace(':', '-')}"

    def _proposal_id_for(self, key: str) -> str:
        return self._stable_id("prop", key)

    # -------------------------------------------------------------- proposal

    def _observe_divergence(self, evaluation: Evaluation) -> Optional[Proposal]:
        """Route one diverging window: update, suppress, or propose.

        Exactly one of three things happens:

        1. This (unit, tick, kind) was already proposed — a repeat
           observation of the SAME anomaly — so the existing proposal is
           refreshed and returned; nothing new is posted anywhere.
        2. A DIFFERENT tick's proposal is already open for this unit — one
           diverging battery is one event — so nothing is proposed.
        3. Neither: this is a genuinely new divergence event, and it is
           proposed.
        """
        key = self._idempotency_key(evaluation)
        existing = self.proposals.get(self._proposal_id_for(key))
        if existing is not None:
            return self._reobserve(existing, evaluation, key)
        if self._open_proposal_for(evaluation.unit_id) is not None:
            return None
        return self._propose(evaluation, key)

    def _append_ledger(self, kind: str, body: dict[str, Any]) -> None:
        """Append one immutable entry. Existing entries are never edited."""
        self.ledger.append({
            "seq": len(self.ledger) + 1, "type": kind,
            "ts": _now().isoformat(), "body": body,
        })

    def _reobserve(
        self, proposal: Proposal, evaluation: Evaluation, key: str
    ) -> Proposal:
        """A repeat observation of an anomaly this proposal already covers.

        The proposal's evidence is refreshed to the latest working — an
        operator reading it should see the current numbers, not the first
        tick's — but nothing is re-posted to the substrate: the signal,
        judgment and effect are unchanged, and re-posting them is exactly
        what built the duplicate flood in the first place.
        """
        with self._lock:
            proposal.evidence = evaluation.render_evidence()
            proposal.checks = [c for c in evaluation.as_dict()["checks"]]
            proposal.magnitude = evaluation.magnitude
            proposal.idempotency_key = key
            self.proposals[proposal.id] = proposal
            self._append_ledger("proposal.reobserved", {
                "proposal_id": proposal.id, "unit_id": proposal.unit_id,
                "tick": evaluation.tick, "idempotency_key": key,
            })
        return proposal

    def _maybe_resolve(self, unit_id: str, evaluation: Evaluation) -> None:
        """A window that stopped diverging closes its unit's open proposal.

        Today a proposal otherwise leaves ``pending`` only via a human
        decision at the gate — and if that decision never arrives, neither
        does the exit. This is the other exit: the condition itself cleared,
        so there is nothing left to load-shed for, and the proposal is
        withdrawn with the reason recorded rather than left to wait forever
        for a human who has nothing left to decide.
        """
        proposal = self._open_proposal_for(unit_id)
        if proposal is None:
            return
        # A recovery is only real if it was observed AFTER the tick that
        # raised the proposal — telemetry ticks are monotonic per unit, so a
        # non-diverging window at or before that tick is not a recovery, it
        # is a caller re-submitting old or out-of-order data (a re-driven
        # fixture, a retried batch). Closing on that would be exactly the
        # kind of false resolution that reopens the proposal a moment later
        # and starts minting fresh ones — the same flood this fix exists to
        # stop, from the other direction.
        if (proposal.tick is not None and evaluation.tick is not None
                and evaluation.tick <= proposal.tick):
            return
        with self._lock:
            reason = (
                f"divergence cleared at tick {evaluation.tick}: "
                f"{evaluation.reason or 'no check still fails'}"
            )
            proposal.status = "withdrawn"
            proposal.closed_reason = reason
            proposal.closed_at = _now()
            self.proposals[proposal.id] = proposal
            self._append_ledger("proposal.withdrawn", {
                "proposal_id": proposal.id, "unit_id": unit_id,
                "tick": evaluation.tick, "reason": reason,
            })

    def _propose(self, evaluation: Evaluation, key: str) -> Proposal:
        substrate = self.registry.active
        stamp = key

        signal = Signal(
            id=self._stable_id("sig-breaker", stamp),
            **{"class": SIGNAL_CLASS},
            source=SOURCE,
            real_or_synthetic="synthetic",
            payload_ref=f"breaker://telemetry/{evaluation.unit_id}/tick/{evaluation.tick}",
            staleness="PT0S",
        )
        recorded_signal = self.client.post_signal(
            signal.model_dump(mode="json", by_alias=True)
        )

        judgment = Judgment(
            id=self._stable_id("jud-breaker", stamp),
            signal_id=recorded_signal["id"],
            finding=(f"{evaluation.unit_id} diverged: soc_delta "
                     f"{evaluation.soc_delta}%, temp_slope {evaluation.temp_slope}°C/10m, "
                     f"charge current at {evaluation.charge_ratio} of baseline"),
            # A deterministic rule either fired or it did not; there is no
            # probability to report, and inventing one would be dishonest. A
            # model substrate reports the confidence the model actually returned.
            confidence=self._confidence_for(substrate, evaluation.unit_id),
            citations=(
                [check.render() for check in evaluation.checks]
                + ([f"verdict: {evaluation.attribution}"] if evaluation.attribution else [])
            ),
            abstained=False,
            substrate=substrate.id,
            substrate_label=substrate.label,
        )
        recorded_judgment = self.client.post_judgment(judgment.model_dump(mode="json"))

        description = (
            f"Load-shed dispatch for {evaluation.unit_id} "
            f"(soc_delta {evaluation.soc_delta}%, tick {evaluation.tick})"
        )
        effect = self.client.post_effect({
            "id": self._stable_id("eff-breaker", stamp),
            "effect_type": EFFECT_TYPE,
            "reversibility": "irreversible",
            "status": "proposed",
            "signal_id": recorded_signal["id"],
            "judgment_id": recorded_judgment["id"],
            "description": description,
        })

        proposal = Proposal(
            id=self._proposal_id_for(key),
            divergence_type=evaluation.divergence_type or "soc",
            status="proposed",
            magnitude=evaluation.magnitude,
            unit_id=evaluation.unit_id,
            tick=evaluation.tick,
            description=description,
            evidence=evaluation.render_evidence(),
            checks=[c for c in evaluation.as_dict()["checks"]],
            substrate=substrate.id,
            substrate_label=substrate.label,
            signal_id=recorded_signal["id"],
            judgment_id=recorded_judgment["id"],
            effect_id=effect["id"],
            gate_mode=getattr(self.client, "mode", "unknown"),
            idempotency_key=key,
        )

        try:
            assert_held(effect, proposal.gate_mode or "unknown")
        except GateViolation as violation:
            # The gate did not hold an irreversible effect. breaker does not
            # dispatch, and says so loudly rather than degrading into acting.
            self.gate_violations.append(str(violation))
            proposal.status = "proposed"
            self.proposals[proposal.id] = proposal
            raise

        proposal.status = "waiting_at_gate"
        proposal.approval_id = effect.get("approval_id") or self._find_approval(effect["id"])
        proposal.held_since = _now()
        self.proposals[proposal.id] = proposal
        self._append_ledger("proposal.created", {
            "proposal_id": proposal.id, "unit_id": proposal.unit_id,
            "tick": proposal.tick, "idempotency_key": key,
            "effect_id": proposal.effect_id,
        })
        return proposal

    def _confidence_for(self, substrate, unit_id: str) -> float:
        if substrate.kind == "deterministic-rule":
            return 1.0
        verdict = self.verdicts.get(unit_id)
        return float(verdict.confidence) if verdict is not None else 0.0

    def _find_approval(self, effect_id: str) -> Optional[str]:
        for approval in self.client.approvals():
            if approval.get("effect_id") == effect_id:
                return approval["id"]
        return None

    # --------------------------------------------------------------- release

    def get(self, proposal_id: str) -> Proposal:
        if proposal_id not in self.proposals:
            raise ProposalNotFound(proposal_id)
        return self.proposals[proposal_id]

    def refresh(self, proposal_id: str) -> Proposal:
        """Ask the substrate what became of the effect, and act on it once."""
        proposal = self.get(proposal_id)
        if not proposal.effect_id:
            return proposal
        effect = self.client.get_effect(proposal.effect_id)
        return self._apply_effect_state(proposal, effect)

    def decide(
        self, proposal_id: str, decision: str, decided_by: str,
        rationale: Optional[str] = None,
        caller_role: str = DEFAULT_CALLER_ROLE,
        auth_mode: str = "", issuer: str = "",
    ) -> Proposal:
        """Relay an identified decision to the gate. The gate is the authority.

        ``caller_role`` is relayed, not decided here. breaker declares what it
        was told and lets the gate apply its allowlist — an agent claiming to
        release an irreversible load shed must be refused *on the ledger*,
        which cannot happen if breaker filters it out first.

        ``auth_mode``/``issuer`` are relayed on exactly the same terms. They
        are the attestation the gate requires before releasing an IRREVERSIBLE
        effect: which authority authenticated the approver. breaker has no
        identity provider of its own, so it relays what it was told and never
        manufactures a default — a fabricated ``oidc`` here would forge the
        one claim the gate is asking for. Told nothing, breaker sends nothing
        and the gate refuses on the record.
        """
        proposal = self.get(proposal_id)
        if not proposal.approval_id:
            raise ProposalNotFound(f"{proposal_id} has no approval at the gate")
        if not decided_by:
            raise ValueError("decided_by (the approver's subject) is required")

        outcome = self.client.decide(
            proposal.approval_id, decision, decided_by, rationale,
            caller_role=caller_role, auth_mode=auth_mode, issuer=issuer)
        approval = outcome.get("approval", {})
        proposal.decided_by = approval.get("decided_by", decided_by)
        proposal.decided_at = _now()
        return self._apply_effect_state(proposal, outcome.get("effect", {}))

    def _apply_effect_state(self, proposal: Proposal, effect: dict[str, Any]) -> Proposal:
        status = effect.get("status")
        if status == "executed":
            proposal.status = "approved"
            self._dispatch_once(proposal)
        elif status == "rejected":
            proposal.status = "rejected"
        elif status in ("queued", "waiting_at_gate"):
            proposal.status = "waiting_at_gate"
        with self._lock:
            self.proposals[proposal.id] = proposal
        return proposal

    def _dispatch_once(self, proposal: Proposal) -> None:
        """Execute the load shed exactly once, however often we are told."""
        with self._lock:
            if proposal.execution_count:
                return
            proposal.execution_count = 1
            proposal.executed_at = _now()
            self.dispatches.append({
                "proposal_id": proposal.id,
                "unit_id": proposal.unit_id,
                "effect_id": proposal.effect_id,
                "executed_at": proposal.executed_at.isoformat(),
                "released_by": proposal.decided_by,
                "action": "load_shed",
            })

    # ------------------------------------------------------------------ walk

    def walk(self, proposal_id: str) -> dict[str, Any]:
        """effect → judgment → signal, as the substrate records it."""
        proposal = self.get(proposal_id)
        if not proposal.effect_id:
            return {"hops": [], "hop_count": 0, "complete": False}
        walk = self.client.walk(proposal.effect_id)
        walk["proposal_id"] = proposal.id
        walk["ids"] = {
            "effect": proposal.effect_id,
            "judgment": proposal.judgment_id,
            "signal": proposal.signal_id,
        }
        return walk

    # ------------------------------------------------------------------ read

    def all_proposals(self) -> list[Proposal]:
        return list(self.proposals.values())

    def evaluation_for(self, unit_id: str) -> Optional[Evaluation]:
        return self.evaluations.get(unit_id)
