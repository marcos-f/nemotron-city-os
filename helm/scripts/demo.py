#!/usr/bin/env python3
"""test://demo/offline-run — the scripted DEMO sequence, network off.

The sequence, in order:

  1. judgment    a signal is judged and a reversible effect routes itself
  2. divergence  telemetry diverges and an IRREVERSIBLE dispatch is proposed
  3. hold        the gate holds it; no timeout exists
  4. approve     a human signet subject releases it; the effect executes
  5. walk        the chain walks back effect → judgment → signal, hash-verified
  6. hot-reload  a signal class is registered without a redeploy
  7. refused     a config that would auto-execute an irreversible effect is refused
  8. agent       NemoClerk tries to approve, and is REFUSED by role, on the ledger

``--offline`` arms a socket guard: any attempt to open a network connection
during the run raises. ``--self-contained`` boots an in-process throughline
substitute (the same contract-faithful fake the tests use) so the sequence
runs on a CI runner with nothing else installed. Without it, the run goes
against the real federation on its fixed ports — which is what the recorded
evidence run does.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}


class NetworkRefused(RuntimeError):
    """The offline guard bit. That is the point of the guard."""


_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE = socket.create_connection


def arm_offline_guard(allow_loopback: bool) -> None:
    """Refuse every outbound connection, so 'offline' is proven, not asserted."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def host_of(address: Any) -> str:
        if isinstance(address, tuple) and address:
            return str(address[0])
        return str(address)

    def guard(address: Any) -> None:
        host = host_of(address)
        if allow_loopback and host in ALLOWED_HOSTS:
            return
        raise NetworkRefused(f"offline demo attempted a connection to {host}")

    def connect(self, address):  # type: ignore[no-untyped-def]
        guard(address)
        return real_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        guard(address)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        guard(address)
        return real_create(address, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]


def guard_bites() -> bool:
    """The guard is itself tested: if it does not bite, the run is not offline."""
    try:
        socket.create_connection(("example.com", 80), timeout=1)
    except NetworkRefused:
        return True
    except OSError:
        return False
    return False


class Step:
    def __init__(self, name: str, coordinate: str) -> None:
        self.name = name
        self.coordinate = coordinate
        self.ok = False
        self.detail = ""
        self.evidence: dict[str, Any] = {}

    def record(self, ok: bool, detail: str, **evidence: Any) -> "Step":
        self.ok = ok
        self.detail = detail
        self.evidence = evidence
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {self.name}: {detail}")
        return self


def set_siren_feed(mode: str, base: str = "http://127.0.0.1:8603") -> str:
    """Switch siren's live feed off for the duration of an offline run.

    A plain --offline run still performed DNS and TLS to data.seattle.gov,
    because the socket guard only fences THIS process and siren polls from
    its own. The UI labels that data as cached, so the run was both a demo
    risk and an honesty problem. siren now takes a runtime switch, and the
    demo uses it rather than hoping.
    """
    import httpx as _httpx

    try:
        reply = _httpx.post(
            f"{base}/feed/mode",
            json={"mode": mode, "substrate": "keep"},
            timeout=10,
        )
        if reply.is_success:
            body = reply.json() if reply.content else {}
            return str(body.get("guarantee") or body.get("source") or mode)
        return f"siren refused the switch: HTTP {reply.status_code}"
    except _httpx.HTTPError as exc:
        return f"siren unreachable: {type(exc).__name__}"


