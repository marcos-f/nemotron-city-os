"""Test fixtures, and the isolation that makes the default suite safe to run.

OFFLINE_MODE is set before siren is imported anywhere: importing
``siren.service`` builds the module-level app, and a test suite that reaches
the real 911 feed on import is a test suite that fails on a train.
Tests that want a live poll say so explicitly by patching ``fetch_live``.

THE DEFAULT SUITE NEVER TOUCHES THE LIVE CHAIN, for the same reason and by the
same mechanism. ``tests/test_integration_throughline.py`` probes
``THROUGHLINE_URL`` (default :8600) and un-skips itself when anything answers,
so on any machine with the federation up a plain ``pytest`` posted hot-reloads
to the real throughline and appended to its real, permanent, append-only
ledger — the chain that will be shown to judges, and that already carries ~900
entries nothing intended to write. Measured on a review machine: a plain
``pytest tests`` grew the live chain by five entries, including a
``config.refused`` row at seq ~2553.

So ``THROUGHLINE_URL`` is pointed at an isolated scratch address and
``SIREN_DATA_DIR`` at a scratch directory, before siren is imported. Running
against a real substrate is still fully supported; it is just no longer
something that happens to you:

    SIREN_LIVE_SUBSTRATE=1 pytest tests/test_integration_throughline.py
    pytest --live-substrate tests/test_integration_throughline.py

``tests/test_ledger_isolation.py`` fails the suite if this stops working.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile

#: Env var / CLI flag that opts a run in to a real substrate. Anything else —
#: including "the gate happened to be running" — gets the isolated scratch one.
LIVE_SUBSTRATE_ENV = "SIREN_LIVE_SUBSTRATE"
LIVE_SUBSTRATE_FLAG = "--live-substrate"

#: A loopback port nothing listens on (RFC 6335 "discard"). A probe against it
#: fails fast rather than hanging, and it can never be a throughline.
ISOLATED_THROUGHLINE_URL = "http://127.0.0.1:9"

#: The address the live federation runs on. Named so the guard can recognise it.
LIVE_THROUGHLINE_URL = "http://127.0.0.1:8600"


def live_substrate_requested() -> bool:
    """True only on an explicit opt-in. Read from argv as well as the
    environment because this is decided before pytest parses its options."""
    return (
        os.environ.get(LIVE_SUBSTRATE_ENV) == "1"
        or LIVE_SUBSTRATE_FLAG in sys.argv
    )


LIVE_SUBSTRATE = live_substrate_requested()

os.environ.setdefault("OFFLINE_MODE", "1")

if LIVE_SUBSTRATE:
    os.environ.setdefault("SIREN_SUBSTRATE", "mock")
else:
    # Overwrite, do not setdefault: an inherited SIREN_SUBSTRATE=real or a
    # THROUGHLINE_URL pointing at the live gate is exactly the accident being
    # prevented, and an inherited value is how it arrives.
    os.environ["SIREN_SUBSTRATE"] = "mock"
    os.environ["THROUGHLINE_URL"] = ISOLATED_THROUGHLINE_URL
    #: A scratch snapshot directory for the whole session. ``SIREN_DATA_DIR``
    #: otherwise defaults to ``data`` relative to the cwd — the repo's real
    #: snapshot store — so the suite wrote over shipped data as well.
    ISOLATED_DATA_DIR = tempfile.mkdtemp(prefix="siren-test-data-")
    os.environ["SIREN_DATA_DIR"] = ISOLATED_DATA_DIR

import pytest  # noqa: E402

from siren import feed  # noqa: E402


def pytest_addoption(parser) -> None:
    parser.addoption(
        LIVE_SUBSTRATE_FLAG,
        action="store_true",
        default=False,
        help=("Run against a REAL throughline, writing to its REAL ledger. "
              "Off by default: the live chain is permanent and append-only."),
    )


def pytest_report_header(config) -> str:
    if LIVE_SUBSTRATE:
        return ("siren: LIVE SUBSTRATE opted in — this run MAY write to the "
                f"real ledger at {os.environ.get('THROUGHLINE_URL')}")
    return (f"siren: isolated substrate (mock, {ISOLATED_THROUGHLINE_URL}) and "
            f"scratch data dir; pass {LIVE_SUBSTRATE_FLAG} for a real ledger")


SAMPLE_ROWS = [
    {
        "incident_number": "F260115303",
        "type": "Activated CO Detector",
        "datetime": "2026-08-15T20:31:00.000",
        "latitude": "47.583666",
        "longitude": "-122.305304",
        "address": "2119 S Walker St",
    },
    {
        "incident_number": "F260115302",
        "type": "Brush Fire",
        "datetime": "2026-08-15T20:29:00.000",
        "latitude": "47.591454",
        "longitude": "-122.317281",
        "address": "1311 12th Ave S",
    },
    {
        # No coordinates: unmappable, and dropped rather than placed at 0,0.
        "incident_number": "F260115301",
        "type": "Aid Response",
        "datetime": "2026-08-15T20:20:00.000",
        "address": "Undisclosed",
    },
]


@pytest.fixture(autouse=True)
def _clear_runtime_offline_override():
    """The offline switch is process-global; a test must not leak it.

    ``feed.set_offline`` is deliberately global state — it is a property of
    the running service, not of one request — so every test starts and ends
    with it cleared and the environment back in charge.
    """
    feed.set_offline(None)
    yield
    feed.set_offline(None)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A private snapshot directory per test."""
    monkeypatch.setenv("SIREN_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    monkeypatch.setenv("SIREN_SUBSTRATE", "mock")


@pytest.fixture
def seeded_snapshot(data_dir):
    """A snapshot on disk, taken at a known instant."""
    incidents = [i for i in (feed.record_to_incident(r) for r in SAMPLE_ROWS) if i]
    snapshot = feed.Snapshot(as_of="2026-08-15T14:42:00Z", incidents=incidents,
                             source="seattle.fire.911")
    feed.write_snapshot(snapshot)
    return snapshot


@pytest.fixture
def no_network(monkeypatch):
    """A socket guard: any attempt to open a connection fails the test.

    This is how 'offline' is proven rather than asserted. It guards the
    connect calls, not socket construction, so in-process ASGI machinery
    keeps working while anything reaching for the network does not.
    """
    opened: list = []

    def forbid(*args, **kwargs):
        opened.append(args)
        raise AssertionError(f"network access attempted in offline mode: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    return opened


@pytest.fixture
def client(offline, data_dir, seeded_snapshot):
    """A TestClient over a freshly built app, offline, on a mock substrate."""
    from fastapi.testclient import TestClient

    from siren.service import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def write_config(path, *, auto_execute_irreversible: bool) -> str:
    """A minimal registry, valid or deliberately not."""
    document = {
        "version": 1,
        "signal_classes": ["fire.incident"],
        "effects": [
            {
                "id": "incident_notify",
                "effect_type": "incident.notify",
                "reversibility_class": "reversible",
                "auto_execute": True,
                "description": "Post an incident to the console.",
            },
            {
                "id": "dispatch_units",
                "effect_type": "dispatch.units",
                "reversibility_class": "irreversible",
                "auto_execute": auto_execute_irreversible,
                "description": "Commit apparatus to an address.",
            },
        ],
    }
    text = json.dumps(document, indent=2)  # JSON is valid YAML
    path.write_text(text, encoding="utf-8")
    return str(path)
