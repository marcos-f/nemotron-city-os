"""The `breaker` command line, implementing contracts/opencli.yaml.

`stream` is the demo driver: it pushes the spec-03 fixture through the service
tick by tick at whatever pace you ask for, printing the rule's evidence as it
goes and stopping the moment the dispatch proposal lands at the gate.

Like the service, this is a client — the rule, the gate and the hold all live
behind the API, so there is no CLI path that dispatches anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_URL = os.environ.get(
    "BREAKER_URL", f"http://127.0.0.1:{os.environ.get('BREAKER_PORT', '8602')}"
)

Transport = Callable[[str, str, Optional[dict[str, Any]]], "tuple[int, Any]"]


def http_transport(base_url: str) -> Transport:
    def call(method: str, path: str, body: Optional[dict[str, Any]] = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload or b"null")
            except json.JSONDecodeError:
                return exc.code, {"detail": payload.decode(errors="replace")}
        except urllib.error.URLError as exc:
            return 0, {"detail": f"cannot reach breaker at {base_url}: {exc.reason}"}

    return call


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="breaker", description="A breaker trips and only a human resets it.")
    parser.add_argument("--url", default=DEFAULT_URL)
    groups = parser.add_subparsers(dest="group", required=True)

    serve = groups.add_parser("serve", help="Run the service")
    serve.add_argument("--host", default=os.environ.get("BREAKER_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("BREAKER_PORT", 8602)))

    signal = groups.add_parser("signal", help="Signal commands").add_subparsers(
        dest="command", required=True)
    telemetry = signal.add_parser("telemetry", help="Ingest microgrid telemetry as a signal")
    telemetry.add_argument("--source", default="breaker.microgrid.feeder-7")
    telemetry.add_argument("--payload-ref")
    telemetry.add_argument("--unit")
    telemetry.add_argument("--tick", type=int)
    telemetry.add_argument("--soc-pct", type=float)
    telemetry.add_argument("--temp-c", type=float)
    telemetry.add_argument("--charge-current-a", type=float)

    proposal = groups.add_parser("proposal", help="Proposal commands").add_subparsers(
        dest="command", required=True)
    get = proposal.add_parser("get", help="Get a dispatch proposal by id")
    get.add_argument("--id", required=True)
    proposal.add_parser("list", help="List dispatch proposals")
    decide = proposal.add_parser("decide", help="Relay an identified decision to the gate")
    decide.add_argument("--id", required=True)
    decide.add_argument("--decision", choices=("approve", "reject"), required=True)
    decide.add_argument("--decided-by", required=True, help="OIDC subject of the approver")
    decide.add_argument("--rationale")
    decide.add_argument(
        "--caller-role", choices=("human", "agent"), default="human",
        help="Who is deciding. The gate requires it and refuses an omission; "
             "declare 'agent' honestly and be refused on the ledger.",
    )
    walk = proposal.add_parser("walk", help="Walk effect → judgment → signal")
    walk.add_argument("--id", required=True)

    stream = groups.add_parser("stream", help="Stream the spec-03 fixture into breaker")
    stream.add_argument("--interval", type=float, default=0.0,
                        help="seconds between ticks (0 = as fast as possible)")
    stream.add_argument("--ticks", type=int, default=40)
    stream.add_argument("--unit", default=None, help="stream one unit only")
    stream.add_argument("--quiet", action="store_true")

    evidence = groups.add_parser("evidence", help="Show the rule's latest working")
    evidence.add_argument("--unit", default="battery_4")

    groups.add_parser("substrates", help="List judgment substrates")

    dataset = groups.add_parser("dataset", help="Dataset registry commands").add_subparsers(
        dest="command", required=True)
    dataset.add_parser("list", help="List every dataset this component consumes")
    show = dataset.add_parser("show", help="Show one dataset's provenance and licence")
    show.add_argument("--id", required=True)
    validate = dataset.add_parser(
        "validate", help="Validate a dataset registry file without loading it")
    validate.add_argument("--path", default=None,
                          help="registry file to validate (default: the running one)")

    return parser


def _stream(args, call: Transport) -> int:
    """Push the fixture through the service, tick by tick."""
    status, fixture = call("GET", f"/telemetry/fixture?ticks={args.ticks}", None)
    if status != 200:
        _emit(fixture)
        return 1

    records = fixture["records"]
    if args.unit:
        records = [r for r in records if r["unit_id"] == args.unit]

    proposal = None
    last_tick = None
    for record in records:
        if args.interval and record["tick"] != last_tick and last_tick is not None:
            time.sleep(args.interval)
        last_tick = record["tick"]

        status, outcome = call("POST", "/signals/telemetry", {
            "class": "microgrid.telemetry",
            "source": "breaker.microgrid.feeder-7",
            "real_or_synthetic": "synthetic",
            "reading": record,
        })
        if status != 201:
            _emit(outcome)
            return 1

        evaluation = outcome.get("evaluation") or {}
        if not args.quiet and record["unit_id"] == (args.unit or "battery_4"):
            verdict = "DIVERGENCE" if evaluation.get("diverged") else "ok"
            print(f"tick {record['tick']:>2}  {record['unit_id']}  "
                  f"soc={record['soc_pct']:>6.2f}  temp={record['temp_c']:>5.2f}  "
                  f"i={record['charge_current_a']:>5.2f}  {verdict}")

        if outcome.get("proposal"):
            proposal = outcome["proposal"]
            print()
            print(proposal["evidence"])
            print()
            print(f"proposal {proposal['id']}: {proposal['status'].upper()} "
                  f"(gate: {proposal['gate_mode']}) — no timeout exists")
            break

    if proposal is None:
        print("no divergence in the streamed window")
        return 1
    return 0


def main(argv: Optional[list[str]] = None, transport: Optional[Transport] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.group == "serve":
        import uvicorn

        from .app import create_app
        from .service import Settings

        settings = Settings(host=args.host, port=args.port)
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return 0

    # `dataset validate` runs against a FILE, not the service, so an operator
    # can check a registry before dropping it in — and so a refusal is visible
    # without a running breaker. It exits non-zero on a refusal: a validate that
    # cannot fail the build is not validation.
    if args.group == "dataset" and args.command == "validate":
        from throughline.config import ConfigRefusal
        from throughline.datasets import load_registry

        from .app import datasets_path

        path = args.path or datasets_path()
        try:
            registry = load_registry(path)
        except ConfigRefusal as refusal:
            _emit(refusal.as_dict())
            return 1
        _emit({"refused": False, **registry.as_dict()})
        return 0

    call = transport or http_transport(args.url)

    if args.group == "stream":
        return _stream(args, call)

    if args.group == "signal":
        body: dict[str, Any] = {
            "class": "microgrid.telemetry",
            "source": args.source,
            "real_or_synthetic": "synthetic",
        }
        if args.payload_ref:
            body["payload_ref"] = args.payload_ref
        if args.unit:
            body["reading"] = {
                "unit_id": args.unit, "tick": args.tick or 0,
                "soc_pct": args.soc_pct if args.soc_pct is not None else 80.0,
                "temp_c": args.temp_c if args.temp_c is not None else 28.0,
                "charge_current_a": (args.charge_current_a
                                     if args.charge_current_a is not None else 40.0),
            }
        status, payload = call("POST", "/signals/telemetry", body)

    elif args.group == "proposal" and args.command == "get":
        status, payload = call("GET", f"/proposals/{args.id}", None)
    elif args.group == "proposal" and args.command == "list":
        status, payload = call("GET", "/proposals", None)
    elif args.group == "proposal" and args.command == "walk":
        status, payload = call("GET", f"/proposals/{args.id}/walk", None)
    elif args.group == "proposal" and args.command == "decide":
        status, payload = call("POST", f"/proposals/{args.id}/decide", {
            "decision": args.decision, "decided_by": args.decided_by,
            "rationale": args.rationale, "caller_role": args.caller_role,
        })
    elif args.group == "dataset" and args.command == "list":
        status, payload = call("GET", "/datasets", None)
    elif args.group == "dataset" and args.command == "show":
        status, payload = call("GET", f"/datasets/{args.id}", None)
    elif args.group == "evidence":
        status, payload = call("GET", f"/evidence/{args.unit}", None)
        if status == 200 and payload.get("evidence"):
            print(payload["evidence"])
            return 0
    else:  # substrates
        status, payload = call("GET", "/substrates", None)

    _emit(payload)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
