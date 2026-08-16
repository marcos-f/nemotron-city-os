"""The hosted (SG3) path, exercised without a key.

NVIDIA_API_KEY is not available in this environment, so the nemotron-3-super
path cannot be run live. That is a recorded blocker, not a licence to ship
untested code: the request shape, the JSON parsing, the regenerate prompt, the
disk cache and the failure behaviour are all asserted here against a stubbed
transport. When a key appears, only the network call is new.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docket import config
from docket.clients.nvidia import (
    HostedJudgeClient,
    JudgeUnavailable,
    MockJudgeClient,
    build_client,
)
from docket.judge import judge_permit
from tests.conftest import CITED


def _completion(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


class StubResponse:
    def __init__(self, body: dict, status: int = 200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class TestClientSelection:
    def test_no_key_means_mock_mode(self, monkeypatch):
        """The service must never pretend it reached a model it cannot reach."""
        monkeypatch.delenv("MOCK_JUDGMENT", raising=False)
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        assert config.mock_judgment() is True
        assert isinstance(build_client(), MockJudgeClient)

    def test_key_present_selects_the_hosted_client(self, monkeypatch):
        monkeypatch.delenv("MOCK_JUDGMENT", raising=False)
        monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
        assert config.mock_judgment() is False
        assert isinstance(build_client(), HostedJudgeClient)

    def test_explicit_mock_flag_wins_over_a_present_key(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
        monkeypatch.setenv("MOCK_JUDGMENT", "1")
        assert config.mock_judgment() is True

    def test_the_entitled_model_is_the_default(self):
        assert config.JUDGMENT_MODEL == "nvidia/nemotron-3-super-120b-a12b"

    def test_embedding_pair_is_declared_but_unimplemented(self):
        """Pin the DECLARATION, and prove it is not a shipped capability.

        This test used to be named ...are_the_entitled_pair, which read as
        proof that docket embeds permits with the entitled embedding model.
        It does not. No embedding is computed anywhere; retrieval is a linear
        scan in corpus.get(). The constants are pinned because the
        declaration is published (config/datasets.yaml:
        docket.retriever-embedding-index, mode: declared-unavailable), and
        the second half of this test is the part that keeps the name honest:
        it fails the moment anything starts consuming them, at which point
        the docs must stop saying "declared, not built".
        """
        assert config.EMBED_MODEL == "nvidia/llama-nemotron-embed-1b-v2"
        assert config.EMBED_DIM == 2048

        pkg = Path(config.__file__).parent
        consumers = [
            path.name
            for path in pkg.rglob("*.py")
            if path.name != "config.py"
            and ("EMBED_MODEL" in path.read_text() or "EMBED_DIM" in path.read_text())
        ]
        assert consumers == [], (
            "EMBED_MODEL/EMBED_DIM are documented as declared-but-unimplemented; "
            "these modules now read them: {consumers}. Update README.md, "
            "PRODUCT.md, ARCHITECTURE.md and config/datasets.yaml before "
            "changing this test."
        )


class TestHostedRequest:
    def test_request_targets_the_entitled_model_and_endpoint(
        self, monkeypatch, tmp_path, permit
    ):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return StubResponse(
                _completion({"finding": "f", "quote": "q", "confidence": 0.5,
                             "route_proposal": "desk"})
            )

        import httpx

        monkeypatch.setattr(httpx, "post", fake_post)
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        client.judge(permit(CITED))

        assert seen["url"].endswith("/chat/completions")
        assert "integrate.api.nvidia.com" in seen["url"]
        assert seen["headers"]["Authorization"] == "Bearer sk-test"
        assert seen["json"]["model"] == "nvidia/nemotron-3-super-120b-a12b"

    def test_prompt_carries_the_description_the_quote_must_match(
        self, monkeypatch, tmp_path, permit
    ):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen["json"] = json
            return StubResponse(_completion({"finding": "f", "quote": "q",
                                             "confidence": 0.5}))

        import httpx

        monkeypatch.setattr(httpx, "post", fake_post)
        row = permit(CITED)
        HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path).judge(row)
        user = seen["json"]["messages"][-1]["content"]
        assert row["description"] in user

    def test_regenerate_prompt_states_the_rejection(
        self, monkeypatch, tmp_path, permit
    ):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen["json"] = json
            return StubResponse(_completion({"finding": "f", "quote": "q",
                                             "confidence": 0.5}))

        import httpx

        monkeypatch.setattr(httpx, "post", fake_post)
        HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path).judge(
            permit(CITED), attempt=2
        )
        user = seen["json"]["messages"][-1]["content"]
        assert "REJECTED" in user

    def test_system_prompt_forbids_paraphrase(self):
        from docket.clients.nvidia import SYSTEM_PROMPT

        assert "character-for-character" in SYSTEM_PROMPT
        assert "Never paraphrase" in SYSTEM_PROMPT


class TestOfflineCaching:
    """Every hosted response on the demo path gets cached; the demo replays offline."""

    def test_response_is_written_to_the_cache(self, monkeypatch, tmp_path, permit):
        import httpx

        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **k: StubResponse(
                _completion({"finding": "f", "quote": "q", "confidence": 0.5})
            ),
        )
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        client.judge(permit(CITED))
        assert list(Path(tmp_path).glob("judgment-*.json")), "nothing cached"

    def test_second_call_replays_from_cache_with_no_network(
        self, monkeypatch, tmp_path, permit
    ):
        import httpx

        calls = []

        def fake_post(*a, **k):
            calls.append(1)
            return StubResponse(
                _completion({"finding": "f", "quote": "q", "confidence": 0.5})
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        client.judge(permit(CITED))

        # Pull the key so any network attempt would raise — the cache must serve.
        offline = HostedJudgeClient(api_key="", cache_dir=tmp_path)
        raw = offline.judge(permit(CITED))
        assert raw["cached"] is True
        assert len(calls) == 1, "cached demo path must not call out again"

    def test_cache_key_separates_attempts(self, tmp_path, permit):
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        row = permit(CITED)
        assert client._cache_path(row, 1) != client._cache_path(row, 2)

    def test_cache_key_separates_permits(self, tmp_path, permit):
        from tests.conftest import CITED_ALT

        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        assert client._cache_path(permit(CITED), 1) != client._cache_path(
            permit(CITED_ALT), 1
        )


class TestHostedFailures:
    def test_missing_key_raises_rather_than_inventing_a_judgment(
        self, tmp_path, permit
    ):
        client = HostedJudgeClient(api_key="", cache_dir=tmp_path)
        with pytest.raises(JudgeUnavailable, match="NVIDIA_API_KEY"):
            client.judge(permit(CITED))

    def test_transport_failure_becomes_judge_unavailable(
        self, monkeypatch, tmp_path, permit
    ):
        import httpx

        def boom(*a, **k):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(httpx, "post", boom)
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        with pytest.raises(JudgeUnavailable, match="connection reset"):
            client.judge(permit(CITED))

    def test_non_json_reply_is_refused(self, monkeypatch, tmp_path, permit):
        import httpx

        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **k: StubResponse(
                {"choices": [{"message": {"content": "I think it's a lab."}}]}
            ),
        )
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        with pytest.raises(JudgeUnavailable, match="did not return JSON"):
            client.judge(permit(CITED))

    def test_fenced_json_is_tolerated(self, monkeypatch, tmp_path, permit):
        import httpx

        fenced = '```json\n{"finding": "f", "quote": "q", "confidence": 0.4}\n```'
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **k: StubResponse({"choices": [{"message": {"content": fenced}}]}),
        )
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        assert client.judge(permit(CITED))["finding"] == "f"

    def test_hosted_failure_abstains_end_to_end(self, monkeypatch, tmp_path, permit):
        """The whole pipeline degrades to abstention, never to a guess."""
        import httpx

        monkeypatch.setattr(
            httpx, "post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503"))
        )
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        judgment = judge_permit(permit(CITED), client=client)
        assert judgment.abstained is True
        assert "judge-unavailable" in judgment.abstain_reason


class TestHostedJudgmentsAreValidatedToo:
    """The validator is not a mock-mode courtesy — it gates the hosted path."""

    def test_hosted_paraphrase_is_rejected_and_abstains(
        self, monkeypatch, tmp_path, permit
    ):
        import httpx

        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **k: StubResponse(
                _completion({
                    "finding": "A conference room refit.",
                    "quote": "a tidy paraphrase that appears nowhere in the record",
                    "confidence": 0.95,
                    "route_proposal": "Commercial desk",
                })
            ),
        )
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        judgment = judge_permit(permit(CITED), client=client)
        assert judgment.abstained is True
        assert judgment.abstain_reason.startswith("uncited")

    def test_hosted_verbatim_quote_is_accepted_and_not_labelled_mock(
        self, monkeypatch, tmp_path, permit
    ):
        import httpx

        row = permit(CITED)
        span = row["description"].split(".")[0]
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **k: StubResponse(
                _completion({"finding": "Conference room alteration.", "quote": span,
                             "confidence": 0.9, "route_proposal": "Commercial desk"})
            ),
        )
        client = HostedJudgeClient(api_key="sk-test", cache_dir=tmp_path)
        judgment = judge_permit(row, client=client)
        assert judgment.abstained is False
        assert judgment.mock is False
        assert judgment.model == "nvidia/nemotron-3-super-120b-a12b"
