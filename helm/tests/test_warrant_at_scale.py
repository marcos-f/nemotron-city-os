"""warrant must work on a chain longer than one page.

test://warrant/authority-at-scale
test://warrant/unknown-names-its-own-cause

warrant keeps NO permission table on purpose: dataset authority is a
projection of throughline's chain, so the chain is the only source of truth.
That design has one obligation attached to it — the projection must be able
to read the whole chain. It could not. It asked for a single 1000-entry
window and, past that, returned ``known: false`` for every dataset and
refused every write with WARRANT-AUTHORITY-UNKNOWN.

Because the ledger is append-only, that was a one-way door: once the real
chain passed 1000 entries, warrant was permanently unusable, and no amount
of restarting anything would bring it back.

These tests are about SCALE. They put a claim at the very head of the chain
and then bury it under thousands of unrelated entries, which is exactly what
a running federation does to it.
"""

from __future__ import annotations

from conftest import sign_in
from helm.warrant import projection
from helm.warrant.model import (
    LEDGER_PAGE,
    UNKNOWN_LEDGER_WINDOW,
    UNKNOWN_PAGING_INCOMPLETE,
    UNKNOWN_SUBSTRATE_UNREACHABLE,
    unknown_reason_text,
)

DATASET = "dot-4181"
JKIM = "jkim@nvidia-demo.example"
PARK = "park@nvidia-demo.example"

#: Comfortably past a single page, and past the length the live chain had
#: when this defect was found.
BURY = 2600


def bury(substrate, count: int = BURY) -> None:
    """Append ``count`` unrelated entries on top of whatever is there.

    Ordinary traffic. Nothing here touches authorization; that is the point —
    the permission graph must survive the federation simply being used.
    """
    for i in range(count):
        substrate.append("signal.ingested", {
            "id": f"noise-{i:05d}",
            "class": "telemetry.tick",
            "source": "tests",
        })


def claim(client, dataset: str, subject: str):
    sign_in(client, subject, "operator")
    return client.post(f"/datasets/{dataset}/claim", json={})


# ==================================== test://warrant/authority-at-scale
def test_authority_resolves_when_the_claim_is_buried_under_thousands(client, substrate):
    """The claim is at the head of the chain and stays true forever."""
    assert claim(client, DATASET, JKIM).status_code == 201
    at_claim = client.get(f"/datasets/{DATASET}/authority").json()
    assert at_claim["known"] is True
    assert at_claim["owner"]["subject"] == JKIM

    bury(substrate)
    assert len(substrate.entries) > LEDGER_PAGE * 2

    after = client.get(f"/datasets/{DATASET}/authority")
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["known"] is True, body.get("detail")
    assert body["chain_length"] == len(substrate.entries)
    # Same answer as at chain length 9. An append-only chain does not lose
    # facts, and neither may the projection built from it.
    assert body["owner"]["subject"] == JKIM
    assert [g["subject"] for g in body["grants"]] == [JKIM]


def test_a_write_still_works_at_scale(client, substrate):
    """WARRANT-AUTHORITY-UNKNOWN refused every write past the window."""
    claim(client, DATASET, JKIM)
    bury(substrate)

    sign_in(client, JKIM, "operator")
    granted = client.post(f"/datasets/{DATASET}/grants",
                          json={"subject": PARK, "role": "reader"})
    assert granted.status_code == 201, granted.text

    people = client.get(f"/datasets/{DATASET}/authority").json()
    assert {g["subject"] for g in people["grants"]} == {JKIM, PARK}


def test_authority_mine_resolves_at_scale(client, substrate):
    claim(client, DATASET, JKIM)
    bury(substrate)
    sign_in(client, JKIM, "operator")
    mine = client.get("/authority/mine")
    assert mine.status_code == 200, mine.text
    assert mine.json()["administers"] == [DATASET]


def test_a_grant_made_after_the_first_page_is_seen(client, substrate):
    """Not just the head of the chain: an event in the LAST page too."""
    claim(client, DATASET, JKIM)
    bury(substrate, 2400)
    sign_in(client, JKIM, "operator")
    assert client.post(f"/datasets/{DATASET}/grants",
                       json={"subject": PARK, "role": "steward"}).status_code == 201
    bury(substrate, 300)

    body = client.get(f"/datasets/{DATASET}/authority").json()
    assert body["known"] is True
    roles = {g["subject"]: g["role"] for g in body["grants"]}
    assert roles == {JKIM: "admin", PARK: "steward"}


def test_the_walk_reads_every_entry_exactly_once(client, substrate):
    """No gap and no duplicate at a page boundary — checked on the chain."""
    claim(client, DATASET, JKIM)
    bury(substrate, 1050)

    read = projection.read_chain(client.app.state.warrant.federation.throughline,
                                 page=100)
    assert read.ok and read.paged
    seqs = [e["seq"] for e in read.entries]
    assert len(seqs) == len(substrate.entries)
    assert len(seqs) == len(set(seqs)), "a page boundary duplicated an entry"
    assert seqs == sorted(seqs)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), "gap at a boundary"


def test_the_per_page_cap_is_respected_while_paging(client, substrate):
    """The DoS guard on the substrate is not bypassed to make this work."""
    bury(substrate, 1200)
    seen: list[int] = []
    real = client.app.state.warrant.federation.throughline.ledger

    def spy(limit=25, offset=None):
        seen.append(limit)
        return real(limit=limit, offset=offset)

    client.app.state.warrant.federation.throughline.ledger = spy
    assert client.get(f"/datasets/{DATASET}/authority").status_code == 200
    assert seen and max(seen) <= 1000