def run(self_contained: bool, offline: bool) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from helm.app import create_app
    from helm.config import Settings

    substrate = None
    if self_contained:
        import helm.clients as clients_module
        from fakes import FakeFederation

        substrate = FakeFederation()
        # The in-process substrate authenticates its caller exactly as :8600
        # does, so the run must hold a token here too. A self-contained demo
        # that skipped this would be easier to satisfy than the real one, and
        # would pass green on the very beat that is broken in production.
        os.environ["THROUGHLINE_CALLER_TOKEN"] = substrate.caller_token
        clients_module.httpx.request = substrate.request

    # Say up front whether this run can authenticate to the substrate at all.
    # Beat 4 — the approve — is on throughline's PRIVILEGED write surface, and
    # a run holding no token will fail there. Naming it here, as a missing
    # token, beats meeting it ten steps later dressed as a gate refusal.
    from helm.clients import read_caller_token

    caller_token = read_caller_token()
    print(
        f"caller token: {'present' if caller_token.present else 'ABSENT'} "
        f"({caller_token.source}"
        f"{'; ' + caller_token.detail if caller_token.detail else ''})"
    )
    if not caller_token.present:
        print(
            "  NOTE: throughline requires a caller token and none was "
            "configured. The approve beat will fail as an AUTHENTICATION "
            "problem, not as a gate refusal. Set THROUGHLINE_CALLER_TOKEN, or "
            "boot with scripts/up, which exports it to every child."
        )

    data_dir = ROOT / "artifacts" / "demo-data"
    settings = Settings(
        env="test",
        data_dir=str(data_dir),
        cache_dir=str(ROOT / "data" / "model-cache"),
        offline=offline,
        agent_model_url="" if offline else Settings.agent_model_url,
        fallback_model_url="" if offline else Settings.fallback_model_url,
        nvidia_api_key="",
        session_secret="demo-secret",
        # Honour THROUGHLINE_URL so a rehearsal can be pointed at a SCRATCH
        # substrate instead of the live :8600. The default is unchanged, so
        # the recorded evidence run still goes against the real federation on
        # its fixed ports — but a run whose only purpose is to check the
        # script no longer has to move the live chain to do it.
        throughline_url=os.environ.get("THROUGHLINE_URL", Settings.throughline_url),
    )
    app = create_app(settings)
    client = TestClient(app)
    signet = app.state.signet
    # The SAME session helm's mock-login route issues — `mock=True`, no
    # issuer. It used to claim provider "github" with no issuer behind it,
    # which signet correctly labels `auth_mode: unverified`: a real sign-in
    # nobody signed an assertion for. Nothing on stage is a GitHub sign-in,
    # so the record should not have said one was.
    #
    # It is also the difference between a demo that runs and one that does
    # not. throughline releases an irreversible effect only on a decision
    # whose record names an auth mode it recognises, and `unverified` is
    # deliberately the fall-through case that never qualifies. `mock` is on
    # the federation's allowlist BY EXPLICIT CHOICE (see ATTESTED_AUTH_MODES
    # in the orchestrator's scripts/lib/federation.py) and is visible in
    # /healthz, so nobody can mistake this chain for one released by
    # verified identities.
    client.cookies.set(
        "helm_session",
        signet.issue("dana@nvidia-demo.example", "mock", "dana",
                     mock=True, issuer=""),
    )

    def seed(effect_id: str, reversibility: str, description: str,
             effect_type: str = "") -> None:
        """Put a real signal and a real effect into whichever substrate is live.

        Against the real throughline the SIGNAL is posted first, so the cause
        walk has a complete chain to walk rather than a stub. ``effect_type``
        is required by the gate for anything that will be QUEUED for a human
        (i.e. anything not auto-executing reversible) — an irreversible seed
        must therefore pass one; the reversible judgment step relies on the
        gate's untyped "caller-claim" path and needs none."""
        signal_id = f"sig-{effect_id}"
        if substrate is not None:
            substrate.propose(effect_id, reversibility, description, signal_id)
            return
        import httpx as _httpx

        base = settings.throughline_url.rstrip("/")
        try:
            _httpx.post(
                f"{base}/signals",
                json={
                    "id": signal_id,
                    "class": "grid.telemetry" if reversibility == "irreversible" else "permit.document",
                    "source": "helm/demo",
                    "ingest_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "real_or_synthetic": "synthetic",
                },
                timeout=10,
            )
            payload: dict[str, Any] = {
                "id": effect_id,
                "reversibility": reversibility,
                "status": "proposed",
                "description": description,
                "signal_id": signal_id,
            }
            if effect_type:
                payload["effect_type"] = effect_type
            _httpx.post(f"{base}/effects", json=payload, timeout=10)
        except _httpx.HTTPError as exc:
            print(f"  (could not seed {effect_id}: {exc})")

    run_id = int(time.time())
    principal = client.get("/nemoclerk/runtime").json()["principal"]

    steps: list[Step] = []
    print("\nDEMO SEQUENCE\n" + "=" * 60)

    # 1 --------------------------------------------------------- judgment
    step = Step("judgment", "scenario://docket/route-ledger")
    seed(f"eff-route-{run_id}", "reversible", "route PN-2026-04471")
    overview = client.get("/overview").json()
    steps.append(
        step.record(
            overview["chain_length"] > 0,
            f"the substrate has judged and ledgered {overview['chain_length']} entries",
            chain_length=overview["chain_length"],
        )
    )

    # 2 ------------------------------------------------------- divergence
    step = Step("divergence", "scenario://breaker/divergence-rule")
    dispatch_id = f"eff-dispatch-{run_id}"
    seed(dispatch_id, "irreversible", "microgrid load-shed battery_4",
         effect_type="dispatch.load_shed")
    pending = client.get("/approvals", params={"state": "pending"}).json()
    irreversible = [
        a
        for a in pending["approvals"]
        if a["reversibility_class"] == "irreversible"
        and a["effect_id"] == dispatch_id
    ] or [
        a for a in pending["approvals"] if a["reversibility_class"] == "irreversible"
    ]
    steps.append(
        step.record(
            bool(irreversible),
            f"{len(irreversible)} irreversible dispatch proposed from a divergence",
            approvals=[a["id"] for a in irreversible],
        )
    )
    if not irreversible:
        return summarise(steps, offline)
    approval_id = irreversible[0]["id"]
    effect_id = irreversible[0]["effect_id"]

    # 3 -------------------------------------------------------------- hold
    step = Step("hold", "scenario://breaker/gate-hold")
    composed = client.get("/composed/state").json()
    steps.append(
        step.record(
            composed["assembled"] and composed["trigger"] == "gate-hold",
            "the gate holds it and the composed view assembled itself",
            trigger=composed["trigger"],
            waiting_since=composed["waiting_since"],
        )
    )

    # 8a ------------------------------------------ agent refusal, before
    step = Step("agent-refusal", "scenario://helm/agent-refusal")
    refused = client.post(
        "/mcp/call",
        json={
            "name": "approve_effect",
            "arguments": {"id": approval_id},
            "principal": principal,
        },
    )
    ledger_after = client.get("/ledger", params={"limit": 50}).json()
    refusal_rows = [r for r in ledger_after["rows"] if r["kind"] == "approval.refused"]
    steps.append(
        step.record(
            refused.status_code == 403 and bool(refusal_rows),
            f"{principal} REFUSED by role, ledgered at seq "
            f"{refusal_rows[0]['seq'] if refusal_rows else '—'}",
            status=refused.status_code,
            principal=principal,
            ledger_seq=refusal_rows[0]["seq"] if refusal_rows else None,
        )
    )

    # 4 ----------------------------------------------------------- approve
    step = Step("approve", "scenario://signet/subject-in-record")
    subjectless = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approve"}
    )
    decided = client.post(
        f"/approvals/{approval_id}/decide",
        json={
            "decision": "approve",
            "decided_by": "dana@nvidia-demo.example",
            "caller_role": "human",
            "rationale": "divergence confirmed against the last maintenance window",
        },
    )
    steps.append(
        step.record(
            subjectless.status_code == 422 and decided.status_code == 200,
            "subject-less decide refused 422; the same effect approved by "
            "dana@nvidia-demo.example",
            subjectless_status=subjectless.status_code,
            approved_status=decided.status_code,
        )
    )

    # 5 -------------------------------------------------------------- walk
    step = Step("walk", "scenario://breaker/ledger-walk")
    walk = client.get(f"/walk/{effect_id}/json").json()
    verified = bool(walk.get("hops")) and all(h["verified"] for h in walk["hops"])
    steps.append(
        step.record(
            verified,
            f"{walk.get('hop_count', 0)} hops, every digest recomputed and verified",
            hops=[h["kind"] for h in walk.get("hops", [])],
        )
    )

    # 6 -------------------------------------------------------- hot-reload
    step = Step("hot-reload", "scenario://siren/hot-reload")
    reload_result = client.post("/admin/classes/incident/reload").json()
    steps.append(
        step.record(
            bool(reload_result.get("ok")),
            reload_result.get("summary", "no reload"),
            tool=reload_result.get("chip"),
        )
    )

    # 7 ------------------------------------------------------ refused config
    step = Step("refused-config", "scenario://throughline/no-bypass")
    policy = client.get("/admin/gate-policy").json()
    steps.append(
        step.record(
            policy["disable_gate"]["available"] is False,
            "the gate-disable control is inert and says why",
            reason=policy["disable_gate"]["reason"][:80],
        )
    )

    # 8b ------------------------------------- NemoClerk says it, grounded
    step = Step("agent-explains", "scenario://helm/agent-drives")
    answer = client.post(
        "/nemoclerk/message",
        json={"message": "what's waiting for approval?", "feature_area": "helm"},
    ).json()
    steps.append(
        step.record(
            bool(answer["chips"]),
            f"grounded in {len(answer['chips'])} tool call(s); source={answer['source']}",
            chips=[c["chip"] for c in answer["chips"]],
            source=answer["source"],
            honesty_label=answer["honesty_label"],
        )
    )

    return summarise(steps, offline)


