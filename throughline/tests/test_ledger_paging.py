"""GET /ledger must let a consumer read the WHOLE chain, not just its tail.

The cap on ``limit`` protects this service and stays exactly where it was.
What was missing is an ``offset``: without one, a chain longer than the cap
is unreadable past its suffix, and warrant — which models dataset authority
as a projection of the ledger and keeps no permission table — cannot build
a permission graph at all. It fails honestly, but it fails.

These tests are about SCALE. They push the chain past the cap on purpose.
"""

from __future__ import annotations

from typing import Any

PAGE = 200


def _fill(substrate: Any, count: int) -> None:
    """Append ``count`` real signal entries straight through the ledger."""
    for i in range(count):
        substrate.ledger.append("signal.ingested", {
            "id": f"sig-{i:05d}",
            "class": "test.fill",
            "source": "tests",
        })


def _page_everything(client, limit: int = PAGE) -> list[dict[str, Any]]:
    """Walk the chain the way warrant does, and return every entry."""
    collected: list[dict[str, Any]] = []
    offset = 0
    while True:
        reply = client.get("/ledger", params={"limit": limit, "offset": offset})
        assert reply.status_code == 200, reply.text
        body = reply.json()
        # The server must echo the offset, or a caller cannot tell paging
        # from an older build that quietly handed back a suffix.
        assert body["offset"] == offset
        collected.extend(body["entries"])
        if not body["has_more"]:
            assert body["next_offset"] is None
            break
        assert body["next_offset"] == offset + body["returned"]
        offset = body["next_offset"]
    return collected


def test_the_tail_still_behaves_exactly_as_it_did(client) -> None:
    """No offset, no change. The default read is the one the console makes."""
    _fill(client.substrate, 30)
    body = client.get("/ledger", params={"limit": 10}).json()
    assert len(body["entries"]) == 10
    assert body["entries"][-1]["seq"] == body["chain_length"]
    # The paging keys are absent, not null, when nobody asked to page.
    assert "offset" not in body
    assert "has_more" not in body


def test_paging_returns_the_complete_chain_past_the_per_page_cap(client) -> None:
    _fill(client.substrate, 2600)
    total = client.get("/ledger", params={"limit": 1}).json()["chain_length"]
    assert total >= 2600

    entries = _page_everything(client, limit=1000)
    assert len(entries) == total, "paging must reach the head of the chain"


def test_no_gaps_and_no_duplicates_across_a_page_boundary(client) -> None:
    _fill(client.substrate, 1050)
    entries = _page_everything(client, limit=100)  # boundary every 100 rows
    seqs = [e["seq"] for e in entries]
    assert seqs == sorted(seqs), "pages must come back in chain order"
    assert len(seqs) == len(set(seqs)), "a page boundary must not duplicate a row"
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), "no gap at a boundary"


def test_the_per_page_cap_is_still_enforced(client) -> None:
    """The DoS guard is not weakened by paging. 1001 is still refused."""
    assert client.get("/ledger", params={"limit": 1001}).status_code == 422
    assert client.get("/ledger", params={"limit": 1001, "offset": 0}).status_code == 422
    assert client.get("/ledger", params={"offset": -1}).status_code == 422


def test_an_offset_past_the_head_is_an_empty_page_not_an_error(client) -> None:
    _fill(client.substrate, 5)
    body = client.get("/ledger", params={"limit": 10, "offset": 500}).json()
    assert body["entries"] == []
    assert body["returned"] == 0
    assert body["has_more"] is False
    assert body["next_offset"] is None
    # And it still tells the truth about how long the chain actually is.
    assert body["total_entries"] >= 5


def test_a_paged_row_is_verified_exactly_like_a_tailed_one(client) -> None:
    _fill(client.substrate, 3)
    paged = client.get("/ledger", params={"offset": 0, "limit": 50}).json()["entries"]
    tailed = client.get("/ledger", params={"limit": 50}).json()["entries"]
    assert [e["seq"] for e in paged] == [e["seq"] for e in tailed]
    assert all(e["verified"] for e in paged)
