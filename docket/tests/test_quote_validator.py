"""test://docket/quote-verbatim — the quote string-matches the source record.

Also covers scenario://docket/citation-check: an uncited judgment is rejected
by the contract rather than rendered.
"""
from __future__ import annotations

import pytest

from docket import corpus
from docket.judge import judge_permit
from docket.quotes import (
    normalize_outside_quotes,
    strip_wrapping_quotes,
    verify_quote,
)
from tests.conftest import CITED, CITED_ALT, REGENERATED, UNCITED


class TestNormalization:
    def test_digit_group_spaces_collapse_outside_quotes(self):
        assert normalize_outside_quotes("a 92 000 sq ft lab") == "a 92000 sq ft lab"

    def test_digit_group_spaces_survive_inside_quotes(self):
        # The rule that gives docket its credibility: text the source itself
        # quoted is never rewritten to make a comparison succeed.
        text = 'zoned "Lot 4 500 East" today'
        assert '"Lot 4 500 East"' in normalize_outside_quotes(text)

    def test_whitespace_runs_collapse(self):
        assert normalize_outside_quotes("north  west  sides") == "north west sides"

    def test_non_digit_spaces_are_untouched(self):
        assert normalize_outside_quotes("4 story") == "4 story"

    def test_unterminated_quote_suppresses_normalization(self):
        assert normalize_outside_quotes('says "4 500 units') == 'says "4 500 units'

    @pytest.mark.parametrize(
        "raw,expected",
        [('"quoted"', "quoted"), ("“curly”", "curly"), ("bare", "bare"),
         ("'single'", "single"), ('"unbalanced', '"unbalanced')],
    )
    def test_strip_wrapping_quotes(self, raw, expected):
        assert strip_wrapping_quotes(raw) == expected

    def test_inner_quotes_are_preserved(self):
        assert strip_wrapping_quotes('"a "b" c"') == 'a "b" c'


class TestVerifyQuote:
    def test_exact_span_is_verbatim(self):
        src = "Interior TI covering the installation of a phone booth per plan"
        assert verify_quote("installation of a phone booth", src).valid

    def test_paraphrase_is_rejected(self):
        src = "Interior TI covering the installation of a phone booth per plan"
        check = verify_quote("installation of a soundproofed booth", src)
        assert not check.valid
        assert check.reason == "not-in-source"

    def test_case_change_is_rejected_and_named(self):
        src = "Modification to existing rooftop telecommunication facility"
        check = verify_quote("MODIFICATION TO EXISTING ROOFTOP", src)
        assert not check.valid
        assert check.reason == "case-mismatch"

    def test_digit_regrouping_by_the_model_is_rejected(self):
        # Source says "92 000"; the model wrote "92,000". Those are different
        # characters, so this is not a verbatim quote.
        src = "a 92 000 sq. ft. laboratory building"
        assert not verify_quote("a 92,000 sq. ft. laboratory", src).valid

    def test_source_digit_spacing_is_normalized_for_the_reader(self):
        src = "a 92 000 sq. ft. laboratory building"
        assert verify_quote("a 92000 sq. ft. laboratory", src).valid

    @pytest.mark.parametrize("quote", ["", "   ", '""'])
    def test_empty_quote_is_rejected(self, quote):
        assert verify_quote(quote, "some description").reason == "empty-quote"

    def test_empty_source_is_rejected(self):
        assert verify_quote("anything", "").reason == "empty-source"

    def test_model_wrapped_quote_marks_are_tolerated(self):
        src = "Site grading per plan for the north parcel"
        assert verify_quote('"Site grading per plan"', src).valid


class TestAgainstRealSnapshot:
    """The acceptance criterion itself: quotes string-match the source record."""

    def test_accepted_judgment_quote_is_in_the_source_record(self, permit):
        row = permit(CITED)
        judgment = judge_permit(row)
        assert judgment.abstained is False
        assert judgment.quote
        # Checked against the ORIGINAL record, not the pipeline's own copy.
        assert verify_quote(judgment.quote, row["description"]).valid
        assert judgment.quote_check["reason"] == "verbatim"

    def test_whole_description_quote_matches(self, permit):
        row = permit(CITED_ALT)
        judgment = judge_permit(row)
        assert judgment.quote.strip() in row["description"]

    def test_citations_resolve_to_real_permit_fields(self, permit):
        row = permit(CITED)
        judgment = judge_permit(row)
        assert f"permitnum:{row['permitnum']}" in judgment.citations
        field = [c for c in judgment.citations if c.startswith("field:")][0]
        assert field.split(":", 1)[1] in row

    def test_every_derived_mock_judgment_in_the_corpus_is_cited(self):
        """No judgment anywhere in the corpus escapes the validator.

        Sweeps a slice of the real snapshot rather than one hand-picked row —
        a validator that only holds for the demo permit is not a validator.
        """
        checked = 0
        for row in corpus.permits()[:120]:
            judgment = judge_permit(row)
            if judgment.abstained:
                assert judgment.quote is None
                continue
            assert verify_quote(judgment.quote, row["description"]).valid, (
                f"{row['permitnum']} published an uncited quote"
            )
            checked += 1
        assert checked > 20, f"only {checked} judgments exercised"


class TestRegenerateOnce:
    def test_uncited_first_attempt_is_regenerated_and_then_cited(self, permit):
        row = permit(REGENERATED)
        judgment = judge_permit(row)
        assert judgment.abstained is False
        assert judgment.attempts == 2, "should have taken exactly one regenerate"
        assert verify_quote(judgment.quote, row["description"]).valid

    def test_regeneration_is_capped_at_one(self, permit):
        from docket.judge import MAX_ATTEMPTS

        row = permit(UNCITED)
        judgment = judge_permit(row)
        assert MAX_ATTEMPTS == 2
        assert judgment.attempts == 2, "must not retry forever"
