"""The `siren` command line — the dataset registry, on a third surface.

Scope, stated plainly: this module implements the three ``datasets`` commands
declared in ``contracts/opencli.yaml`` and nothing else. The two older
commands in that contract (``signal incident`` and ``incident list``) were
declared before siren had any CLI at all and are still unimplemented; they are
left in the contract rather than deleted, because deleting a declared feature
to make a checker green is how a promise disappears quietly. See the
"Pre-existing CLI drift" note in README.md.

``datasets list`` and ``datasets show`` are CLIENTS of the HTTP API, so the
registry a human reads on the command line is the same object the service
serves — there is no second loader here.

``datasets validate`` deliberately runs against a FILE, not the service, so an
operator can check a registry before dropping it in and see the refusal
without anything running. It exits non-zero on a refusal: a validate that
cannot fail the build is not validation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_URL = os.environ.get(
    "SIREN_URL", f"http://127.0.0.1:{os.environ.get('SIREN_PORT', '8603')}"
)

Transport = Callable[[str, str, Optional[dict[str, Any]]], tuple[int, Any]]


def http_transport(base_url: str) -> Transport:
    def call(method: str, path: str, body: Optional[dict[str, Any]] = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            base_url.rstrip("/") + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload or b"null")
            except json.JSONDecodeError:
                return exc.code, {"detail": payload.decode(errors="replace")}
        except urllib.error.URLError as exc:
            # Unreachable is reported as unreachable, never as an empty list.
            return 0, {"detail": f"cannot reach siren at {base_url}: {exc.reason}"}

    return call


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="siren",
        description="The class that hot-loads mid-demo from one config file.",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="service base URL")
    groups = parser.add_subparsers(dest="group", required=True)

    datasets = groups.add_parser(
        "datasets", help="Dataset registry commands"
    ).add_subparsers(dest="command", required=True)

    datasets.add_parser(
        "list", help="List every dataset this component consumes, "
                     "with licence and provenance")

    show = datasets.add_parser(
        "show", help="Show one dataset's source, licence, provenance and as-of time")
    show.add_argument("--id", required=True)

    validate = datasets.add_parser(
        "validate", help="Validate a dataset registry file; "
                         "exits non-zero on a refusal")
    validate.add_argument("--path", default=None,
                          help="registry file to validate (default: the declared one)")

    return parser


def main(argv: Optional[list[str]] = None,
         transport: Optional[Transport] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.group == "datasets" and args.command == "validate":
        from throughline.config import ConfigRefusal
        from throughline.datasets import load_registry
        from throughline.datasets_api import registry_path

        path = args.path or registry_path("SIREN_DATASETS")
        try:
            registry = load_registry(path)
        except ConfigRefusal as refusal:
            _emit(refusal.as_dict())
            return 1
        _emit({"refused": False, **registry.as_dict()})
        return 0

    call = transport or http_transport(args.url)

    if args.command == "list":
        status, payload = call("GET", "/datasets", None)
    else:  # datasets show
        status, payload = call("GET", f"/datasets/{args.id}", None)

    _emit(payload)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
