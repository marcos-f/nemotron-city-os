"""The gate: class decides, ledger precedes, nothing bypasses.

Covers scenario://throughline/chain-write (writes before effects) and
scenario://throughline/no-bypass (no flag, env var or API path disables it).
"""

from __future__ import annotations

import asyncio
import re
import tokenize
import io
from pathlib import Path

import pytest

from throughline.gate import UntraceableEffectRefused
from throughline.models import EffectProposal
from throughline.service import Settings, Substrate

PACKAGE = Path(__file__).resolve().parents[1] / "throughline"


def submit(substrate: Substrate, **kwargs):
    return asyncio.run(substrate.gate.submit(EffectProposal(**kwargs)))


def _ingest_signal(substrate, signal_id: str) -> None:
    substrate.ledger.append("signal.ingested", {
        "id": signal_id, "class": "test.signal", "source": "test",
        "real_or_synthetic": "real",
    })


def test_reversible_registered_effect_executes(substrate):
    effect = submit(substrate, id="eff-notify-ping", effect_type="notify.operator", description="ping")
    assert effect.reversibility_class == "reversible"
    assert effect.status == "executed"


def test_irreversible_registered_effect_is_held(substrate):
    _ingest_signal(substrate, "sig-payment-release")
    effect = submit(
        substrate, id="eff-payment-release", effect_type="payment.release",
        signal_id="sig-payment-release", description="release funds",
    )
    assert effect.reversibility_class == "irreversible"
    assert effect.status == "queued"
    assert effect.approval_id is not None
    assert len(substrate.queue.pending()) == 1


def test_the_registry_overrides_the_callers_claim(substrate):
    """A caller calling an irreversible effect reversible does not get away with it."""
    _ingest_signal(substrate, "sig-registry-overrides")
    effect = submit(
        substrate, id="eff-registry-overrides", effect_type="payment.release",
        reversibility="reversible", signal_id="sig-registry-overrides",
        description="release funds",
    )
    assert effect.reversibility == "irreversible"
    assert effect.status == "queued"

    proposed = next(
        e for e in substrate.ledger.read() if e["type"] == "effect.proposed"
    )
    assert proposed["body"]["claimed_reversibility"] == "reversible"
    assert proposed["body"]["reversibility_class"] == "irreversible"


def test_unregistered_effect_type_is_treated_as_irreversible(substrate):
    """Fail closed: an effect the registry never heard of does not self-certify."""
    _ingest_signal(substrate, "sig-mystery-action")
    effect = submit(
        substrate, id="eff-mystery-action", effect_type="mystery.action",
        reversibility="reversible", signal_id="sig-mystery-action",
        description="mystery",
    )
    assert effect.reversibility_class == "irreversible"
    assert effect.status == "queued"


def test_effect_with_no_type_defaults_to_irreversible(substrate):
    """A proposal with no fields at all names no id, so it is refused before
    it ever gets a chance to be classified or queued. This is the genuine,
    deliberate behaviour change the traceability fix requires: an anonymous
    proposal used to be silently minted an id and queued; now it is refused
    outright and ledgered as such."""
    with pytest.raises(UntraceableEffectRefused):
        submit(substrate)


def test_ledger_is_written_before_the_effect_executes(tmp_path):
    """scenario://throughline/chain-write — the entry precedes the effect."""
    seen: list[list[str]] = []

    def executor(effect):
        # What the ledger already says at the moment the effect actually runs.
        seen.append([e["type"] for e in substrate.ledger.read()])
        return {"ran": True}

    substrate = Substrate(
        Settings(data_dir=tmp_path / "data", config_path=Path("config/effects.yaml"))
    )
    substrate.gate.executor = executor
    effect = submit(substrate, id="eff-ledger-write-order", effect_type="notify.operator")

    assert effect.status == "executed"
    assert seen, "executor never ran"
    assert "effect.proposed" in seen[0]
    assert "effect.executing" in seen[0]
    # ...and the completion is recorded only afterwards.
    assert "effect.executed" not in seen[0]
    assert "effect.executed" in [e["type"] for e in substrate.ledger.read()]
    assert substrate.ledger.verify()["valid"] is True


def test_a_held_effect_never_reaches_the_executor(tmp_path):
    ran: list[str] = []
    substrate = Substrate(
        Settings(data_dir=tmp_path / "data", config_path=Path("config/effects.yaml"))
    )
    substrate.gate.executor = lambda effect: ran.append(effect.id)
    _ingest_signal(substrate, "sig-held-never-executes")
    submit(
        substrate, id="eff-held-never-executes", effect_type="payment.release",
        signal_id="sig-held-never-executes", description="release funds",
    )
    assert ran == []