def summarise(steps: list[Step], offline: bool) -> dict[str, Any]:
    passed = [s for s in steps if s.ok]
    result = {
        "coordinate": "test://demo/offline-run",
        "offline": offline,
        "guard_bites": guard_bites() if offline else None,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": [
            {
                "name": s.name,
                "coordinate": s.coordinate,
                "ok": s.ok,
                "detail": s.detail,
                "evidence": s.evidence,
            }
            for s in steps
        ],
        "passed": len(passed),
        "total": len(steps),
        "complete": len(passed) == len(steps) == 9,
    }
    print("=" * 60)
    print(f"{len(passed)}/{len(steps)} steps passed; complete={result['complete']}")
    if offline:
        print(f"offline guard bites: {result['guard_bites']}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="arm the socket guard")
    parser.add_argument(
        "--self-contained",
        action="store_true",
        help="use an in-process substrate instead of the real ports",
    )
    parser.add_argument(
        "--out", default=str(ROOT / "artifacts" / "demo-run.json")
    )
    parser.add_argument(
        "--ledger",
        default=os.environ.get("THROUGHLINE_LEDGER", ""),
        help=(
            "the chain the scripted run should narrate. throughline serves it "
            "with --ledger / THROUGHLINE_LEDGER; passing it here records WHICH "
            "chain the evidence describes, so a run against the pinned demo "
            "ledger is not mistaken for one against the live probe-scarred one."
        ),
    )
    args = parser.parse_args()
    if args.ledger:
        os.environ["THROUGHLINE_LEDGER"] = args.ledger

    feed_before = ""
    if args.offline and not args.self_contained:
        # Fence the SIBLINGS' egress before fencing our own: the guard below
        # only covers this process.
        feed_before = set_siren_feed("offline")
        print(f"siren feed -> offline: {feed_before}")

    if args.offline:
        arm_offline_guard(allow_loopback=not args.self_contained)
        if not guard_bites():
            print("FAIL: the offline guard does not bite", file=sys.stderr)
            return 1

    try:
        result = run(args.self_contained, args.offline)
    finally:
        if feed_before:
            # restore siren to whatever its environment says, always
            import importlib

            socket.socket.connect = _REAL_CONNECT
            socket.socket.connect_ex = _REAL_CONNECT_EX
            socket.create_connection = _REAL_CREATE
            importlib.invalidate_caches()
            print(f"siren feed -> env: {set_siren_feed('env')}")
    result["siren_feed_fenced"] = bool(feed_before)
    result["ledger"] = args.ledger or "live (unpinned)"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(f"recorded to {out}")
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
