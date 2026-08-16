"""The judgment pipeline.

One rule governs everything below: an uncited judgment never leaves this
service. The order is fixed and each step can only ever make the outcome more
conservative.

    1. thin evidence?      -> abstain, no model call at all
    2. generate            -> validate the quote against the source
    3. quote rejected?     -> regenerate ONCE
    4. still rejected?     -> abstain, reason "uncited"

Step 4 is the important one. When the model cannot cite itself, docket does not
publish a lower-confidence version of the same claim — it publishes nothing and
says why.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import config
from .clients.nvidia import JudgeUnavailable, MockJudgeClient
from .corpus import description_of
from .models import Judgment
from .quotes import QuoteCheck, verify_quote

MAX_ATTEMPTS = 2  # the initial generation, plus exactly one regenerate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _judgment_id(permitnum: str) -> str:
    return f"jdg-{permitnum}-{uuid.uuid4().hex[:8]}"


def citations_for(permit: dict, field: str = "description") -> list[str]:
    """Citations resolve to a real permit field, per scenario://docket/citation-check."""
    return [f"permitnum:{permit.get('permitnum')}", f"field:{field}"]


def has_sufficient_evidence(description: str) -> bool:
    return len(description.strip()) >= config.EVIDENCE_MIN_CHARS


def abstain(
    permit: dict,
    reason: str,
    *,
    mock: bool,
    model: str | None = None,
    quote_check: QuoteCheck | None = None,
    attempts: int = 0,
    field: str = "description",
) -> Judgment:
    """Build an explicit abstention.

    An abstention is a judgment outcome, not an error: it is ledgered, it
    carries citations to the field it found wanting, and it proposes no route.
    """
    return Judgment(
        id=_judgment_id(permit.get("permitnum", "unknown")),
        permitnum=permit.get("permitnum"),
        finding="",
        confidence=0.0,
        citations=citations_for(permit, field),
        abstained=True,
        quote=None,
        route_proposal=None,
        abstain_reason=reason,
        mock=mock,
        model=model,
        produced_at=_now(),
        quote_check=quote_check.as_dict() if quote_check else None,
        attempts=attempts,
    )


def judge_permit(permit: dict, client=None) -> Judgment:
    """Judge one permit, or abstain. Never returns an uncited finding."""
    from .clients.nvidia import build_client

    client = client or build_client()
    is_mock = isinstance(client, MockJudgeClient)
    description = description_of(permit)

    # 1. Thin evidence — decided before any model is asked, because a model
    #    asked to judge an empty description will happily invent one.
    if not description:
        return abstain(permit, "description-empty", mock=is_mock, model=client.model)
    if not has_sufficient_evidence(description):
        return abstain(
            permit,
            f"description-below-threshold ({len(description)} < "
            f"{config.EVIDENCE_MIN_CHARS} chars)",
            mock=is_mock,
            model=client.model,
        )

    last_check: QuoteCheck | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = client.judge(permit, attempt=attempt)
        except JudgeUnavailable as exc:
            return abstain(
                permit, f"judge-unavailable: {exc}", mock=is_mock,
                model=client.model, attempts=attempt,
            )

        finding = (raw.get("finding") or "").strip()
        quote = raw.get("quote") or ""

        # A model that declines is honoured, not retried into compliance.
        if not finding and not quote.strip():
            return abstain(
                permit, "model-declined", mock=is_mock, model=raw.get("model"),
                attempts=attempt,
            )

        check = verify_quote(quote, description)
        last_check = check

        if check.valid:
            confidence = float(raw.get("confidence") or 0.0)
            return Judgment(
                id=_judgment_id(permit.get("permitnum", "unknown")),
                permitnum=permit.get("permitnum"),
                finding=finding,
                confidence=min(max(confidence, 0.0), 1.0),
                citations=citations_for(permit),
                abstained=False,
                quote=check.quote,
                route_proposal=raw.get("route_proposal"),
                mock=bool(raw.get("mock", is_mock)),
                model=raw.get("model") or client.model,
                produced_at=raw.get("produced_at") or _now(),
                quote_check=check.as_dict(),
                attempts=attempt,
            )
        # else: fall through and regenerate, once.

    # 4. Two attempts, still uncited. The finding is discarded, not downgraded.
    return abstain(
        permit,
        f"uncited: quote failed verbatim check ({last_check.reason if last_check else 'unknown'}) "
        f"after {MAX_ATTEMPTS} attempts",
        mock=is_mock,
        model=client.model,
        quote_check=last_check,
        attempts=MAX_ATTEMPTS,
    )
