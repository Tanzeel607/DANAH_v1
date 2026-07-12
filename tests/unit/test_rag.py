"""RAG unit tests: chunking, citation extraction, the confidence formula, and the audit chain."""

from __future__ import annotations

import uuid
from itertools import pairwise

import pytest

from app.enums import Classification, Language
from app.schemas.common import Citation
from app.services.rag import composer
from app.services.rag.chunking import (
    chunk_text,
    count_tokens,
    detect_language,
    extract_text,
    normalise,
)
from app.services.rag.retriever import RetrievedChunk


def _chunk(score: float, n: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title=f"Doc {n}",
        chunk_index=n,
        content=f"Content of chunk {n}." * 5,
        classification=Classification.INTERNAL,
        score=score,
    )


def _citation(n: int, score: float) -> Citation:
    return Citation(
        n=n,
        kind="chunk",
        id=uuid.uuid4(),
        title=f"Doc {n}",
        snippet="…",
        score=score,
    )


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
class TestChunking:
    def test_respects_paragraph_boundaries(self) -> None:
        text = "\n\n".join(f"Paragraph {i} with some substantive content." for i in range(6))

        chunks = chunk_text(text, chunk_size=40, overlap=10)

        assert chunks
        # A chunk that begins mid-sentence produces an unreadable citation, which is the whole
        # reason chunking is paragraph-aware.
        for chunk in chunks:
            assert chunk.content == chunk.content.strip()
            assert chunk.content[0].isupper() or chunk.content[0].isdigit()

    def test_chunks_stay_within_size_budget(self) -> None:
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(10))

        chunks = chunk_text(text, chunk_size=200, overlap=40)

        assert len(chunks) > 1
        for chunk in chunks:
            # Overlap is carried as whole paragraphs, so a chunk can exceed the target slightly;
            # what matters is that it never runs away.
            assert chunk.token_count <= 200 * 1.6

    def test_overlap_carries_context_forward(self) -> None:
        paragraphs = [f"Distinct paragraph number {i} about topic {i}." for i in range(8)]
        text = "\n\n".join(paragraphs)

        chunks = chunk_text(text, chunk_size=30, overlap=15)

        assert len(chunks) >= 2
        # The tail of one chunk should reappear at the head of the next.
        overlaps = sum(
            1
            for a, b in pairwise(chunks)
            if any(line and line in b.content for line in a.content.split("\n\n")[-1:])
        )
        assert overlaps >= 1

    def test_paragraph_larger_than_chunk_is_split(self) -> None:
        giant = " ".join(f"Sentence number {i} of a very long paragraph." for i in range(200))

        chunks = chunk_text(giant, chunk_size=100, overlap=20)

        assert len(chunks) > 1
        assert all(c.token_count <= 100 * 1.6 for c in chunks)

    def test_indexes_are_sequential(self) -> None:
        text = "\n\n".join(f"Para {i}. " + "word " * 50 for i in range(6))

        chunks = chunk_text(text, chunk_size=120, overlap=20)

        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_empty_text_yields_no_chunks(self) -> None:
        assert chunk_text("   \n\n  ", chunk_size=100, overlap=10) == []

    def test_overlap_must_be_smaller_than_chunk(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            chunk_text("text", chunk_size=100, overlap=100)

    def test_normalise_collapses_whitespace_not_paragraphs(self) -> None:
        assert normalise("a  \t b\n\n\n\nc") == "a b\n\nc"

    def test_token_counting_is_nonzero(self) -> None:
        assert count_tokens("hello world") > 0
        assert count_tokens("") == 0


class TestExtraction:
    def test_markdown(self) -> None:
        text = extract_text(b"# Title\n\nBody text.", filename="a.md")
        assert "Title" in text

    def test_html_strips_scripts_and_tags(self) -> None:
        html = b"<html><body><script>evil()</script><p>Real content</p></body></html>"

        text = extract_text(html, filename="a.html")

        assert "Real content" in text
        assert "evil" not in text

    def test_arabic_utf8_survives(self) -> None:
        text = extract_text("الاستراتيجية الوطنية".encode(), filename="a.txt")
        assert "الاستراتيجية" in text

    def test_unsupported_type_is_rejected(self) -> None:
        from app.exceptions import RetrievalError

        with pytest.raises(RetrievalError, match="Cannot extract"):
            extract_text(b"data", filename="malware.exe")

    def test_language_detection(self) -> None:
        assert detect_language("This is an English strategy document.") == "en"
        assert detect_language("هذه وثيقة استراتيجية وطنية باللغة العربية") == "ar"


# ---------------------------------------------------------------------------
# Grounding contract
# ---------------------------------------------------------------------------
class TestComposer:
    def test_source_block_is_numbered_from_one(self) -> None:
        block = composer.build_source_block([_chunk(0.9, 1), _chunk(0.8, 2)])

        assert "[1]" in block
        assert "[2]" in block
        assert "[3]" not in block

    def test_empty_corpus_says_so(self) -> None:
        block = composer.build_source_block([])
        assert "no sources" in block.lower() or "nothing relevant" in block.lower()

    def test_extracts_only_cited_numbers(self) -> None:
        answer = "Diversification targets 65 percent [2]. Data must stay onshore [1]."

        assert composer.extract_cited_numbers(answer, available=3) == [2, 1]

    def test_hallucinated_citation_number_is_dropped(self) -> None:
        """A model citing [9] when 3 sources were given has invented a source."""
        answer = "This is supported [9] and also this [2]."

        assert composer.extract_cited_numbers(answer, available=3) == [2]

    def test_citations_map_to_the_right_chunks(self) -> None:
        chunks = [_chunk(0.9, 1), _chunk(0.8, 2), _chunk(0.7, 3)]

        citations = composer.build_citations("Claim [3] and claim [1].", chunks)

        assert [c.n for c in citations] == [3, 1]
        assert citations[0].id == chunks[2].chunk_id
        assert citations[1].id == chunks[0].chunk_id

    def test_uncited_sources_are_not_returned_as_citations(self) -> None:
        """Returning every retrieved chunk as a "citation" would be citation theatre."""
        chunks = [_chunk(0.9, 1), _chunk(0.8, 2), _chunk(0.7, 3)]

        citations = composer.build_citations("Only this one matters [2].", chunks)

        assert len(citations) == 1
        assert citations[0].n == 2

    def test_abstention_detected_by_marker(self) -> None:
        assert composer.is_abstention(f"{composer.ABSTAIN_MARKER} The corpus does not cover this.")

    def test_answer_without_any_citation_counts_as_abstention(self) -> None:
        """A confident, uncited assertion is exactly what the grounding contract forbids."""
        assert composer.is_abstention("The policy was introduced in 2019 and covers all agencies.")

    def test_cited_answer_is_not_an_abstention(self) -> None:
        assert not composer.is_abstention("The target is 65 percent [1].")

    def test_system_prompt_contains_the_contract(self) -> None:
        prompt = composer.system_prompt(Language.EN)

        assert composer.ABSTAIN_MARKER in prompt
        assert "cite" in prompt.lower()
        # Prompt-injection defence: ingested text is data, never instructions.
        assert "instruction" in prompt.lower()

    def test_arabic_system_prompt_is_arabic(self) -> None:
        prompt = composer.system_prompt(Language.AR)

        assert composer.ABSTAIN_MARKER in prompt
        assert any("؀" <= c <= "ۿ" for c in prompt)

    def test_answer_language_follows_the_question(self) -> None:
        assert composer.detect_answer_language("What is the GDP target?", None) is Language.EN
        assert composer.detect_answer_language("ما هو هدف الناتج المحلي؟", None) is Language.AR

    def test_explicit_language_request_wins(self) -> None:
        assert composer.detect_answer_language("What is the target?", Language.AR) is Language.AR


class TestConfidence:
    def test_abstention_scores_zero(self) -> None:
        """The UI must never show a high-confidence badge next to "I don't know"."""
        chunks = [_chunk(0.9)]

        assert composer.compute_confidence(chunks, [], abstained=True) == 0.0

    def test_no_citations_scores_zero(self) -> None:
        assert composer.compute_confidence([_chunk(0.9)], [], abstained=False) == 0.0

    def test_confidence_is_always_in_range(self) -> None:
        for score in (0.0, 0.25, 0.5, 0.75, 1.0):
            chunks = [_chunk(score, i) for i in range(4)]
            citations = [_citation(i + 1, score) for i in range(3)]

            confidence = composer.compute_confidence(chunks, citations, abstained=False)

            assert 0.0 <= confidence <= 1.0

    def test_strong_evidence_beats_weak_evidence(self) -> None:
        chunks = [_chunk(0.9, i) for i in range(4)]
        strong = [_citation(i + 1, 0.92) for i in range(3)]
        weak = [_citation(i + 1, 0.30) for i in range(3)]

        assert composer.compute_confidence(
            chunks, strong, abstained=False
        ) > composer.compute_confidence(chunks, weak, abstained=False)

    def test_corroboration_beats_a_single_source(self) -> None:
        chunks = [_chunk(0.8, i) for i in range(4)]
        one = [_citation(1, 0.8)]
        three = [_citation(i + 1, 0.8) for i in range(3)]

        assert composer.compute_confidence(
            chunks, three, abstained=False
        ) > composer.compute_confidence(chunks, one, abstained=False)

    def test_never_claims_certainty(self) -> None:
        """A 1.0 confidence badge on an LLM answer would be a lie."""
        chunks = [_chunk(1.0, i) for i in range(8)]
        citations = [_citation(i + 1, 1.0) for i in range(8)]

        assert composer.compute_confidence(chunks, citations, abstained=False) <= 0.95