def test_release_is_the_only_path_to_execution(tmp_path):
    from throughline.models import Decision

    substrate = Substrate(
        Settings(data_dir=tmp_path / "data", config_path=Path("config/effects.yaml"))
    )
    _ingest_signal(substrate, "sig-release-only-path")
    effect = submit(
        substrate, id="eff-release-only-path", effect_type="order.cancel",
        signal_id="sig-release-only-path", description="cancel order",
    )
    approval = substrate.queue.pending()[0]

    result = asyncio.run(
        substrate.gate.decide(
            approval.id,
            Decision(decision="approve", decided_by="oidc|operator-7",
                     caller_role="human", issuer="https://git.nemotron.example.com",
                     auth_mode="oidc"),
        )
    )
    assert result["effect"]["status"] == "executed"
    assert substrate.gate.get(effect.id).status == "executed"


def test_effect_state_survives_a_restart(tmp_path):
    """Effects are rebuilt from the ledger, which is the source of truth."""
    settings = Settings(data_dir=tmp_path / "data", config_path=Path("config/effects.yaml"))
    first = Substrate(settings)
    _ingest_signal(first, "sig-survives-restart")
    effect = submit(
        first, id="eff-survives-restart", effect_type="payment.release",
        signal_id="sig-survives-restart", description="release funds",
    )

    second = Substrate(settings)
    assert second.gate.get(effect.id).status == "queued"
    assert len(second.queue.pending()) == 1


def test_no_bypass_switch_exists_in_the_package():
    """scenario://throughline/no-bypass — no flag or env var disables the gate.

    The only environment variables the package reads are operational ones —
    where to serve, where the data lives, which chain to read. None of them
    changes what the gate does with an effect; nothing named like a gate
    override exists at all.

    THROUGHLINE_LEDGER selects the chain FILE (a demo can be pointed at the
    pinned fixture); it cannot make a held effect run. THROUGHLINE_CONFIG_DIRS
    only ever *narrows* what /config/reload will accept. THROUGHLINE_CALLER_ROLE
    is a CLI-side default for a field the service requires anyway, and the
    service refuses whatever it does not recognise.

    Two of these touch the gate's policy and are named here rather than
    quietly permitted, because a reader deserves the exact shape of what an
    environment can and cannot do:

    * THROUGHLINE_CALLER_TOKEN sets the caller token instead of the
      self-minted file. It can only ever CHANGE the secret, never remove the
      requirement: there is no value of it that makes an unauthenticated
      decide succeed.
    * THROUGHLINE_ATTESTED_AUTH_MODES widens which auth modes count as an
      attestation — for a console running mock login, which would otherwise
      be unable to release anything irreversible. It is the one loosening
      switch in the package, and it is bounded: it can only name modes, an
      empty entry is discarded, and a decision that omits ``auth_mode``
      altogether therefore stays refused whatever it is set to. The widening
      shows in /healthz and in every refusal row.

    ``test_the_attestation_allowlist_cannot_readmit_a_missing_auth_mode``
    below is the enforcement of that second bullet.
    """
    allowed_env = {
        "THROUGHLINE_DATA_DIR", "THROUGHLINE_CONFIG", "THROUGHLINE_PORT",
        "THROUGHLINE_HOST", "THROUGHLINE_URL", "THROUGHLINE_LEDGER",
        "THROUGHLINE_CONFIG_DIRS", "THROUGHLINE_CALLER_ROLE",
        "THROUGHLINE_CALLER_TOKEN", "THROUGHLINE_ATTESTED_AUTH_MODES",
    }
    env_reads: set[str] = set()
    suspicious = re.compile(
        r"(disable|bypass|skip|force|override|unsafe|god|nogate)_?(gate|approval|guard|check)?",
        re.IGNORECASE,
    )
    offenders: list[str] = []

    for module in sorted(PACKAGE.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        env_reads.update(re.findall(r'environ(?:\.get)?[\[(]\s*"([^"]+)"', source))
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME and suspicious.fullmatch(token.string):
                offenders.append(f"{module.name}:{token.start[0]}: {token.string}")

    assert not offenders, "possible gate bypass: " + "; ".join(offenders)
    assert env_reads <= allowed_env, f"unexpected env switches: {env_reads - allowed_env}"


def test_gate_cannot_be_talked_out_of_holding_over_http(client):
    """No request shape gets an irreversible effect executed on submission."""
    for index, body in enumerate((
        {"effect_type": "payment.release", "status": "executed"},
        {"effect_type": "payment.release", "reversibility": "reversible"},
        {"effect_type": "payment.release", "status": "approved"},
    )):
        signal_id = f"sig-talked-out-of-holding-{index}"
        client.post("/signals", json={
            "id": signal_id, "class": "test.signal", "source": "test",
            "real_or_synthetic": "real",
        })
        body = {
            **body,
            "id": f"eff-talked-out-of-holding-{index}",
            "signal_id": signal_id,
            "description": "release funds",
        }
        response = client.post("/effects", json=body)
        assert response.status_code == 202
        assert response.json()["status"] == "queued", body
