"""The default test suite may not touch the live, permanent chain.

throughline's ledger is append-only and it is what the judges will be shown.
``tests/test_integration_throughline.py`` un-skips itself whenever anything
answers on ``THROUGHLINE_URL``, so on any machine with the federation up a
plain ``pytest`` posted hot-reloads to the real gate and wrote to that chain —
a ``config.refused`` row landed at seq ~2553 during a review run, and a
measured plain run of this suite grew the live chain by five entries.

``tests/conftest.py`` pins the default run to an isolated substrate and a
scratch data directory. These are the tests that fail if that stops being
true. Running against a real substrate is still supported and is one flag
away — see ``LIVE_SUBSTRATE_FLAG``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from siren import feed, substrate
from tests.conftest import (
    ISOLATED_THROUGHLINE_URL,
    LIVE_SUBSTRATE,
    LIVE_SUBSTRATE_ENV,
    LIVE_SUBSTRATE_FLAG,
    LIVE_THROUGHLINE_URL,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    LIVE_SUBSTRATE,
    reason=(f"{LIVE_SUBSTRATE_ENV}=1 / {LIVE_SUBSTRATE_FLAG}: the operator has "
            "explicitly opted this run in to a real substrate"),
)


def test_the_default_run_is_pinned_to_an_isolated_substrate():
    assert os.environ["SIREN_SUBSTRATE"] == "mock"
    assert os.environ["THROUGHLINE_URL"] == ISOLATED_THROUGHLINE_URL


def test_the_configured_gate_is_never_the_live_one():
    """A URL check, not a mode check: the mode is overridden per-fixture, and
    ``test_integration_throughline.py`` reads the URL directly."""
    configured = substrate.throughline_url().rstrip("/")
    assert configured != LIVE_THROUGHLINE_URL.rstrip("/"), (
        "the default test configuration resolves to the LIVE throughline. "
        "Every entry this suite writes lands in a permanent, append-only chain "
        f"that will be shown to judges. Opt in with {LIVE_SUBSTRATE_FLAG} if "
        "that is genuinely what you want."
    )
    assert configured == ISOLATED_THROUGHLINE_URL
    assert substrate.substrate_choice() == "mock"


def test_the_snapshot_store_is_never_the_repository_data_directory():
    """The other permanent thing a test run could overwrite: the shipped
    snapshot. ``SIREN_DATA_DIR`` defaults to ``data`` relative to the cwd."""
    configured = feed.data_dir().resolve()
    assert configured != (REPO_ROOT / "data").resolve(), (
        "the suite writes snapshots over the repository's shipped data dir"
    )
    assert not str(configured).startswith(str(REPO_ROOT) + os.sep), (
        f"the scratch data dir {configured} is inside the repository"
    )


def test_the_integration_suite_stays_skipped_without_the_opt_in():
    """The mechanism that actually did the writing: a reachability probe that
    un-skipped the whole module. Pointed at the isolated address it cannot
    reach a throughline, whatever is running on :8600."""
    from tests import test_integration_throughline as integration

    assert integration.THROUGHLINE == ISOLATED_THROUGHLINE_URL
    assert integration._reachable() is False, (
        "the integration suite found a substrate at the isolated address; it "
        "will run, and it will write to whatever it found"
    )


def test_the_opt_in_is_a_real_and_documented_escape_hatch():
    """Isolation that could not be turned off would have removed a capability."""
    conftest = (Path(__file__).parent / "conftest.py").read_text()
    assert LIVE_SUBSTRATE_FLAG in conftest
    assert LIVE_SUBSTRATE_ENV in conftest
    assert "parser.addoption" in conftest
