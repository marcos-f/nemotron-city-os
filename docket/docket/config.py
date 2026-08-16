"""Runtime configuration. Everything is env-overridable; defaults run offline."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = Path(os.environ.get("DOCKET_DATA_DIR", REPO_ROOT / "data"))
PERMITS_PATH = Path(os.environ.get("DOCKET_PERMITS", DATA_DIR / "permits.json"))
FIXTURES_DIR = Path(os.environ.get("DOCKET_FIXTURES", DATA_DIR / "fixtures"))
CACHE_DIR = Path(os.environ.get("DOCKET_CACHE", DATA_DIR / "cache"))

PORT = int(os.environ.get("DOCKET_PORT", "8601"))

#: Env var that overrides the dataset registry location, so changing where a
#: dataset is declared is never a code change.
DATASETS_ENV = "DOCKET_DATASETS"


def datasets_path() -> Path:
    """Where the dataset registry lives.

    Resolved rather than hardcoded because docket is run three ways: from a
    source checkout (tests, dev), from a wheel installed into a clean venv with
    the repo as the working directory (scripts/bvt.sh), and from an arbitrary
    directory in CI. Component var first, then the federation-wide
    ``DATASETS_CONFIG``, then the checkout, then the working directory.
    """
    for var in (DATASETS_ENV, "DATASETS_CONFIG"):
        override = os.environ.get(var, "").strip()
        if override:
            return Path(override)
    in_checkout = REPO_ROOT / "config" / "datasets.yaml"
    if in_checkout.exists():
        return in_checkout
    return Path.cwd() / "config" / "datasets.yaml"
THROUGHLINE_URL = os.environ.get("THROUGHLINE_URL", "http://127.0.0.1:8600")

NVIDIA_BASE_URL = os.environ.get(
    "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
)
JUDGMENT_MODEL = os.environ.get(
    "DOCKET_JUDGMENT_MODEL", "nvidia/nemotron-3-super-120b-a12b"
)
# DECLARED, NOT IMPLEMENTED. These two constants describe a NeMo Retriever
# ingest that does not exist: nothing in docket/ reads them, no embedding is
# ever computed, and no vector index is ever built or queried. Retrieval is a
# linear scan in corpus.get() over the 500-record snapshot, which is adequate
# at that size. Their only consumer anywhere is
# tests/test_hosted_judge.py::test_embedding_pair_is_declared_but_unimplemented,
# which pins the DECLARATION and asserts nothing about a shipped capability.
# They are kept rather than deleted because the declaration is real and
# published (config/datasets.yaml: docket.retriever-embedding-index,
# mode: declared-unavailable); retiring it is a scope decision, not a cleanup.
EMBED_MODEL = os.environ.get(
    "DOCKET_EMBED_MODEL", "nvidia/llama-nemotron-embed-1b-v2"
)
EMBED_DIM = 2048

# A description shorter than this carries too little to quote responsibly.
# Abstaining is the honest outcome, not a degraded judgment.
EVIDENCE_MIN_CHARS = int(os.environ.get("DOCKET_EVIDENCE_MIN_CHARS", "40"))


def mock_judgment() -> bool:
    """True when judgments come from fixtures instead of the hosted model.

    Defaults ON when no NVIDIA_API_KEY is present: the service must never
    silently pretend it reached a model it cannot reach.
    """
    raw = os.environ.get("MOCK_JUDGMENT")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return not bool(os.environ.get("NVIDIA_API_KEY", "").strip())


def mock_throughline() -> bool:
    """True when route effects go to the in-process mock substrate."""
    raw = os.environ.get("MOCK_THROUGHLINE")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return False


def api_key() -> str:
    return os.environ.get("NVIDIA_API_KEY", "").strip()
