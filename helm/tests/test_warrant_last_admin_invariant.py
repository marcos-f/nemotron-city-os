"""The last-administrator invariant, enforced on EVERY verb that can break it.

test://warrant/last-admin-invariant
test://warrant/self-downgrade-refused
test://warrant/breakglass-distinct-datasets
test://warrant/steward-roster-empty

The guard used to live on the revoke path only. ``grant`` could change an
existing administrator's role and nothing looked, so a sole administrator could
grant ITSELF ``reader`` and be told 201 — orphaning its own dataset with no
administrator left and nobody able to appoint one. That is exactly the failure
the guard exists to prevent, reached by a different verb.

These tests assert the INVARIANT ("this dataset must retain at least one direct
administrator") rather than one verb's special case, so a third verb that can
reduce the direct-admin set is covered by construction: it has to pass through
``Warrant._require_admin_floor`` to change a grant at all.
"""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient

from conftest import sign_in
from helm.warrant.model import (
    BREAKGLASS_DISTINCT_DATASETS,
    BREAKGLASS_QUORUM,
    GLOBAL_STEWARD_ROLE,
    RULE_LAST_ADMIN,
    RULE_SELF_REVOKE_LAST,
    RULES,
    Authority,
    Grant,
    iso,
    utcnow,
)

DATASET = "dot-4181"
OTHER_A = "dot-9001"
OTHER_B = "dot-9002"

JKIM = "jkim@nvidia-demo.example"
RLEE = "rlee@nvidia-demo.example"
PARK = "park@nvidia-demo.example"
ODOM = "odom@nvidia-demo.example"


def as_user(client: TestClient, subject: str, role: str = "operator") -> TestClient:
    return sign_in(client, subject, role)


def claim(client: TestClient, dataset: str, subject: str):
    as_user(client, subject)
    return client.post(f"/datasets/{dataset}/claim", json={})


def authority(client: TestClient, dataset: str, **params):
    return client.get(f"/datasets/{dataset}/authority", params=params or None)


def signal_payloads(substrate, effect_type: str) -> list[dict]:
    out = []
    for entry in substrate.entries:
        if entry["type"] != "signal.ingested":
            continue
        body = entry["body"]
        if body.get("class") != f"warrant.{effect_type}":
            continue
        out.append(json.loads(body["payload_ref"]))
    return out


# ============================================ test://warrant/self-downgrade-refused
def test_the_sole_administrator_cannot_downgrade_itself_with_a_grant(client, substrate):
    """THE DEFECT. This returned 201 and left ``dot-4181`` with no administrator.

    Same dataset, same last administrator, same orphaning — reached by ``grant``
    instead of ``revoke``, which is why guarding one verb was never enough.
    """
    claim(client, DATASET, JKIM)
    as_user(client, JKIM)

    response = client.post(f"/datasets/{DATASET}/grants",
                           json={"subject": JKIM, "role": "reader"})

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["refused"] is True
    assert body["rule"] == RULE_SELF_REVOKE_LAST
    # The SAME sentence the revoke path gives. One rule, one answer.
    assert body["reason"] == RULES[RULE_SELF_REVOKE_LAST]
    assert body["direct_admins"] == [JKIM]
    assert body["would_leave_direct_admins"] == []
    assert body["ledgered"] is True

    # Nothing changed, and the dataset still has its administrator.
    read = authority(client, DATASET).json()
    assert read["admin_count"] == 1
    assert read["owner"]["subject"] == JKIM
    assert {p["subject"]: p["role"] for p in read["people"]} == {JKIM: "admin"}

    # And the refusal is on the chain, by name, like every other refusal.
    refusal = signal_payloads(substrate, "authz.refused")[-1]
    assert refusal["rule"] == RULE_SELF_REVOKE_LAST
    assert refusal["actor"] == JKIM


