"""test://siren/dataset-registry-refused — provenance is not optional.

siren's registry says where every incident row it serves came from, under what
licence, and how old it is. The loader is throughline's — imported, not
copied — so a siren dataset entry is refused by exactly the same rules as a
throughline one: an entry that omits a licence or a provenance, or that dresses
a fixture as real or a snapshot as live, is refused WHOLE, naming file, entry
id and line, and the previously loaded registry keeps running.

Also discharges:

* test://siren/dataset-licence-unknown-is-honest — siren records the Seattle
  Fire 911 licence as the literal string "unknown". The specs assert "public
  domain"; nothing in this repository sources that to a licence URL (GAP-002).
  We refuse silence, not honest ignorance, so "unknown" is written down and
  the reason travels in `provenance` and `notes`.
* test://siren/dataset-surfaces-agree — the OpenAPI contract, the opencli
  contract and the served implementation describe the same dataset surface,
  in both directions, so none of them can drift quietly.
* test://siren/dataset-refusal-not-ledgered-locally — siren keeps no local
  ledger, so the refusal is proven on the wire and by survival of the running
  registry rather than by a hash chain, and this test says so out loud.

Everything here runs offline: conftest pins OFFLINE_MODE=1 before siren is
imported, and the `no_network` fixture fails any test that opens a socket.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "config" / "datasets.yaml"


CONTRACT = REPO_ROOT / "contracts" / "openapi.yaml"
OPENCLI = REPO_ROOT / "contracts" / "opencli.yaml"


def _allowlisted_bad(text: str) -> Path:
    """Write a bad registry INSIDE the allowlisted config directory.

    throughline confines POST /datasets/reload to the directory the registry
    itself lives in (policy config-source-must-be-allowlisted), so a test that
    wants to exercise a *shape* refusal has to put the bad file somewhere the
    allowlist admits — otherwise it gets the confinement 403 instead and
    proves the wrong thing. Callers unlink it in a finally.
    """
    bad = REGISTRY.parent / "bad-registry-under-test.yaml"
    bad.write_text(text, encoding="utf-8")
    return bad


_GOOD_ENTRY = """
version: 1
component: siren
datasets:
  - id: siren.demo
    name: A dataset
    component: siren
    mode: fixture
    real_or_synthetic: synthetic
    availability: available
    source: tests/conftest.py
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
def registry():
    return load_registry(REGISTRY)


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


# ------------------------------------------------- the shipped registry


def test_the_shipped_registry_parses(registry):
    assert registry.component == "siren"
    assert registry.entries, "the registry declares no datasets at all"


def test_every_entry_has_a_licence_and_a_provenance(registry):
    """The whole point. A registry entry with no licence is not shippable."""
    for entry in registry.entries:
        assert entry.licence, f"{entry.id} has no licence"
        assert entry.provenance, f"{entry.id} has no provenance"


def test_every_cached_entry_carries_its_as_of(registry):
    cached = [e for e in registry.entries if e.mode == "cached"]
    assert cached, "siren serves cache; the registry should say so"
    for entry in cached:
        assert entry.as_of, f"{entry.id} is cached with no as-of time"


def test_no_fixture_is_labelled_real(registry):
    """The count is asserted too: a registry with no fixture entries satisfies
    the loop by having nothing to check, and a pass that proves nothing is the
    shape that let four other defects through."""
    fixtures = [e for e in registry.entries if e.mode == "fixture"]
    assert fixtures, (
        "no registry entry is in fixture mode, so this test checked nothing — "
        "either siren stopped shipping a fixture, or the mode label changed"
    )
    for entry in fixtures:
        assert entry.real_or_synthetic == "synthetic", entry.id


def test_the_live_feed_entry_matches_what_feed_py_actually_polls(registry):
    """The registry describes the code, not an intention about it."""
    from siren import feed

    entry = registry.get("siren.seattle-fire-911")
    assert entry is not None
    assert entry.mode == "live"
    assert entry.dataset_identifier == feed.SOCRATA_DATASET == "kzjm-xkqj"
    assert entry.source == feed.DEFAULT_FEED_URL
    assert entry.record_count == feed.DEFAULT_LIMIT == 50


