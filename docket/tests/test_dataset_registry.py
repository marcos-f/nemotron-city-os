"""test://docket/dataset-registry-refused — provenance is not optional.

docket is the reference standard for provenance in this federation: its permit
snapshot already carries its own provenance header, and this registry makes the
same claim for everything else the component stands on. The loader is
throughline's, IMPORTED rather than reimplemented, so a bad dataset config is
refused here in exactly the words it would be refused in over there — naming
file, entry id and line, whole-file, with the previously loaded registry left
running untouched.

Also discharges:

* test://docket/embedding-index-declared-unavailable — the dim-2048 NeMo
  Retriever index four spec documents describe as implemented is declared
  unavailable and renders as unavailable, not as an empty list and not as a
  plausible-looking figure;
* test://docket/dataset-surfaces-agree — the OpenAPI contract, the opencli
  contract and the served implementation describe the same dataset surface, in
  both directions, so none of them can drift quietly.

Offline throughout. conftest pins MOCK_JUDGMENT=1 and MOCK_THROUGHLINE=1 before
anything imports, and nothing here reaches for a network or a key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from throughline.config import ConfigRefusal
from throughline.datasets import (
    DESIGNED_NOT_BUILT,
    POLICY_DATASET_AS_OF,
    POLICY_DATASET_HONESTY,
    POLICY_DATASET_LICENCE,
    POLICY_DATASET_PROVENANCE,
    DatasetRegistryStore,
    load_registry,
    parse_registry,
)

from docket import config

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "config" / "datasets.yaml"


def _allowlisted_bad(text: str) -> Path:
    """Write a bad registry INSIDE the allowlisted config directory.

    throughline confines POST /datasets/reload to the operator's config
    directories, exactly as it confines /config/reload. A bad file in a tmp
    dir is refused 403 for being outside the allowlist before it can be
    refused for being malformed, so a test about the SHAPE refusal has to put
    its fixture where a real one would live.
    """
    bad = REGISTRY.parent / "bad-registry-under-test.yaml"
    bad.write_text(text, encoding="utf-8")
    return bad

#: The entry this whole registry exists to carry: configured, specced, unbuilt.
EMBEDDING_INDEX = "docket.retriever-embedding-index"

_GOOD_ENTRY = """
version: 1
component: demo
datasets:
  - id: demo.one
    name: A dataset
    component: demo
    mode: fixture
    real_or_synthetic: synthetic
    availability: available
    source: data/one.json
    licence: CC0-1.0
    provenance: authored by hand for the test suite
"""


def _registry_with(**overrides) -> str:
    """Build a one-entry registry document, overriding individual fields."""
    doc = yaml.safe_load(_GOOD_ENTRY)
    entry = doc["datasets"][0]
    for key, value in overrides.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return yaml.safe_dump(doc, sort_keys=False)


@pytest.fixture
def transport(client):
    """Route CLI calls into the test app instead of a socket."""

    def call(method, path, body=None):
        response = client.request(method, path, json=body)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    return call


# ------------------------------------------------------------ the registry


def test_the_shipped_registry_parses():
    registry = load_registry(REGISTRY)
    assert registry.component == "docket"
    assert registry.entries, "the registry declares no datasets at all"


def test_config_datasets_path_finds_the_shipped_registry():
    assert config.datasets_path() == REGISTRY


def test_every_entry_has_a_licence_and_a_provenance():
    """The whole point. A registry entry with no licence is not shippable."""
    for entry in load_registry(REGISTRY).entries:
        assert entry.licence, f"{entry.id} has no licence"
        assert entry.provenance, f"{entry.id} has no provenance"


def test_every_cached_entry_carries_its_as_of():
    """A snapshot without its as-of time cannot be told apart from a live feed."""
    cached = [e for e in load_registry(REGISTRY).entries if e.mode == "cached"]
    assert cached, "docket's whole demo path is a cached snapshot; none declared"
    for entry in cached:
        assert entry.as_of, f"{entry.id} is cached with no as-of time"


def test_no_fixture_is_labelled_real():
    for entry in load_registry(REGISTRY).entries:
        if entry.mode == "fixture":
            assert entry.real_or_synthetic == "synthetic"


def test_the_permit_snapshot_entry_matches_the_snapshot_on_disk():
    """The registry's numbers are checked against the file, not just asserted."""
    import json

    entry = load_registry(REGISTRY).get("docket.seattle-building-permits")
    assert entry is not None
    snapshot = json.loads(Path(entry.offline_cache).read_text())

    assert entry.dataset_identifier == snapshot["dataset"] == "76t5-zqzr"
    assert entry.source == snapshot["source"]
    assert entry.as_of == snapshot["snapshot_utc"]
    assert entry.real_or_synthetic == snapshot["real_or_synthetic"] == "real"
    # Declared count, header count and counted rows all agree, or the registry
    # is overstating the corpus it sits on.
    assert entry.record_count == snapshot["count"] == len(snapshot["permits"])