def test_one_admin_may_not_downgrade_the_only_other_administrator(client, substrate):
    """The non-self case takes WARRANT-LAST-ADMIN, exactly as revoke does.

    Two administrators; one is removed by revoke, and the survivor is then
    downgraded by grant. The second step is the one that would orphan the
    dataset and it is the one that is refused.
    """
    claim(client, DATASET, JKIM)
    as_user(client, JKIM)
    promoted = client.post(f"/datasets/{DATASET}/grants",
                           json={"subject": RLEE, "role": "admin"})
    assert promoted.status_code == 201, promoted.text
    assert authority(client, DATASET).json()["admin_count"] == 2

    # Two admins: downgrading one of them is legitimate and must still work.
    as_user(client, RLEE)
    demoted = client.post(f"/datasets/{DATASET}/grants",
                          json={"subject": JKIM, "role": "steward"})
    assert demoted.status_code == 201, demoted.text
    assert authority(client, DATASET).json()["admin_count"] == 1

    # One admin left. Now the same call is refused — and by the other rule,
    # because JKIM is not the actor here.
    as_user(client, JKIM)  # JKIM is only a steward now
    refused = client.post(f"/datasets/{DATASET}/grants",
                          json={"subject": RLEE, "role": "reader"})
    assert refused.status_code == 403
    assert refused.json()["rule"] == "WARRANT-NOT-ADMIN"

    as_user(client, RLEE)
    self_refused = client.post(f"/datasets/{DATASET}/grants",
                               json={"subject": RLEE, "role": "reader"})
    assert self_refused.status_code == 409
    assert self_refused.json()["rule"] == RULE_SELF_REVOKE_LAST

    # And a THIRD party admin downgrading the last admin takes LAST-ADMIN.
    # Re-promote JKIM so there is an actor who is not the target...
    as_user(client, RLEE)
    client.post(f"/datasets/{DATASET}/grants", json={"subject": JKIM, "role": "admin"})
    assert authority(client, DATASET).json()["admin_count"] == 2
    # ...then remove RLEE, leaving JKIM sole admin...
    client.post(f"/datasets/{DATASET}/revocations", json={"subject": RLEE})
    assert authority(client, DATASET).json()["admin_count"] == 1
    # ...and RLEE, no longer an admin, cannot even try.
    blocked = client.post(f"/datasets/{DATASET}/grants",
                          json={"subject": JKIM, "role": "reader"})
    assert blocked.status_code == 403
    assert blocked.json()["rule"] == "WARRANT-NOT-ADMIN"


def test_a_delegate_does_not_satisfy_the_invariant_on_the_grant_path_either(
    client, substrate
):
    """The guard counts DIRECT administrators. It counts them on every verb.

    Otherwise the vacation problem gets a second, quieter answer: delegate,
    downgrade yourself with a grant, and the dataset's only authority is one
    that expires on a timer with nobody able to renew it.
    """
    claim(client, DATASET, JKIM)
    as_user(client, JKIM)
    until = iso(utcnow() + timedelta(days=7))
    delegated = client.post(f"/datasets/{DATASET}/delegations",
                            json={"subject": RLEE, "role": "admin",
                                  "expires_at": until})
    assert delegated.status_code == 201, delegated.text

    refused = client.post(f"/datasets/{DATASET}/grants",
                          json={"subject": JKIM, "role": "reader"})
    assert refused.status_code == 409
    body = refused.json()
    assert body["rule"] == RULE_SELF_REVOKE_LAST
    assert body["delegates_do_not_count"] == [RLEE]
    assert "expires and does not count" in body["detail"]
    assert authority(client, DATASET).json()["admin_count"] == 1


def test_an_ordinary_grant_and_a_promotion_are_untouched_by_the_invariant(
    client, substrate
):
    """The guard must not become a general obstruction. Widening always works."""
    claim(client, DATASET, JKIM)
    as_user(client, JKIM)

    reader = client.post(f"/datasets/{DATASET}/grants",
                         json={"subject": PARK, "role": "reader"})
    assert reader.status_code == 201, reader.text

    # Re-granting the sole admin the admin role is a no-op on the invariant.
    again = client.post(f"/datasets/{DATASET}/grants",
                        json={"subject": JKIM, "role": "admin"})
    assert again.status_code == 201, again.text
    assert authority(client, DATASET).json()["admin_count"] == 1

    # A reader may be raised to steward without anyone consulting the guard.
    steward = client.post(f"/datasets/{DATASET}/grants",
                          json={"subject": PARK, "role": "steward"})
    assert steward.status_code == 201, steward.text
    roles = {p["subject"]: p["role"] for p in authority(client, DATASET).json()["people"]}
    assert roles == {JKIM: "admin", PARK: "steward"}


