"""Shared fixtures. Every test runs against the real snapshot, offline."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# The corpus is real Socrata data; the judgments are fixtures. Tests must never
# depend on a key being present, so mock mode is pinned before anything imports.
os.environ.setdefault("MOCK_JUDGMENT", "1")
os.environ.setdefault("MOCK_THROUGHLINE", "1")

from docket import corpus  # noqa: E402
from docket.clients.nvidia import MockJudgeClient  # noqa: E402
from docket.clients.throughline import MockThroughlineClient  # noqa: E402

# Permits chosen from the snapshot for the behaviour each one exercises.
CITED = "7110794-CN"          # quote is a verbatim span
CITED_ALT = "7111162-CN"      # quote is the whole description
REGENERATED = "7110807-CN"    # paraphrase first, verbatim on regenerate
UNCITED = "7110791-EX"        # paraphrase both attempts -> abstain
EMPTY_DESC = "7132584-CN"     # empty description -> abstain before any model


@pytest.fixture(autouse=True)
def _clean_state():
    corpus.reset_cache()
    yield
    corpus.reset_cache()


@pytest.fixture
def judge_client() -> MockJudgeClient:
    return MockJudgeClient()


@pytest.fixture
def substrate() -> MockThroughlineClient:
    return MockThroughlineClient()


@pytest.fixture
def permit():
    def _get(permitnum: str) -> dict:
        row = corpus.get(permitnum)
        assert row is not None, f"{permitnum} missing from snapshot"
        return row

    return _get


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from docket import app as app_module

    app_module.reset_state()
    with TestClient(app_module.app) as c:
        yield c
    app_module.reset_state()