def test_the_permit_licence_is_recorded_as_unknown_not_guessed():
    """We refuse silence, not honest ignorance — and we do not invent a licence.

    No licence statement for the Socrata permit data exists anywhere in this
    repository. "unknown" is the only defensible value; "public domain" would
    be a legal conclusion nobody on this build has reached.
    """
    entry = load_registry(REGISTRY).get("docket.seattle-building-permits")
    assert entry.licence == "unknown"
    assert "kzjm-xkqj" in entry.notes, (
        "the note must point at siren's sibling dataset from the same portal, "
        "so one licence determination is seen to cover both")


# ------------------------------------------- the unbuilt thing, declared


def test_the_embedding_index_is_declared_unavailable_not_quietly_absent():
    """test://docket/embedding-index-declared-unavailable.

    docket/config.py declares EMBED_MODEL and EMBED_DIM = 2048, and four spec
    documents describe a NeMo Retriever ingest as if it were implemented. No
    index is built. The registry must not let that claim stand unchallenged.
    """
    entry = load_registry(REGISTRY).get(EMBEDDING_INDEX)
    assert entry is not None, "the specs claim an embedding index; declare it"
    assert entry.mode == "declared-unavailable"
    assert entry.availability == "unavailable"
    assert entry.unavailable
    # No invented figure stands in for the index that does not exist.
    assert entry.record_count is None
    assert entry.as_of is None
    assert entry.offline_cache is None
    assert DESIGNED_NOT_BUILT in entry.notes
    # And it names the configured pair, so the reader can find the constants.
    assert config.EMBED_MODEL in (entry.source, entry.dataset_identifier)
    assert str(config.EMBED_DIM) in entry.source


def test_nothing_unbuilt_is_labelled_real():
    """The word `real` may not sit beside data this repository does not have."""
    for entry in load_registry(REGISTRY).entries:
        if entry.mode == "declared-unavailable":
            assert entry.real_or_synthetic == "synthetic", (
                f"{entry.id} holds nothing and must not be labelled real")


def test_an_unavailable_dataset_renders_as_unavailable_over_http(client):
    """Not an empty payload, not a 404, not a fabricated row."""
    listing = client.get("/datasets").json()
    assert EMBEDDING_INDEX in listing["unavailable"]
    # Listed, never omitted: a reader counting datasets sees it.
    assert EMBEDDING_INDEX in {d["id"] for d in listing["datasets"]}
    assert listing["honesty_note"] == DESIGNED_NOT_BUILT

    shown = client.get(f"/datasets/{EMBEDDING_INDEX}")
    assert shown.status_code == 200, "an unavailable dataset is reported, not hidden"
    body = shown.json()
    assert body["availability"] == "unavailable"
    assert body["unavailable_reason"] == DESIGNED_NOT_BUILT
    assert body["record_count"] is None


def test_the_descoped_zoning_corpus_is_visible_rather_than_forgotten():
    entry = load_registry(REGISTRY).get("docket.smc-title-23")
    assert entry is not None
    assert entry.mode == "declared-unavailable"
    assert entry.availability == "unavailable"
    assert "F5" in entry.provenance


# ------------------------------------------------------------ the refusals


def test_a_missing_licence_is_refused_naming_file_entry_and_line():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(_registry_with(licence=None), source="registry.yaml")
    refusal = caught.value
    assert refusal.policy == POLICY_DATASET_LICENCE
    assert refusal.file == "registry.yaml"
    assert refusal.rule == "demo.one"
    assert refusal.line, "the refusal must name a line"
    assert "licence" in str(refusal)


def test_a_missing_provenance_is_refused():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(_registry_with(provenance=None), source="registry.yaml")
    assert caught.value.policy == POLICY_DATASET_PROVENANCE


def test_a_blank_licence_is_refused_just_like_a_missing_one():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(_registry_with(licence="   "), source="registry.yaml")
    assert caught.value.policy == POLICY_DATASET_LICENCE