def test_the_invariant_refusal_is_the_same_over_mcp(client, substrate):
    """Same rule, same reason, third surface. A refusal that changes shape
    between surfaces is two refusals, and only one of them is documented."""
    claim(client, DATASET, JKIM)

    # The same refusal, over HTTP...
    as_user(client, JKIM)
    http = client.post(f"/datasets/{DATASET}/grants",
                       json={"subject": JKIM, "role": "reader"}).json()

    # ...and over MCP, where the caller names itself as the administrator.
    result = client.post("/mcp/call", json={
        "name": "authority_grant",
        "arguments": {"dataset_id": DATASET, "subject": JKIM, "role": "reader",
                      "principal": JKIM},
    }).json()
    assert result["ok"] is False
    assert result["refused"] is True
    assert result["data"]["rule"] == RULE_SELF_REVOKE_LAST == http["rule"]
    assert result["data"]["reason"] == http["reason"] == RULES[RULE_SELF_REVOKE_LAST]
    assert result["data"]["detail"] == http["detail"]
    assert RULE_SELF_REVOKE_LAST in result["summary"]
    assert authority(client, DATASET).json()["admin_count"] == 1

    # ...and the rule is in the documented catalogue every surface quotes from.
    catalogue = {r["rule"]: r["reason"]
                 for r in client.get("/authority/rules").json()["rules"]}
    assert catalogue[RULE_SELF_REVOKE_LAST] == http["reason"]
    assert catalogue[RULE_LAST_ADMIN] == RULES[RULE_LAST_ADMIN]


def test_the_invariant_refusal_reaches_the_cli_with_the_same_rule(
    client, substrate, monkeypatch, capsys
):
    """Fourth surface. A refusal that only three surfaces can quote is a
    refusal a script will silently step over."""
    import helm.cli as cli

    claim(client, DATASET, JKIM)
    as_user(client, JKIM)
    monkeypatch.setattr(cli.httpx, "post", lambda url, **kw: client.post(
        url.replace("http://127.0.0.1:8610", ""), json=kw.get("json")))

    # 3, not 0: a CLI that prints a refusal and exits 0 has not refused.
    code = cli.main(["dataset-grant", DATASET, "--subject", JKIM, "--role", "reader"])
    assert code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["refused"] is True
    assert out["rule"] == RULE_SELF_REVOKE_LAST
    assert out["reason"] == RULES[RULE_SELF_REVOKE_LAST]
    assert out["ledgered"] is True
    assert authority(client, DATASET).json()["admin_count"] == 1


def test_the_console_shows_the_invariant_refusal_it_just_made(client, substrate):
    """A refusal nobody recorded is indistinguishable from one that never
    happened — including on the page the operator is looking at."""
    claim(client, DATASET, JKIM)
    as_user(client, JKIM)
    client.post(f"/datasets/{DATASET}/grants", json={"subject": JKIM, "role": "reader"})

    page = client.get(f"/datasets/{DATASET}/people")
    assert page.status_code == 200
    assert RULE_SELF_REVOKE_LAST in page.text
    assert "would leave it with none" in page.text
    assert "Refusals on the record" in page.text
    # And the page still shows one administrator, because nothing changed.
    assert authority(client, DATASET).json()["admin_count"] == 1


# ============================================== test://warrant/last-admin-invariant
def test_the_invariant_is_stated_once_and_computed_from_the_resulting_state():
    """A unit test on the invariant itself, with no verb in sight.

    ``direct_admins_after`` answers "who would administer this dataset if that
    row became that role", which is the question every verb has to ask. A verb
    added later cannot get this wrong by forgetting to special-case itself; it
    can only get it wrong by not asking, and nothing can change a grant without
    going through the one place that asks.
    """
    a = Authority(dataset_id=DATASET, known=True, claimed=True)
    a.grants[JKIM] = Grant(subject=JKIM, role="admin", granted_by="self", owner=True)

    assert a.direct_admins_after(JKIM, "reader") == []
    assert a.direct_admins_after(JKIM, "steward") == []
    assert a.direct_admins_after(JKIM, None) == []
    assert a.direct_admins_after(JKIM, "admin") == [JKIM]
    # A row that is not the last admin's does not move the floor.
    assert a.direct_admins_after(PARK, "reader") == [JKIM]
    assert a.direct_admins_after(PARK, "admin") == [JKIM, PARK]

    a.grants[RLEE] = Grant(subject=RLEE, role="admin", granted_by=JKIM)
    assert a.direct_admins_after(JKIM, "reader") == [RLEE]
    assert a.direct_admins_after(RLEE, None) == [JKIM]


