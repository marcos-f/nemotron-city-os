"""test://throughline/dataset-registry-refused — provenance is not optional.

The dataset registry is the effect-config refusal aimed at data honesty. A
registry that omits a licence or a provenance, or that dresses a fixture as
real or a snapshot as live, is refused WHOLE — naming file, entry id and line
— and the refusal is appended to the ledger. The previously loaded registry
keeps running.

Also discharges test://throughline/dataset-surfaces-agree: the OpenAPI
contract, the opencli contract and the served implementation describe the same
dataset surface, in both directions, so none of them can drift quietly.
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
from throughline.ledger import Ledger

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "config" / "datasets.yaml"

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


def _registry_with(**overrides: str) -> str:
    """Build a one-entry registry document, overriding individual fields."""
    doc = yaml.safe_load(_GOOD_ENTRY)
    entry = doc["datasets"][0]
    for key, value in overrides.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return yaml.safe_dump(doc, sort_keys=False)


# ------------------------------------------------- the registry itself


def test_the_shipped_registry_parses():
    registry = load_registry(REGISTRY)
    assert registry.component == "throughline"
    assert registry.entries, "the registry declares no datasets at all"


def test_every_entry_has_a_licence_and_a_provenance():
    """The whole point. A registry entry with no licence is not shippable."""
    for entry in load_registry(REGISTRY).entries:
        assert entry.licence, f"{entry.id} has no licence"
        assert entry.provenance, f"{entry.id} has no provenance"


def test_every_cached_entry_carries_its_as_of():
    """This used to be ``for entry: if entry.mode == "cached": assert ...``
    over a registry that declares NO cached entry, so it asserted nothing and
    could never fail. throughline may legitimately ship no cached dataset, so
    the shipped-data loop is kept AND the RULE itself is exercised against a
    constructed cached entry — which is the thing the test is named for."""
    entries = load_registry(REGISTRY).entries
    assert entries, "the registry declares no datasets at all"
    cached = [e for e in entries if e.mode == "cached"]
    for entry in cached:
        assert entry.as_of, f"{entry.id} is cached with no as-of time"

    # The rule, asserted directly rather than left to whatever happens to be
    # in config/datasets.yaml today.
    cached_doc = dict(mode="cached", offline_cache="data/one.json")
    good = parse_registry(
        _registry_with(as_of="2026-08-16T00:00:00Z", **cached_doc),
        source="registry.yaml",
    )
    assert good.entries[0].as_of == "2026-08-16T00:00:00Z"
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(_registry_with(**cached_doc), source="registry.yaml")
    assert caught.value.policy == POLICY_DATASET_AS_OF


def test_no_fixture_is_labelled_real():
    """Same shape, same fix: the shipped entries are checked, the count is
    asserted so the loop cannot go vacuous, and the rule is exercised."""
    entries = load_registry(REGISTRY).entries
    fixtures = [e for e in entries if e.mode == "fixture"]
    assert fixtures, (
        "no registry entry is in fixture mode, so the loop below checked "
        "nothing — a rename or a removal is a change this test should notice"
    )
    for entry in fixtures:
        assert entry.real_or_synthetic == "synthetic", entry.id

    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(
            _registry_with(mode="fixture", real_or_synthetic="real"),
            source="registry.yaml",
        )
    assert caught.value.policy == POLICY_DATASET_HONESTY


# ------------------------------------------------- the refusals


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


# ------------------------------------------------- refusal is ledgered


def test_a_refused_registry_is_ledgered_and_leaves_the_previous_one_running(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    store = DatasetRegistryStore(ledger=ledger, component="demo")

    good = tmp_path / "good.yaml"
    good.write_text(_GOOD_ENTRY, encoding="utf-8")
    store.reload(good)
    assert len(store.current.entries) == 1

    bad = tmp_path / "bad.yaml"
    bad.write_text(_registry_with(licence=None), encoding="utf-8")
    with pytest.raises(ConfigRefusal):
        store.reload(bad)

    # The previous registry is untouched — not emptied, not partially replaced.
    assert len(store.current.entries) == 1
    assert store.current.source == str(good)

    refusals = list(ledger.by_type("config.refused"))
    assert len(refusals) == 1
    body = refusals[-1]["body"]
    assert body["refused"] is True
    assert body["subject"] == "datasets"
    assert body["policy"] == POLICY_DATASET_LICENCE
    assert body["file"] == str(bad)
    assert body["rule"] == "demo.one"
    assert body["line"]

    # And the chain still verifies with the refusal in it.
    assert ledger.verify()["valid"]


def test_an_unreadable_registry_is_a_refusal_not_a_crash(tmp_path):
    store = DatasetRegistryStore(component="demo")
    with pytest.raises(ConfigRefusal) as caught:
        store.reload(tmp_path / "nope.yaml")
    assert "unreadable dataset registry" in caught.value.message


# ------------------------------------------------- the three surfaces agree


def test_the_http_surface_lists_and_shows_datasets(client):
    listing = client.get("/datasets")
    assert listing.status_code == 200
    body = listing.json()
    assert body["component"] == "throughline"
    assert body["count"] == len(body["datasets"])

    first = body["datasets"][0]["id"]
    shown = client.get(f"/datasets/{first}")
    assert shown.status_code == 200
    assert shown.json()["id"] == first
    # Licence and provenance travel on the wire, not just in the file.
    assert shown.json()["licence"]
    assert shown.json()["provenance"]


def test_an_unknown_dataset_is_404_not_an_empty_object(client):
    assert client.get("/datasets/nope").status_code == 404


def test_reloading_a_bad_registry_over_http_refuses_and_names_the_rule(client):
    # Inside the allowlisted directory, so this exercises the SHAPE refusal
    # rather than the source-confinement one. Cleaned up at the end.
    bad = REGISTRY.parent / "bad-registry-under-test.yaml"
    bad.write_text(_registry_with(provenance=None), encoding="utf-8")
    try:
        _assert_bad_reload_is_refused(client, bad)
    finally:
        bad.unlink(missing_ok=True)


def _assert_bad_reload_is_refused(client, bad: Path) -> None:
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


def test_the_dataset_paths_are_in_the_openapi_contract():
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    for path in ("/datasets", "/datasets/{id}", "/datasets/reload"):
        assert path in contract["paths"], f"{path} is served but undocumented"


def test_the_served_dataset_surface_matches_the_contract_in_both_directions(client):
    """A route implemented and undocumented is a drift; so is the reverse."""
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    served = client.get("/openapi.json").json()["paths"]

    documented = {p for p in contract["paths"] if p.startswith("/datasets")}
    implemented = {p for p in served if p.startswith("/datasets")}
    assert documented == implemented, (
        f"documented-not-implemented: {sorted(documented - implemented)}; "
        f"implemented-not-documented: {sorted(implemented - documented)}")


def test_the_dataset_cli_commands_are_declared_in_opencli():
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "opencli.yaml").read_text(encoding="utf-8"))
    names = {command["name"] for command in contract["commands"]}
    assert {"dataset list", "dataset show", "dataset validate"} <= names


def test_dataset_list_and_show_over_the_cli(transport, capsys):
    from throughline.cli import main

    assert main(["dataset", "list"], transport=transport) == 0
    listed = capsys.readouterr().out
    assert "licence" in listed and "provenance" in listed

    registry = load_registry(REGISTRY)
    first = registry.entries[0].id
    assert main(["dataset", "show", "--id", first], transport=transport) == 0
    assert first in capsys.readouterr().out


def test_dataset_validate_exits_non_zero_on_a_refusal(tmp_path, capsys):
    """A validate that cannot fail the build is not validation."""
    from throughline.cli import main

    bad = tmp_path / "bad.yaml"
    bad.write_text(_registry_with(licence=None), encoding="utf-8")
    assert main(["dataset", "validate", "--path", str(bad)]) == 1
    out = capsys.readouterr().out
    assert POLICY_DATASET_LICENCE in out
    assert str(bad) in out

    assert main(["dataset", "validate", "--path", str(REGISTRY)]) == 0
    assert '"refused": false' in capsys.readouterr().out


def test_a_registry_outside_the_allowlist_is_refused_403_naming_the_policy(
    client, tmp_path
):
    """Same confinement as /config/reload, same policy id, same status.

    ``POST /datasets/reload`` must not be the unlocked back door that
    ``/config/reload`` was closed against: handing the service a registry from
    an arbitrary filesystem path is refused by name, and the running registry
    is untouched.
    """
    from throughline.config import POLICY_SOURCE_ALLOWLIST

    outsider = tmp_path / "somewhere-else.yaml"
    outsider.write_text(_GOOD_ENTRY, encoding="utf-8")

    before = client.get("/datasets").json()
    response = client.post("/datasets/reload", json={"path": str(outsider)})

    assert response.status_code == 403
    body = response.json()
    assert body["refused"] is True
    assert body["policy"] == POLICY_SOURCE_ALLOWLIST
    assert str(outsider) in body["file"]
    assert body["allowlist"], "the refusal must show what IS allowed"
    assert client.get("/datasets").json()["datasets"] == before["datasets"]


def test_the_allowlisted_directory_still_reloads(client):
    """Confinement must not break the legitimate path."""
    response = client.post("/datasets/reload", json={})
    assert response.status_code == 200
    assert response.json()["refused"] is False
