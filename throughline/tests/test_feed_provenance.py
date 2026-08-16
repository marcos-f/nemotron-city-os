"""The sibling-feed-envelopes dataset says the same thing in both places.

`integration/feeds.json` records four sibling SHAs in its `_provenance` block,
and `config/datasets.yaml` repeats those same four SHAs in prose. Two copies
of one fact, neither previously checked against the other, is a drift waiting
to happen: refresh one and the registry goes on advertising the old revisions
as the provenance of the new data.

These tests are hermetic. They assert INTERNAL consistency only — that the
file and the registry agree, and that the registry's counts match the file's
contents. They deliberately do NOT check the SHAs against the siblings'
current mains: that needs the sibling repositories, and this suite must pass
on an offline clone, which is the same reason
`scripts/check-sibling-contracts.py` is not a CI job.

The cross-repository half — "are these four revisions still current?" — is
held by the orchestrator, in
`nemo-nvidia-demo-system/scripts/check-seam-ownership --with-trees`, under
`pinned_artifacts`. It is held there because it is a claim about somebody
else's repository, and nothing in this tree can see whether it is still true.
That check is what caught these four pins at 33, 36, 26 and 13 commits behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FEEDS = json.loads((ROOT / "integration" / "feeds.json").read_text(encoding="utf-8"))
REGISTRY = yaml.safe_load((ROOT / "config" / "datasets.yaml").read_text(encoding="utf-8"))

DATASET_ID = "throughline.sibling-feed-envelopes"
SIBLINGS = ("docket", "breaker", "siren", "blindspot")


def entry() -> dict:
    for candidate in REGISTRY["datasets"]:
        if candidate["id"] == DATASET_ID:
            return candidate
    raise AssertionError(f"{DATASET_ID} is not in config/datasets.yaml")


def test_provenance_names_every_sibling() -> None:
    sources = FEEDS["_provenance"]["sources"]
    assert set(sources) == set(SIBLINGS), (
        f"the provenance block pins {sorted(sources)}, "
        f"but the dataset claims to cover {sorted(SIBLINGS)}"
    )
    for sibling in SIBLINGS:
        sha = sources[sibling]["sha"]
        assert isinstance(sha, str) and 7 <= len(sha) <= 40, sha
        assert all(c in "0123456789abcdef" for c in sha), f"{sibling}: {sha!r}"


def test_registry_prose_repeats_the_recorded_shas() -> None:
    """The duplication that made the drift invisible, made checkable."""
    provenance = entry()["provenance"]
    for sibling in SIBLINGS:
        sha = FEEDS["_provenance"]["sources"][sibling]["sha"]
        assert f"{sibling} {sha}" in provenance, (
            f"config/datasets.yaml's provenance does not name {sibling} {sha}. "
            f"integration/feeds.json was refreshed and the registry entry was "
            f"not, so the registry is advertising a revision that is no longer "
            f"the one the envelopes were derived from."
        )


def test_registry_as_of_matches_the_files_read_time() -> None:
    assert entry()["as_of"] == FEEDS["_provenance"]["read_utc"], (
        "config/datasets.yaml's as_of and integration/feeds.json's read_utc "
        "disagree about when the siblings were read"
    )


def test_record_count_matches_the_envelopes_present() -> None:
    assert entry()["record_count"] == len(FEEDS["feeds"]), (
        f"the registry advertises {entry()['record_count']} records and the "
        f"file holds {len(FEEDS['feeds'])}"
    )


def test_every_sibling_has_exactly_one_envelope() -> None:
    components = [feed["component"] for feed in FEEDS["feeds"]]
    assert sorted(components) == sorted(SIBLINGS), components
    assert len(set(components)) == len(components), "a sibling has two envelopes"


def test_every_envelope_names_the_path_its_pin_records() -> None:
    """An envelope derived from /signals/document must say so, so that a
    refresh can be checked against the contract it claims to come from."""
    sources = FEEDS["_provenance"]["sources"]
    for feed in FEEDS["feeds"]:
        recorded = sources[feed["component"]]["path"]
        assert recorded.startswith("/signals/"), (
            f"{feed['component']}: {recorded!r} is not a signal ingest path"
        )