def test_unknown_is_an_acceptable_licence_because_it_is_honest():
    """We refuse silence, not honest ignorance."""
    registry = parse_registry(_registry_with(licence="unknown"), source="r.yaml")
    assert registry.entries[0].licence == "unknown"


def test_a_cached_dataset_without_as_of_is_refused():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(
            _registry_with(mode="cached", real_or_synthetic="real",
                           offline_cache="data/x.json"),
            source="registry.yaml")
    assert caught.value.policy == POLICY_DATASET_AS_OF


def test_a_fixture_may_not_be_labelled_real():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(_registry_with(real_or_synthetic="real"), source="r.yaml")
    assert caught.value.policy == POLICY_DATASET_HONESTY


def test_a_declared_unavailable_dataset_may_not_claim_to_be_available():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(
            _registry_with(mode="declared-unavailable", availability="available"),
            source="r.yaml")
    assert caught.value.policy == POLICY_DATASET_HONESTY
    assert DESIGNED_NOT_BUILT in str(caught.value)


def test_an_available_cache_must_name_where_the_cache_is():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(
            _registry_with(mode="cached", real_or_synthetic="real",
                           as_of="2026-08-16T03:42:06+00:00"),
            source="r.yaml")
    assert caught.value.policy == POLICY_DATASET_HONESTY


def test_an_unknown_mode_is_refused_rather_than_guessed():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(_registry_with(mode="probably-fine"), source="r.yaml")
    assert "mode must be one of" in caught.value.message


def test_a_duplicate_id_is_refused():
    doc = yaml.safe_load(_GOOD_ENTRY)
    doc["datasets"].append(dict(doc["datasets"][0]))
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(yaml.safe_dump(doc), source="r.yaml")
    assert "duplicate dataset id" in caught.value.message


def test_a_refused_registry_leaves_the_previous_one_running(tmp_path):
    """docket keeps no ledger of its own; the atomic swap still holds."""
    store = DatasetRegistryStore(component="docket")

    good = tmp_path / "good.yaml"
    good.write_text(_GOOD_ENTRY, encoding="utf-8")
    store.reload(good)
    assert len(store.current.entries) == 1

    bad = tmp_path / "bad.yaml"
    bad.write_text(_registry_with(licence=None), encoding="utf-8")
    with pytest.raises(ConfigRefusal) as caught:
        store.reload(bad)

    assert caught.value.file == str(bad)
    assert caught.value.rule == "demo.one"
    assert caught.value.line
    # Untouched: not emptied, not partially replaced.
    assert len(store.current.entries) == 1
    assert store.current.source == str(good)


def test_an_unreadable_registry_is_a_refusal_not_a_crash(tmp_path):
    store = DatasetRegistryStore(component="docket")
    with pytest.raises(ConfigRefusal) as caught:
        store.reload(tmp_path / "nope.yaml")
    assert "unreadable dataset registry" in caught.value.message


# -------------------------------------------------------------- the HTTP surface


def test_the_http_surface_lists_and_shows_datasets(client):
    listing = client.get("/datasets")
    assert listing.status_code == 200
    body = listing.json()
    assert body["component"] == "docket"
    assert body["count"] == len(body["datasets"])
    assert body["refusal"] is None, "the shipped registry must load at boot"

    first = body["datasets"][0]["id"]
    shown = client.get(f"/datasets/{first}")
    assert shown.status_code == 200
    assert shown.json()["id"] == first
    # Licence and provenance travel on the wire, not just in the file.
    assert shown.json()["licence"]
    assert shown.json()["provenance"]


def test_an_unknown_dataset_is_404_not_an_empty_object(client):
    response = client.get("/datasets/nope")
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_reloading_a_bad_registry_over_http_refuses_and_names_the_rule(client):
    bad = _allowlisted_bad(_registry_with(provenance=None))
    try:
        _assert_shape_refusal(client, bad)
    finally:
        bad.unlink(missing_ok=True)


def _assert_shape_refusal(client, bad: Path) -> None:
    before = client.get("/datasets").json()
    response = client.post("/datasets/reload", json={"path": str(bad)})
    assert response.status_code == 422
    body = response.json()
    assert body["refused"] is True
    assert body["file"] == str(bad)
    assert body["rule"] == "demo.one"
    assert body["line"]
    assert body["policy"] == POLICY_DATASET_PROVENANCE

    # The running registry survived the refusal.
    assert client.get("/datasets").json()["datasets"] == before["datasets"]

    # And a good reload clears the refusal again.
    ok = client.post("/datasets/reload", json={"path": str(REGISTRY)})
    assert ok.status_code == 200
    assert ok.json()["refused"] is False
    assert client.get("/datasets").json()["refusal"] is None