def test_the_seattle_licence_is_recorded_as_unknown_not_as_public_domain(registry):
    """test://siren/dataset-licence-unknown-is-honest.

    The specs say "public domain". No licence URL in this repository backs
    that, so the registry says "unknown" and explains why. Writing the
    unsourced claim into the licence field would launder an assertion into a
    citation.
    """
    for dataset_id in ("siren.seattle-fire-911", "siren.seed-snapshot",
                       "siren.runtime-poll-cache"):
        entry = registry.get(dataset_id)
        assert entry is not None, dataset_id
        assert entry.licence == "unknown", (
            f"{dataset_id} claims licence {entry.licence!r}; nothing in this "
            "repository sources a licence for kzjm-xkqj")
    seattle = registry.get("siren.seattle-fire-911")
    assert "GAP-002" in seattle.provenance


def test_the_seed_snapshot_entry_matches_the_committed_file(registry):
    """Counted from the file, not copied from a spec."""
    import json

    from siren import feed

    entry = registry.get("siren.seed-snapshot")
    seed = json.loads(feed.SEED_SNAPSHOT.read_text(encoding="utf-8"))
    assert entry.mode == "cached"
    assert entry.real_or_synthetic == "real", (
        "cached rows are real records; their age belongs in staleness")
    assert entry.as_of == seed["as_of"]
    assert entry.record_count == len(seed["incidents"]) == 40
    assert entry.offline_cache == "siren/seed_snapshot.json"
    # The honest gap: no snapshot-taking script is committed anywhere, so the
    # entry must ADMIT that rather than imply a procedure nobody recorded.
    assert not list(REPO_ROOT.glob("scripts/*snapshot*"))
    assert "commits no snapshot-taking script" in entry.provenance.lower()


def test_the_runtime_cache_does_not_claim_a_fixed_as_of(registry):
    """It has no as-of at rest; the entry must not invent one."""
    entry = registry.get("siren.runtime-poll-cache")
    assert entry.mode == "cached"
    assert entry.availability == "degraded", (
        "a gitignored file that a clean checkout does not have is not 'available'")
    assert entry.as_of and "per-poll" in entry.as_of
    assert entry.record_count is None, "an unknown row count is omitted, not guessed"


def test_the_fixture_rows_entry_counts_the_rows_in_conftest(registry):
    from conftest import SAMPLE_ROWS

    entry = registry.get("siren.test-fixture-rows")
    assert entry.mode == "fixture"
    assert entry.real_or_synthetic == "synthetic"
    assert entry.record_count == len(SAMPLE_ROWS) == 3


# ------------------------------------------------- the refusals


def test_a_missing_licence_is_refused_naming_file_entry_and_line():
    with pytest.raises(ConfigRefusal) as caught:
        parse_registry(_registry_with(licence=None), source="registry.yaml")
    refusal = caught.value
    assert refusal.policy == POLICY_DATASET_LICENCE
    assert refusal.file == "registry.yaml"
    assert refusal.rule == "siren.demo"
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
    """We refuse silence, not honest ignorance — this is the rule siren leans
    on for kzjm-xkqj."""
    parsed = parse_registry(_registry_with(licence="unknown"), source="r.yaml")
    assert parsed.entries[0].licence == "unknown"


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


# ------------------------------------------------- refusal survives, unledgered


