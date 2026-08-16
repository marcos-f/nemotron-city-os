"""The CLI drives the same documented paths the console does."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from helm import cli


@pytest.fixture
def cli_over_app(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Point the CLI's httpx calls at the in-process app."""
    seen: list[str] = []

    def get(url: str, **kwargs: Any):
        path = url.split("8610", 1)[1]
        seen.append(f"GET {path}")
        return client.get(path, params=kwargs.get("params"))

    def post(url: str, **kwargs: Any):
        path = url.split("8610", 1)[1]
        seen.append(f"POST {path}")
        return client.post(path, json=kwargs.get("json"))

    monkeypatch.setattr(cli.httpx, "get", get)
    monkeypatch.setattr(cli.httpx, "post", post)
    return seen


def test_read_commands(cli_over_app: list[str], substrate, capsys) -> None:
    substrate.propose("eff-cli")
    for argv, expected in (
        (["overview"], "GET /overview"),
        (["feeds"], "GET /feeds"),
        (["health"], "GET /healthz"),
        (["approvals"], "GET /approvals"),
        (["ledger"], "GET /ledger"),
        (["verify"], "GET /ledger/verify"),
        (["composed"], "GET /composed/state"),
        (["walk", "eff-cli"], "GET /walk/eff-cli/json"),
    ):
        assert cli.main(argv) == 0
        assert expected in cli_over_app
    assert capsys.readouterr().out.strip()


def test_decide_requires_a_subject_flag() -> None:
    with pytest.raises(SystemExit):
        cli.main(["decide", "apr-1"])


def test_decide_as_agent_is_refused(cli_over_app: list[str], substrate, capsys) -> None:
    approval_id = substrate.propose("eff-cli-agent")
    code = cli.main(
        ["decide", approval_id, "--subject", "agent:nemoclerk(m@t)", "--as-agent"]
    )
    assert code == 3
    assert "REFUSED" in capsys.readouterr().out
    assert substrate.approvals[approval_id]["state"] == "pending"


def test_ask_reaches_nemoclerk(cli_over_app: list[str], substrate, capsys) -> None:
    substrate.propose("eff-cli-ask")
    assert cli.main(["ask", "what is waiting?"]) == 0
    assert "POST /nemoclerk/message" in cli_over_app
    assert "list_approvals" in capsys.readouterr().out
