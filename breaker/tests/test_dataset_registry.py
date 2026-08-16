"""test://breaker/dataset-registry-refused — provenance is not optional.

breaker's headline dataset is the one nobody downloaded: 360 telemetry records
generated in code, from no file, with no seed. The registry exists so that fact
is machine-readable rather than a paragraph in a README that can rot.

Discharges:

* test://breaker/fixture-declared-synthetic — the 360-record fixture is
  declared ``mode: fixture`` and ``real_or_synthetic: synthetic``, and the
  loader refuses any attempt to relabel it real;
* test://breaker/dataset-registry-refused — a registry that omits a licence or
  a provenance is refused WHOLE, naming file, entry id and line, and the
  previously loaded registry keeps running;
* test://breaker/dataset-surfaces-agree — the OpenAPI contract, the opencli
  contract and the served implementation describe the same dataset surface, in
  both directions;
* test://breaker/contract-drift-fails-ci — the drift check passes on this tree
  and genuinely fails on drift, in either direction.

The loader is throughline's, imported rather than reimplemented: one refusal
idiom across the federation, not five that disagree at the edges.
"""

from __future__ import annotations

import importlib.util
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
FIXTURE_DATASET = "breaker.microgrid-telemetry-fixture"

#: The fixture's size, asserted against the generator rather than quoted from
#: the README — see test_the_declared_record_count_matches_the_generator.
FIXTURE_RECORDS = 360

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