def test_a_refused_registry_leaves_the_previous_one_running(tmp_path):
    """test://siren/dataset-refusal-not-ledgered-locally.

    siren has NO local ledger — it posts to throughline over HTTP — so there
    is no hash chain here to append to. What must still hold is the part that
    protects a running service: the refusal names file, rule and line, and the
    registry that was already accepted keeps serving. The ledgered form of the
    same refusal is throughline's, recorded when a registry is reloaded there.
    """
    import siren

    assert not hasattr(siren, "Ledger"), "if siren grows a ledger, wire it here"

    store = DatasetRegistryStore(ledger=None, component="siren")

    good = tmp_path / "good.yaml"
    good.write_text(_GOOD_ENTRY, encoding="utf-8")
    store.reload(good)
    assert len(store.current.entries) == 1

    bad = tmp_path / "bad.yaml"
    bad.write_text(_registry_with(licence=None), encoding="utf-8")
    with pytest.raises(ConfigRefusal) as caught:
        store.reload(bad)

    refusal = caught.value
    assert refusal.file == str(bad)
    assert refusal.rule == "siren.demo"
    assert refusal.line
    assert refusal.policy == POLICY_DATASET_LICENCE

    # The previous registry is untouched — not emptied, not partially replaced.
    assert len(store.current.entries) == 1
    assert store.current.source == str(good)


def test_an_unreadable_registry_is_a_refusal_not_a_crash(tmp_path):
    store = DatasetRegistryStore(component="siren")
    with pytest.raises(ConfigRefusal) as caught:
        store.reload(tmp_path / "nope.yaml")
    assert "unreadable dataset registry" in caught.value.message


# ------------------------------------------------- the HTTP surface


def test_the_http_surface_lists_and_shows_datasets(client, no_network):
    listing = client.get("/datasets")
    assert listing.status_code == 200
    body = listing.json()
    assert body["component"] == "siren"
    assert body["count"] == len(body["datasets"])
    assert body["refusal"] is None, body["refusal"]

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
    # Written INSIDE the allowlisted config directory, so this exercises the
    # shape refusal rather than throughline's source-confinement refusal.
    bad = _allowlisted_bad(_registry_with(provenance=None))
    try:
        before = client.get("/datasets").json()
        response = client.post("/datasets/reload", json={"path": str(bad)})
        assert response.status_code == 422
        body = response.json()
        assert body["refused"] is True
        assert body["file"] == str(bad)
        assert body["rule"] == "siren.demo"
        assert body["line"]
        assert body["policy"] == POLICY_DATASET_PROVENANCE

        # The running registry survived the refusal.
        assert client.get("/datasets").json()["datasets"] == before["datasets"]
    finally:
        bad.unlink(missing_ok=True)


def test_healthz_still_answers_after_a_dataset_refusal(client):
    """A refused registry must not take the service down."""
    bad = _allowlisted_bad(_registry_with(licence=None))
    try:
        client.post("/datasets/reload", json={"path": str(bad)})
        assert client.get("/healthz").status_code == 200
        assert client.get("/pulse").status_code == 200
    finally:
        bad.unlink(missing_ok=True)


def test_a_registry_outside_the_allowlist_is_refused_403(client, tmp_path):
    """throughline confines the dataset reload source exactly as it confines
    the effect-config reload source: same policy id, same 403."""
    from throughline.config import POLICY_SOURCE_ALLOWLIST

    outsider = tmp_path / "somewhere-else.yaml"
    outsider.write_text(_registry_with(), encoding="utf-8")

    before = client.get("/datasets").json()
    response = client.post("/datasets/reload", json={"path": str(outsider)})
    assert response.status_code == 403
    body = response.json()
    assert body["refused"] is True
    assert body["policy"] == POLICY_SOURCE_ALLOWLIST
    assert body["allowlist"], "a confinement refusal must say what IS allowed"
    # A file that is perfectly VALID is still not loaded from outside the
    # allowlist: the confinement is about where, not about whether it parses.
    assert client.get("/datasets").json()["datasets"] == before["datasets"]


# ------------------------------------------------- the three surfaces agree


def test_the_dataset_paths_are_in_the_openapi_contract():
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    for path in ("/datasets", "/datasets/{id}", "/datasets/reload"):
        assert path in contract["paths"], f"{path} is served but undocumented"
    for schema in ("Dataset", "DatasetRegistry"):
        assert schema in contract["components"]["schemas"]