# ----------------------------------------------------- the surfaces agree


def test_the_dataset_paths_are_in_the_openapi_contract():
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    for path in ("/datasets", "/datasets/{id}", "/datasets/reload"):
        assert path in contract["paths"], f"{path} is served but undocumented"
    for schema in ("Dataset", "DatasetRegistry"):
        assert schema in contract["components"]["schemas"]


def test_the_served_surface_matches_the_contract_in_both_directions(client):
    """test://docket/dataset-surfaces-agree.

    A route implemented and undocumented is a drift; so is the reverse. Checked
    over docket's WHOLE surface, not only /datasets, because the pre-existing
    contract tests in this repo only check contract-subset-of-implementation.
    """
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    framework = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    documented = set(contract["paths"])
    served = set(client.get("/openapi.json").json()["paths"]) - framework

    assert documented == served, (
        f"documented-not-implemented: {sorted(documented - served)}; "
        f"implemented-not-documented: {sorted(served - documented)}")


def test_the_dataset_cli_commands_are_declared_in_opencli():
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "opencli.yaml").read_text(encoding="utf-8"))
    names = {command["name"] for command in contract["commands"]}
    assert {"dataset list", "dataset show", "dataset validate"} <= names


def test_the_preexisting_opencli_drift_is_reported_not_erased():
    """Two commands were declared before this branch and never implemented.

    They are NOT deleted: deleting the declaration would erase the evidence of
    the gap rather than close it. This test pins the drift so it stays visible
    and so closing it is a deliberate act that updates this list.
    """
    from docket.cli import build_parser

    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "opencli.yaml").read_text(encoding="utf-8"))
    declared = {command["name"] for command in contract["commands"]}

    actions = build_parser()._subparsers._group_actions[0]
    implemented = set()
    for group, parser in actions.choices.items():
        subs = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        if subs:
            implemented |= {f"{group} {command}" for command in subs[0].choices}
        else:
            implemented.add(group)

    assert {"dataset list", "dataset show", "dataset validate"} <= implemented
    assert declared - implemented == {"signal document", "judgment get"}, (
        "the known, pre-existing opencli drift has changed; update this test "
        "deliberately rather than letting it drift again")


def test_dataset_list_and_show_over_the_cli(transport, capsys):
    from docket.cli import main

    assert main(["dataset", "list"], transport=transport) == 0
    listed = capsys.readouterr().out
    assert "licence" in listed and "provenance" in listed

    first = load_registry(REGISTRY).entries[0].id
    assert main(["dataset", "show", "--id", first], transport=transport) == 0
    assert first in capsys.readouterr().out


def test_dataset_show_of_an_unknown_id_exits_non_zero(transport, capsys):
    from docket.cli import main

    assert main(["dataset", "show", "--id", "nope"], transport=transport) == 1
    capsys.readouterr()


def test_dataset_validate_exits_non_zero_on_a_refusal(tmp_path, capsys):
    """A validate that cannot fail the build is not validation."""
    from docket.cli import main

    bad = tmp_path / "bad.yaml"
    bad.write_text(_registry_with(licence=None), encoding="utf-8")
    assert main(["dataset", "validate", "--path", str(bad)]) == 1
    out = capsys.readouterr().out
    assert POLICY_DATASET_LICENCE in out
    assert str(bad) in out

    assert main(["dataset", "validate", "--path", str(REGISTRY)]) == 0
    assert '"refused": false' in capsys.readouterr().out


def test_dataset_validate_defaults_to_dockets_own_registry(capsys):
    from docket.cli import main

    assert main(["dataset", "validate"]) == 0
    assert '"component": "docket"' in capsys.readouterr().out


def test_a_registry_outside_the_allowlist_is_refused_403(client, tmp_path):
    """Same confinement, same policy id, same status as /config/reload.

    A reload surface that accepts an arbitrary filesystem path is a way to
    hand the service data of someone else's choosing.
    """
    from throughline.config import POLICY_SOURCE_ALLOWLIST

    outsider = tmp_path / "somewhere-else.yaml"
    outsider.write_text(_registry_with(), encoding="utf-8")

    before = client.get("/datasets").json()
    response = client.post("/datasets/reload", json={"path": str(outsider)})
    assert response.status_code == 403
    assert response.json()["policy"] == POLICY_SOURCE_ALLOWLIST
    assert client.get("/datasets").json()["datasets"] == before["datasets"]