def test_every_verb_that_can_change_a_grant_asks_the_invariant(client, substrate):
    """By construction, not by inspection.

    The invariant is enforced in ``_require_admin_floor``. This asserts that
    every warrant method which emits a grant-changing effect calls it — so the
    third verb, when it is written, fails this test rather than shipping a
    third hole.
    """
    import inspect

    from helm.warrant import service as service_module

    source = inspect.getsource(service_module.Warrant)
    assert "_require_admin_floor" in source, "the invariant has no home"

    # Which public methods emit an effect that can change a direct grant.
    # Named by SOURCE, so a method added later is picked up without anyone
    # remembering to extend a list here.
    grant_changing = {
        name: inspect.getsource(method)
        for name, method in vars(service_module.Warrant).items()
        if callable(method) and not name.startswith("_")
        and any(t in inspect.getsource(method)
                for t in ("EFFECT_GRANT", "EFFECT_REVOKE"))
    }
    # NOT a vacuous loop: the set is asserted before it is walked, so a rename
    # that empties it fails here instead of passing silently.
    assert set(grant_changing) == {"grant", "revoke"}, (
        f"the grant-changing verbs are now {sorted(grant_changing)}; each one "
        "must call _require_admin_floor and this test must be told about it"
    )
    for name, body in grant_changing.items():
        assert "_require_admin_floor" in body, (
            f"Warrant.{name} can change a direct grant but never asks the "
            "last-administrator invariant. Every verb that can reduce the set "
            "of direct administrators must call _require_admin_floor."
        )


# ======================================= test://warrant/breakglass-distinct-datasets
def _three_datasets(client):
    claim(client, DATASET, JKIM)
    claim(client, OTHER_A, RLEE)
    claim(client, OTHER_B, PARK)


def test_one_person_administering_two_datasets_is_one_voter_not_two(client, substrate):
    """A quorum of one person is a rubber stamp, so it never gets to be one.

    RLEE administers BOTH other datasets. If eligibility were counted by
    dataset, RLEE alone would look like two voters and the window would be one
    person's decision. It is counted by PERSON, so the request is refused for
    want of a quorum before any vote is cast — and the refusal says how many
    eligible voters there actually are.
    """
    claim(client, DATASET, JKIM)
    claim(client, OTHER_A, RLEE)
    claim(client, OTHER_B, RLEE)

    as_user(client, ODOM)
    refused = client.post(f"/datasets/{DATASET}/breakglass",
                          json={"reason": "jkim unreachable", "window_minutes": 30})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["rule"] == "WARRANT-BREAKGLASS-NO-QUORUM"
    assert body["eligible_voters"] == [RLEE]
    assert "1 eligible voter(s); 2 are needed" in body["detail"], (
        "an administrator of six datasets is one voter, not six"
    )
    assert body["ledgered"] is True
    assert authority(client, DATASET).json()["breakglass_active"] == []