class _RecordingLedger:
    """breaker keeps no ledger of its own; this proves the refusal is emitted
    to whatever ledger it is handed, so wiring one later needs no new code."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def append(self, event_type: str, body: dict) -> None:
        self.entries.append((event_type, body))


@pytest.fixture(autouse=True)
def _mock_gate(monkeypatch):
    """The drift check builds a real app; keep it off the network."""
    monkeypatch.setenv("BREAKER_SUBSTRATE", "mock")


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


# ------------------------------------------------- the registry itself


def test_the_shipped_registry_parses():
    registry = load_registry(REGISTRY)
    assert registry.component == "breaker"
    assert registry.entries, "the registry declares no datasets at all"


def test_every_entry_has_a_licence_and_a_provenance():
    """The whole point. A registry entry with no licence is not shippable."""
    for entry in load_registry(REGISTRY).entries:
        assert entry.licence, f"{entry.id} has no licence"
        assert entry.provenance, f"{entry.id} has no provenance"


def test_every_cached_entry_carries_its_as_of():
    for entry in load_registry(REGISTRY).entries:
        if entry.mode == "cached":
            assert entry.as_of, f"{entry.id} is cached with no as-of time"


def test_no_fixture_is_labelled_real():
    for entry in load_registry(REGISTRY).entries:
        if entry.mode == "fixture":
            assert entry.real_or_synthetic == "synthetic", entry.id


def test_the_telemetry_fixture_is_declared_a_synthetic_fixture():
    """test://breaker/fixture-declared-synthetic — the headline case.

    The numbers this component reads were authored, not measured. The registry
    has to say so in the two fields a machine reads, not only in prose.
    """
    entry = load_registry(REGISTRY).get(FIXTURE_DATASET)
    assert entry is not None, f"{FIXTURE_DATASET} is not declared at all"
    assert entry.mode == "fixture"
    assert entry.real_or_synthetic == "synthetic"
    assert entry.record_count == FIXTURE_RECORDS


def test_the_declared_record_count_matches_the_generator():
    """The count is checked against the code, so the registry cannot drift."""
    from breaker.telemetry import UNITS, TICKS, fixture

    assert len(fixture()) == FIXTURE_RECORDS == len(UNITS) * TICKS
    entry = load_registry(REGISTRY).get(FIXTURE_DATASET)
    assert entry.record_count == len(fixture())


def test_the_fixture_entry_records_no_seed_because_there_is_none():
    """Recording a seed would be a fabricated fact: the module has no RNG.

    Determinism comes from a closed-form sine of the indices. The registry says
    exactly that, and nothing stronger.
    """
    import breaker.telemetry as telemetry

    source = Path(telemetry.__file__).read_text(encoding="utf-8")
    assert "import random" not in source
    assert "getrandbits" not in source and "urandom" not in source

    provenance = load_registry(REGISTRY).get(FIXTURE_DATASET).provenance
    assert "no RNG and no seed" in provenance
    assert "sin(" in provenance


def test_the_fixture_entry_does_not_claim_a_source_file():
    """There is no telemetry artifact in this repository; nothing may imply one."""
    entry = load_registry(REGISTRY).get(FIXTURE_DATASET)
    assert entry.source.startswith("breaker/telemetry.py")
    assert not list(REPO_ROOT.glob("**/*telemetry*.csv"))
    assert not list(REPO_ROOT.glob("**/*telemetry*.parquet"))


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
    """We refuse silence, not honest ignorance — and breaker ships two of these:
    no licence is stated anywhere for the DeepSeek output it caches."""
    registry = parse_registry(_registry_with(licence="unknown"), source="r.yaml")
    assert registry.entries[0].licence == "unknown"
    assert "unknown" in {e.licence for e in load_registry(REGISTRY).entries}


def test_a_cached_dataset_without_as_of_is_refused():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(
            _registry_with(mode="cached", real_or_synthetic="real",
                           offline_cache="data/x.json"),
            source="registry.yaml")
    assert caught.value.policy == POLICY_DATASET_AS_OF


def test_the_telemetry_fixture_may_not_be_relabelled_real():
    """Take the SHIPPED entry and lie about it; the loader must refuse."""
    doc = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    for entry in doc["datasets"]:
        if entry["id"] == FIXTURE_DATASET:
            entry["real_or_synthetic"] = "real"
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(yaml.safe_dump(doc, sort_keys=False), source="r.yaml")
    assert caught.value.policy == POLICY_DATASET_HONESTY
    assert caught.value.rule == FIXTURE_DATASET


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
                           as_of="2026-08-16T00:00:00Z", offline_cache=None),
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


# ------------------------------------------- a refusal leaves the old one up


def test_a_refused_registry_leaves_the_previous_one_running(tmp_path):
    ledger = _RecordingLedger()
    store = DatasetRegistryStore(ledger=ledger, component="breaker")

    good = tmp_path / "good.yaml"
    good.write_text(_GOOD_ENTRY, encoding="utf-8")
    store.reload(good)
    assert len(store.current.entries) == 1

    bad = tmp_path / "bad.yaml"
    bad.write_text(_registry_with(licence=None), encoding="utf-8")
    with pytest.raises(ConfigRefusal):
        store.reload(bad)

    # Untouched — not emptied, not partially replaced.
    assert len(store.current.entries) == 1
    assert store.current.source == str(good)

    refusals = [body for event, body in ledger.entries if event == "config.refused"]
    assert len(refusals) == 1
    assert refusals[0]["refused"] is True
    assert refusals[0]["subject"] == "datasets"
    assert refusals[0]["policy"] == POLICY_DATASET_LICENCE
    assert refusals[0]["file"] == str(bad)
    assert refusals[0]["rule"] == "demo.one"
    assert refusals[0]["line"]


def test_an_unreadable_registry_is_a_refusal_not_a_crash(tmp_path):
    store = DatasetRegistryStore(component="breaker")
    with pytest.raises(ConfigRefusal) as caught:
        store.reload(tmp_path / "nope.yaml")
    assert "unreadable dataset registry" in caught.value.message


# ------------------------------------------------- the served surface


def test_the_http_surface_lists_and_shows_datasets(client):
    listing = client.get("/datasets")
    assert listing.status_code == 200
    body = listing.json()
    assert body["component"] == "breaker"
    assert body["count"] == len(body["datasets"]) >= 1
    assert body["refusal"] is None, body["refusal"]

    first = body["datasets"][0]["id"]
    shown = client.get(f"/datasets/{first}")
    assert shown.status_code == 200
    assert shown.json()["id"] == first
    # Licence and provenance travel on the wire, not just in the file.
    assert shown.json()["licence"]
    assert shown.json()["provenance"]


def test_the_unreachable_model_endpoint_is_listed_not_hidden(client):
    """An unavailable dataset is reported, never omitted."""
    body = client.get("/datasets").json()
    assert "breaker.dsv4-inference-endpoint" in body["unavailable"]
    shown = client.get("/datasets/breaker.dsv4-inference-endpoint").json()
    assert shown["availability"] == "unavailable"
    assert shown["unavailable_reason"] == DESIGNED_NOT_BUILT


def test_an_unknown_dataset_is_404_not_an_empty_object(client):
    assert client.get("/datasets/nope").status_code == 404


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


# ------------------------------------------------- the surfaces agree


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
    from breaker.cli import main

    assert main(["dataset", "list"], transport=transport) == 0
    listed = capsys.readouterr().out
    assert "licence" in listed and "provenance" in listed

    first = load_registry(REGISTRY).entries[0].id
    assert main(["dataset", "show", "--id", first], transport=transport) == 0
    assert first in capsys.readouterr().out


def test_dataset_validate_exits_non_zero_on_a_refusal(tmp_path, capsys):
    """A validate that cannot fail the build is not validation."""
    from breaker.cli import main

    bad = tmp_path / "bad.yaml"
    bad.write_text(_registry_with(licence=None), encoding="utf-8")
    assert main(["dataset", "validate", "--path", str(bad)]) == 1
    out = capsys.readouterr().out
    assert POLICY_DATASET_LICENCE in out
    assert str(bad) in out

    assert main(["dataset", "validate", "--path", str(REGISTRY)]) == 0
    assert '"refused": false' in capsys.readouterr().out


# ------------------------------------------------- the drift check itself


def _drift_module():
    spec = importlib.util.spec_from_file_location(
        "check_contract_drift", REPO_ROOT / "scripts" / "check-contract-drift.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_contract_and_the_served_routes_agree_in_both_directions(capsys):
    """test://breaker/contract-drift-fails-ci — green on this tree."""
    assert _drift_module().main([]) == 0
    assert "OK:" in capsys.readouterr().out