# =========================== test://warrant/unknown-names-its-own-cause
def test_a_genuinely_unreachable_substrate_still_says_unreachable(client, substrate):
    """The honest unknown is preserved where it is genuinely correct."""
    claim(client, DATASET, JKIM)
    substrate.online.discard("throughline")

    body = client.get(f"/datasets/{DATASET}/authority")
    assert body.status_code == 503
    payload = body.json()
    assert payload["known"] is False
    assert payload["grants"] is None
    assert payload["reason"] == UNKNOWN_SUBSTRATE_UNREACHABLE
    assert "unreachable" in payload["reason_text"]

    sign_in(client, JKIM, "operator")
    write = client.post(f"/datasets/{DATASET}/grants",
                        json={"subject": PARK, "role": "reader"})
    assert write.status_code == 503
    assert write.json()["unknown_reason"] == UNKNOWN_SUBSTRATE_UNREACHABLE
    assert "unreachable" in write.json()["reason"]


def test_a_broken_chain_is_never_called_unreachable(client, substrate):
    claim(client, DATASET, JKIM)
    substrate.tamper = True
    payload = client.get(f"/datasets/{DATASET}/authority").json()
    assert payload["reason"] == "chain-unverified"
    assert "unreachable" not in payload["reason_text"]
    assert "does not verify" in payload["reason_text"]


def test_a_short_read_says_so_and_never_claims_unreachability(client, substrate):
    """An older substrate that cannot page: honest, and honest about WHAT.

    This is the second defect. With throughline answering in milliseconds,
    the refusal said "the substrate is unreachable" — a false assertion about
    a running service, printed next to a ``detail`` field that already said
    the true thing.
    """
    claim(client, DATASET, JKIM)
    bury(substrate)
    substrate.ledger_paging = False  # an older throughline: no offset support

    payload = client.get(f"/datasets/{DATASET}/authority").json()
    assert payload["known"] is False
    assert payload["reason"] == UNKNOWN_LEDGER_WINDOW
    assert "unreachable" not in payload["reason_text"]
    assert "unreachable" not in payload["message_ui"]
    assert "the read fell short" in payload["reason_text"]

    sign_in(client, JKIM, "operator")
    write = client.post(f"/datasets/{DATASET}/grants",
                        json={"subject": PARK, "role": "reader"})
    assert write.status_code == 503
    refusal = write.json()
    assert refusal["rule"] == "WARRANT-AUTHORITY-UNKNOWN"
    assert refusal["unknown_reason"] == UNKNOWN_LEDGER_WINDOW
    # The exact regression: never "unreachable" when we mean "I could not
    # read far enough".
    assert "unreachable" not in refusal["reason"]
    assert "unreachable" not in refusal["message_ui"]
    assert "read fell short" in refusal["reason"]


def test_mine_names_its_own_cause_too(client, substrate):
    bury(substrate, 1500)
    substrate.ledger_paging = False
    sign_in(client, JKIM, "operator")
    mine = client.get("/authority/mine")
    assert mine.status_code == 503
    body = mine.json()
    assert body["known"] is False
    assert body["reason"] == UNKNOWN_LEDGER_WINDOW
    assert "not answering" not in body["message_ui"]
    assert "the read fell short" in body["reason_text"]

    substrate.online.discard("throughline")
    dead = client.get("/authority/mine").json()
    assert dead["reason"] == UNKNOWN_SUBSTRATE_UNREACHABLE
    assert "not answering" in dead["message_ui"]


def test_every_named_unknown_has_words_of_its_own(client):
    """A reason with no sentence would fall back to a stock one. None may."""
    named = [UNKNOWN_SUBSTRATE_UNREACHABLE, "chain-unverified",
             UNKNOWN_LEDGER_WINDOW, UNKNOWN_PAGING_INCOMPLETE]
    texts = {r: unknown_reason_text(r) for r in named}
    assert len(set(texts.values())) == len(named), texts
    # And the unnamed default asserts nothing about reachability.
    assert "unreachable" not in unknown_reason_text("something-new")


# ------------------------------------------------- the walk's own failures
class _Stuck:
    """A substrate that pages but never advances. Must not loop forever."""

    base_url = "http://stuck:8600"

    class _R:
        ok = True
        error = ""

        def __init__(self, data):
            self.data = data

    def ledger(self, limit=25, offset=None):
        return self._R({"entries": [{"seq": 1}], "chain_length": 10,
                        "offset": offset or 0, "returned": 1,
                        "total_entries": 10, "has_more": True,
                        "next_offset": 0})


def test_a_substrate_that_never_advances_is_unknown_not_a_hang():
    read = projection.read_chain(_Stuck(), page=10, max_pages=5)
    assert read.ok is False
    assert read.reason == UNKNOWN_PAGING_INCOMPLETE
    assert "unreachable" not in unknown_reason_text(read.reason)


class _Endless(_Stuck):
    """Pages forever, always advancing. Stopped by the page ceiling."""

    def ledger(self, limit=25, offset=None):
        start = offset or 0
        return self._R({"entries": [{"seq": start + 1}], "chain_length": 10**9,
                        "offset": start, "returned": 1,
                        "has_more": True, "next_offset": start + 1})


def test_an_endless_chain_stops_at_the_page_ceiling_and_says_unknown():
    read = projection.read_chain(_Endless(), page=1, max_pages=8)
    assert read.ok is False
    assert read.reason == UNKNOWN_PAGING_INCOMPLETE
    assert "8 pages" in read.detail