def test_a_multi_dataset_voter_is_credited_with_exactly_one_dataset(client, substrate):
    """And the one dataset it is credited with cannot also be somebody else's.

    RLEE administers OTHER_A and OTHER_B; PARK administers OTHER_A only. Two
    people vote, so the raw count is met — but RLEE is credited with a single
    dataset, and if that dataset is the one PARK is also credited with, the
    distinctness floor is not met and the window stays shut. The quorum is two
    votes from two DISTINCT datasets, not two votes.
    """
    claim(client, DATASET, JKIM)
    claim(client, OTHER_A, RLEE)
    claim(client, OTHER_B, RLEE)
    as_user(client, RLEE)
    promoted = client.post(f"/datasets/{OTHER_A}/grants",
                           json={"subject": PARK, "role": "admin"})
    assert promoted.status_code == 201, promoted.text

    as_user(client, ODOM)
    opened = client.post(f"/datasets/{DATASET}/breakglass",
                         json={"reason": "jkim unreachable", "window_minutes": 30})
    assert opened.status_code == 201, opened.text
    request_id = opened.json()["request_id"]
    assert sorted(opened.json()["eligible_voters"]) == sorted([RLEE, PARK])

    as_user(client, RLEE)
    first = client.post(f"/datasets/{DATASET}/breakglass/{request_id}/vote")
    assert first.status_code == 201, first.text
    credited = first.json()["distinct_datasets"]
    assert len(credited) == 1, (
        f"RLEE administers two datasets and was credited with {credited}"
    )

    as_user(client, PARK)
    second = client.post(f"/datasets/{DATASET}/breakglass/{request_id}/vote")
    assert second.status_code == 201, second.text
    body = second.json()
    assert len(body["votes"]) == BREAKGLASS_QUORUM
    # Two votes, two people, and still only one dataset between them.
    assert body["distinct_datasets"] == [OTHER_A]
    assert body["quorum_met"] is False
    assert authority(client, DATASET).json()["breakglass_active"] == []


def test_two_voters_from_one_dataset_do_not_make_a_quorum(client, substrate):
    """Two people, two votes, ONE dataset between them. Still not a quorum.

    The vote count is met and the distinctness floor is not, which is the whole
    reason the two numbers are separate constants.
    """
    claim(client, DATASET, JKIM)
    claim(client, OTHER_A, RLEE)
    as_user(client, RLEE)
    # PARK becomes a second DIRECT administrator of the same other dataset.
    promoted = client.post(f"/datasets/{OTHER_A}/grants",
                           json={"subject": PARK, "role": "admin"})
    assert promoted.status_code == 201, promoted.text

    as_user(client, ODOM)
    request_id = client.post(
        f"/datasets/{DATASET}/breakglass",
        json={"reason": "jkim unreachable", "window_minutes": 30},
    ).json()["request_id"]

    as_user(client, RLEE)
    assert client.post(f"/datasets/{DATASET}/breakglass/{request_id}/vote").status_code == 201
    as_user(client, PARK)
    second = client.post(f"/datasets/{DATASET}/breakglass/{request_id}/vote")
    assert second.status_code == 201, second.text
    body = second.json()

    assert len(body["votes"]) >= BREAKGLASS_QUORUM
    assert body["distinct_datasets"] == [OTHER_A]
    assert body["distinct_datasets_needed"] == BREAKGLASS_DISTINCT_DATASETS
    assert body["quorum_met"] is False
    assert "distinct dataset" in body["still_needed"]
    assert authority(client, DATASET).json()["breakglass_active"] == []


# ============================================== test://warrant/steward-roster-empty
def test_the_federation_steward_roster_is_empty_and_nothing_seeds_it(client, substrate):
    """Correct by design: refuse for want of a quorum, never lower the bar.

    A seeded steward would be a silent superuser wearing a name badge. The
    roster stays empty, so a single-dataset federation gets
    WARRANT-BREAKGLASS-NO-QUORUM — which is the honest answer.
    """
    signet = client.app.state.signet
    stewards = [row for row in signet.role_table()
                if row.get("global_role") == GLOBAL_STEWARD_ROLE]
    assert stewards == [], (
        f"the federation-steward roster must be empty; found {stewards}"
    )

    claim(client, DATASET, JKIM)
    as_user(client, ODOM)
    refused = client.post(f"/datasets/{DATASET}/breakglass",
                          json={"reason": "jkim is on a plane"})
    assert refused.status_code == 409
    body = refused.json()
    assert body["rule"] == "WARRANT-BREAKGLASS-NO-QUORUM"
    assert body["eligible_voters"] == []
    assert body["quorum"] == BREAKGLASS_QUORUM
    assert GLOBAL_STEWARD_ROLE in body["detail"]
    assert body["ledgered"] is True

    # It is discoverable as a named role, and named as not-a-superuser.
    rules = client.get("/authority/rules").json()
    assert rules["global_role"]["role"] == GLOBAL_STEWARD_ROLE
    assert "not a superuser" in rules["global_role"]["note"]