def test_every_served_route_is_documented_or_waived_with_a_reason():
    """No silent undocumented surface: the waiver file is the only escape, and
    a waiver carries a reason someone has to write."""
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    waivers = yaml.safe_load(
        (REPO_ROOT / "contracts" / "undocumented.yaml").read_text(encoding="utf-8"))
    for waiver in waivers.get("undocumented") or []:
        assert waiver.get("reason"), f"{waiver['path']} is waived with no reason"
        assert waiver["path"] not in contract["paths"], (
            f"{waiver['path']} is waived as undocumented but IS documented")


def test_the_drift_check_fails_when_a_served_route_is_undocumented(tmp_path, capsys):
    module = _drift_module()
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    contract["paths"].pop("/telemetry/series")

    stripped = tmp_path / "openapi.yaml"
    stripped.write_text(yaml.safe_dump(contract), encoding="utf-8")
    module.CONTRACT = stripped
    module.WAIVERS = tmp_path / "undocumented.yaml"

    assert module.main([]) == 1
    assert "server serves /telemetry/series" in capsys.readouterr().out


def test_the_drift_check_fails_when_the_contract_promises_a_route(tmp_path, capsys):
    module = _drift_module()
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    contract["paths"]["/effects"] = {"get": {"responses": {"200": {"description": "x"}}}}

    promised = tmp_path / "openapi.yaml"
    promised.write_text(yaml.safe_dump(contract), encoding="utf-8")
    module.CONTRACT = promised
    module.WAIVERS = tmp_path / "undocumented.yaml"

    assert module.main([]) == 1
    assert "contract promises /effects" in capsys.readouterr().out


def test_effects_is_neither_declared_nor_served_by_breaker(client):
    """A reviewer reported `GET /effects` as contracted-but-405. It is neither.

    /effects is throughline's route; breaker only ever CALLS it outbound
    (breaker/throughline.py). It is absent from breaker's contract and absent
    from breaker's app, so the honest answer is 404 — not 405, and not a drift.
    """
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    assert "/effects" not in contract["paths"]
    assert "/effects" not in client.get("/openapi.json").json()["paths"]
    for method in ("get", "post"):
        assert client.request(method, "/effects").status_code == 404

    outbound = (REPO_ROOT / "breaker" / "throughline.py").read_text(encoding="utf-8")
    assert '"/effects"' in outbound, "the only /effects here is an outbound call"


def test_telemetry_series_is_served_and_now_documented(client):
    """The other reviewer claim, which WAS true: implemented, undeclared."""
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    assert client.get("/telemetry/series?unit=battery_4").status_code == 200
    assert "/telemetry/series" in contract["paths"]


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