def test_the_ten_pre_existing_contract_paths_are_untouched():
    """Adding a surface may not quietly remove one."""
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert {
        "/signals/incident", "/incidents", "/pulse", "/feed/status",
        "/feed/refresh", "/feed/emit", "/hot-reload", "/hot-reload/timeline",
        "/config/incident", "/healthz",
    } <= set(contract["paths"])


def test_the_served_dataset_surface_matches_the_contract_in_both_directions(client):
    """A route implemented and undocumented is a drift; so is the reverse."""
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    served = client.get("/openapi.json").json()["paths"]

    documented = {p for p in contract["paths"] if p.startswith("/datasets")}
    implemented = {p for p in served if p.startswith("/datasets")}
    assert documented == implemented, (
        f"documented-not-implemented: {sorted(documented - implemented)}; "
        f"implemented-not-documented: {sorted(implemented - documented)}")


def test_the_whole_served_surface_agrees_with_the_contract_both_ways(client):
    """The drift check CI runs, run in-process, so a local suite catches it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_contract_drift", REPO_ROOT / "scripts" / "check-contract-drift.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    served = set(client.get("/openapi.json").json()["paths"]) - module.FRAMEWORK_PATHS
    waived = set(module.load_waivers())
    documented = set(contract["paths"])

    assert not (documented - served), f"promised, not served: {documented - served}"
    assert not (served - documented - waived), (
        f"served, not documented: {served - documented - waived}")


def test_the_dataset_cli_commands_are_declared_in_opencli():
    contract = yaml.safe_load(OPENCLI.read_text(encoding="utf-8"))
    names = {command["name"] for command in contract["commands"]}
    assert {"datasets list", "datasets show", "datasets validate"} <= names


def test_the_pre_existing_unimplemented_cli_commands_are_still_declared():
    """They were declared before siren had a CLI and are still unbuilt. They
    stay in the contract: deleting a promise is not the same as keeping it."""
    contract = yaml.safe_load(OPENCLI.read_text(encoding="utf-8"))
    names = {command["name"] for command in contract["commands"]}
    assert {"signal incident", "incident list"} <= names


def test_every_declared_dataset_command_exists_in_the_cli(transport, capsys):
    """The contract names the flags; the parser must accept exactly them."""
    from siren.cli import build_parser

    contract = yaml.safe_load(OPENCLI.read_text(encoding="utf-8"))
    parser = build_parser()
    for command in contract["commands"]:
        if not command["name"].startswith("datasets "):
            continue
        _, sub = command["name"].split(" ", 1)
        argv = ["datasets", sub]
        for flag in command.get("args") or []:
            argv += [flag, "x"]
        parsed = parser.parse_args(argv)
        assert parsed.command == sub


def test_datasets_list_and_show_over_the_cli(transport, capsys, no_network):
    from siren.cli import main

    assert main(["datasets", "list"], transport=transport) == 0
    listed = capsys.readouterr().out
    assert "licence" in listed and "provenance" in listed

    assert main(["datasets", "show", "--id", "siren.seed-snapshot"],
                transport=transport) == 0
    assert "siren.seed-snapshot" in capsys.readouterr().out


def test_datasets_show_on_an_unknown_id_exits_non_zero(transport, capsys):
    from siren.cli import main

    assert main(["datasets", "show", "--id", "nope"], transport=transport) == 1


def test_datasets_validate_exits_non_zero_on_a_refusal(tmp_path, capsys, no_network):
    """A validate that cannot fail the build is not validation."""
    from siren.cli import main

    bad = tmp_path / "bad.yaml"
    bad.write_text(_registry_with(licence=None), encoding="utf-8")
    assert main(["datasets", "validate", "--path", str(bad)]) == 1
    out = capsys.readouterr().out
    assert POLICY_DATASET_LICENCE in out
    assert str(bad) in out

    assert main(["datasets", "validate", "--path", str(REGISTRY)]) == 0
    assert '"refused": false' in capsys.readouterr().out
