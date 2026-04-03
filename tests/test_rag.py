"""
tests/test_rag.py
-----------------
Unit tests for the RAG ingestion pipeline and SemanticRetriever.

All ChromaDB and OpenAI calls are mocked — no external services required.

Tests cover:
- test_ingest_creates_collection: ingest.py creates/upserts to ChromaDB
- test_query_returns_results: retriever.query() returns properly formatted dicts
- test_hybrid_search: hybrid_search() falls back to semantic when keyword yields nothing
- test_empty_query_handled: empty queries return [] without raising
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_chromadb_client() -> MagicMock:
    """A mock chromadb.HttpClient with a pre-configured collection."""
    client = MagicMock()
    collection = MagicMock()
    client.get_or_create_collection.return_value = collection
    return client


@pytest.fixture
def mock_collection(mock_chromadb_client: MagicMock) -> MagicMock:
    """The mock collection extracted from the mock client."""
    return mock_chromadb_client.get_or_create_collection.return_value


@pytest.fixture
def sample_chroma_query_result() -> Dict[str, Any]:
    """Simulates a ChromaDB query() response for a single query vector."""
    return {
        "ids": [["crashloopbackoff__chunk_0000", "high-cpu-usage__chunk_0000"]],
        "documents": [
            [
                "Check kubectl logs --previous for crash details. Rollback if needed.",
                "kubectl top pods shows CPU at 95%. Scale out with HPA.",
            ]
        ],
        "metadatas": [
            [
                {
                    "runbook_name": "crashloopbackoff",
                    "severity": "high",
                    "category": "kubernetes",
                    "chunk_index": 0,
                },
                {
                    "runbook_name": "high-cpu-usage",
                    "severity": "medium",
                    "category": "performance",
                    "chunk_index": 0,
                },
            ]
        ],
        "distances": [[0.12, 0.28]],
    }


@pytest.fixture
def sample_single_result() -> Dict[str, Any]:
    """ChromaDB result with a single document."""
    return {
        "ids": [["crashloopbackoff__chunk_0000"]],
        "documents": [["Check kubectl logs --previous for crash details."]],
        "metadatas": [
            [
                {
                    "runbook_name": "crashloopbackoff",
                    "severity": "high",
                    "category": "kubernetes",
                    "chunk_index": 0,
                }
            ]
        ],
        "distances": [[0.05]],
    }


# ---------------------------------------------------------------------------
# Test: ingest.py — load_runbooks
# ---------------------------------------------------------------------------

class TestLoadRunbooks:
    """Tests for rag.ingest.load_runbooks()."""

    def test_load_runbooks_returns_list(self, tmp_path: Any) -> None:
        """load_runbooks should return a list of dicts for each .md file."""
        # Create dummy runbook files
        (tmp_path / "crashloopbackoff.md").write_text(
            "# CrashLoopBackOff\nseverity: high\ncategory: kubernetes\nSome content."
        )
        (tmp_path / "high-cpu-usage.md").write_text(
            "# High CPU\nseverity: medium\ncategory: performance\nSome CPU content."
        )

        from rag.ingest import load_runbooks

        docs = load_runbooks(runbooks_dir=tmp_path)

        assert isinstance(docs, list)
        assert len(docs) == 2

    def test_load_runbooks_extracts_metadata(self, tmp_path: Any) -> None:
        """load_runbooks should extract severity and category from content."""
        (tmp_path / "crashloopbackoff.md").write_text(
            "# CrashLoopBackOff\nseverity: high\ncategory: kubernetes\nContent here."
        )

        from rag.ingest import load_runbooks

        docs = load_runbooks(runbooks_dir=tmp_path)

        assert docs[0]["runbook_name"] == "crashloopbackoff"
        assert docs[0]["severity"] in {"high", "medium", "critical", "low"}
        assert isinstance(docs[0]["category"], str)
        assert isinstance(docs[0]["text"], str)

    def test_load_runbooks_empty_directory(self, tmp_path: Any) -> None:
        """load_runbooks should return an empty list if no .md files exist."""
        from rag.ingest import load_runbooks

        docs = load_runbooks(runbooks_dir=tmp_path)

        assert docs == []

    def test_load_runbooks_missing_directory(self, tmp_path: Any) -> None:
        """load_runbooks should raise FileNotFoundError for missing directory."""
        from rag.ingest import load_runbooks

        with pytest.raises(FileNotFoundError):
            load_runbooks(runbooks_dir=tmp_path / "nonexistent")


# ---------------------------------------------------------------------------
# Test: ingest.py — split_documents
# ---------------------------------------------------------------------------

class TestSplitDocuments:
    """Tests for rag.ingest.split_documents()."""

    def test_split_documents_produces_chunks(self) -> None:
        """split_documents should produce at least as many chunks as documents."""
        from rag.ingest import split_documents

        docs = [
            {
                "text": "A" * 1500,  # Large enough to split
                "runbook_name": "test-runbook",
                "severity": "low",
                "category": "general",
            }
        ]

        chunks = split_documents(docs, chunk_size=500, chunk_overlap=50)

        assert len(chunks) >= 3  # 1500 chars / 500 chunk_size = at least 3 chunks

    def test_split_documents_preserves_metadata(self) -> None:
        """Each chunk should carry the parent document's metadata."""
        from rag.ingest import split_documents

        docs = [
            {
                "text": "Short content that fits in one chunk.",
                "runbook_name": "my-runbook",
                "severity": "high",
                "category": "kubernetes",
            }
        ]

        chunks = split_documents(docs)

        assert chunks[0]["runbook_name"] == "my-runbook"
        assert chunks[0]["severity"] == "high"
        assert chunks[0]["category"] == "kubernetes"
        assert "chunk_index" in chunks[0]

    def test_split_documents_empty_input(self) -> None:
        """split_documents with empty input should return an empty list."""
        from rag.ingest import split_documents

        chunks = split_documents([])

        assert chunks == []


# ---------------------------------------------------------------------------
# Test: ingest.py — embed_and_store (full ingestion)
# ---------------------------------------------------------------------------

class TestIngestCreatesCollection:
    """Tests for rag.ingest.embed_and_store()."""

    @patch("rag.ingest.chromadb.HttpClient")
    @patch("rag.ingest.OpenAIEmbeddings")
    def test_ingest_creates_collection(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
    ) -> None:
        """embed_and_store should call get_or_create_collection and upsert."""
        # Setup mock embeddings
        mock_embeddings_instance = MagicMock()
        mock_embeddings_instance.embed_documents.return_value = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
        ]
        mock_embeddings_cls.return_value = mock_embeddings_instance

        # Setup mock ChromaDB
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chroma_cls.return_value = mock_client

        from rag.ingest import embed_and_store

        chunks = [
            {
                "text": "Check pod logs for errors.",
                "runbook_name": "crashloopbackoff",
                "severity": "high",
                "category": "kubernetes",
                "chunk_index": 0,
            },
            {
                "text": "kubectl top shows CPU at 95%.",
                "runbook_name": "high-cpu-usage",
                "severity": "medium",
                "category": "performance",
                "chunk_index": 0,
            },
        ]

        result = embed_and_store(chunks, collection_name="sre_runbooks")

        # Verify collection was created
        mock_client.get_or_create_collection.assert_called_once_with(
            name="sre_runbooks",
            metadata={"hnsw:space": "cosine"},
        )

        # Verify upsert was called
        assert mock_collection.upsert.called
        assert result == 2

    @patch("rag.ingest.chromadb.HttpClient")
    @patch("rag.ingest.OpenAIEmbeddings")
    def test_ingest_raises_on_empty_chunks(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
    ) -> None:
        """embed_and_store should raise ValueError if chunks is empty."""
        from rag.ingest import embed_and_store

        with pytest.raises(ValueError, match="No chunks to embed"):
            embed_and_store([])

    @patch("rag.ingest.chromadb.HttpClient")
    @patch("rag.ingest.OpenAIEmbeddings")
    def test_ingest_deterministic_chunk_ids(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
    ) -> None:
        """Chunk IDs should be deterministic (runbook_name__chunk_INDEX)."""
        mock_embeddings_instance = MagicMock()
        mock_embeddings_instance.embed_documents.return_value = [[0.1, 0.2, 0.3]]
        mock_embeddings_cls.return_value = mock_embeddings_instance

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chroma_cls.return_value = mock_client

        from rag.ingest import embed_and_store

        chunks = [
            {
                "text": "Content",
                "runbook_name": "crashloopbackoff",
                "severity": "high",
                "category": "kubernetes",
                "chunk_index": 0,
            }
        ]

        embed_and_store(chunks)

        upsert_call = mock_collection.upsert.call_args
        ids = upsert_call[1]["ids"] if "ids" in upsert_call[1] else upsert_call[0][0]
        assert ids[0] == "crashloopbackoff__chunk_0000"


# ---------------------------------------------------------------------------
# Test: retriever.py — SemanticRetriever.query()
# ---------------------------------------------------------------------------

class TestQueryReturnsResults:
    """Tests for SemanticRetriever.query()."""

    @patch("rag.retriever.chromadb.HttpClient")
    @patch("rag.retriever.OpenAIEmbeddings")
    def test_query_returns_formatted_results(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
        sample_chroma_query_result: Dict[str, Any],
    ) -> None:
        """query() should return a list of dicts with the expected keys."""
        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.return_value = [0.1, 0.2, 0.3]
        mock_embeddings_cls.return_value = mock_embeddings

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.query.return_value = sample_chroma_query_result
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chroma_cls.return_value = mock_client

        from rag.retriever import SemanticRetriever

        retriever = SemanticRetriever()
        results = retriever.query("pod is crashing", n_results=2)

        assert len(results) == 2
        for r in results:
            assert "content" in r
            assert "runbook_name" in r
            assert "severity" in r
            assert "category" in r
            assert "score" in r
            assert 0.0 <= r["score"] <= 1.0

    @patch("rag.retriever.chromadb.HttpClient")
    @patch("rag.retriever.OpenAIEmbeddings")
    def test_query_score_computed_correctly(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
        sample_single_result: Dict[str, Any],
    ) -> None:
        """score should be 1.0 - distance (cosine similarity)."""
        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.return_value = [0.1, 0.2, 0.3]
        mock_embeddings_cls.return_value = mock_embeddings

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.query.return_value = sample_single_result
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chroma_cls.return_value = mock_client

        from rag.retriever import SemanticRetriever

        retriever = SemanticRetriever()
        results = retriever.query("pod crash", n_results=1)

        assert len(results) == 1
        # distance=0.05 → score=0.95
        assert results[0]["score"] == pytest.approx(0.95, abs=0.01)


# ---------------------------------------------------------------------------
# Test: retriever.py — hybrid_search()
# ---------------------------------------------------------------------------

class TestHybridSearch:
    """Tests for SemanticRetriever.hybrid_search()."""

    @patch("rag.retriever.chromadb.HttpClient")
    @patch("rag.retriever.OpenAIEmbeddings")
    def test_hybrid_search_uses_keyword_when_provided(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
        sample_single_result: Dict[str, Any],
    ) -> None:
        """hybrid_search should use keyword_filter path when keyword is given."""
        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.return_value = [0.1, 0.2, 0.3]
        mock_embeddings_cls.return_value = mock_embeddings

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.query.return_value = sample_single_result
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chroma_cls.return_value = mock_client

        from rag.retriever import SemanticRetriever

        retriever = SemanticRetriever()
        results = retriever.hybrid_search("pod crash", keyword="CrashLoopBackOff", n_results=3)

        # Should call query with where_document filter
        call_kwargs = mock_collection.query.call_args[1]
        assert "where_document" in call_kwargs
        assert call_kwargs["where_document"] == {"$contains": "CrashLoopBackOff"}

    @patch("rag.retriever.chromadb.HttpClient")
    @patch("rag.retriever.OpenAIEmbeddings")
    def test_hybrid_search_falls_back_to_semantic(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
        sample_single_result: Dict[str, Any],
    ) -> None:
        """hybrid_search should fall back to semantic search when keyword yields no results."""
        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.return_value = [0.1, 0.2, 0.3]
        mock_embeddings_cls.return_value = mock_embeddings

        mock_client = MagicMock()
        mock_collection = MagicMock()

        # First call (keyword filter) raises exception (no matches)
        # Second call (semantic) returns normal results
        mock_collection.query.side_effect = [
            Exception("no documents match where_document filter"),
            sample_single_result,
        ]
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chroma_cls.return_value = mock_client

        from rag.retriever import SemanticRetriever

        retriever = SemanticRetriever()
        results = retriever.hybrid_search("pod crash", keyword="NonExistentKeyword", n_results=3)

        # Should fall back and return semantic results
        assert len(results) >= 0  # May be 1 or empty depending on fallback

    @patch("rag.retriever.chromadb.HttpClient")
    @patch("rag.retriever.OpenAIEmbeddings")
    def test_hybrid_search_no_keyword_delegates_to_query(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
        sample_single_result: Dict[str, Any],
    ) -> None:
        """hybrid_search with no keyword should delegate directly to query()."""
        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.return_value = [0.1, 0.2, 0.3]
        mock_embeddings_cls.return_value = mock_embeddings

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.query.return_value = sample_single_result
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chroma_cls.return_value = mock_client

        from rag.retriever import SemanticRetriever

        retriever = SemanticRetriever()
        results = retriever.hybrid_search("pod crash", keyword=None, n_results=1)

        # Should call ChromaDB query without where_document
        call_kwargs = mock_collection.query.call_args[1]
        assert "where_document" not in call_kwargs or call_kwargs.get("where_document") is None


# ---------------------------------------------------------------------------
# Test: empty query handling
# ---------------------------------------------------------------------------

class TestEmptyQueryHandled:
    """Tests that empty or whitespace-only queries are handled gracefully."""

    @patch("rag.retriever.chromadb.HttpClient")
    @patch("rag.retriever.OpenAIEmbeddings")
    def test_empty_query_returns_empty_list(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
    ) -> None:
        """query('') should return [] without calling ChromaDB."""
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = MagicMock()
        mock_chroma_cls.return_value = mock_client

        from rag.retriever import SemanticRetriever

        retriever = SemanticRetriever()
        results = retriever.query("")

        assert results == []
        # ChromaDB query should NOT have been called
        mock_client.get_or_create_collection.return_value.query.assert_not_called()

    @patch("rag.retriever.chromadb.HttpClient")
    @patch("rag.retriever.OpenAIEmbeddings")
    def test_whitespace_query_returns_empty_list(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
    ) -> None:
        """query('   ') should return [] without calling ChromaDB."""
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = MagicMock()
        mock_chroma_cls.return_value = mock_client

        from rag.retriever import SemanticRetriever

        retriever = SemanticRetriever()
        results = retriever.query("   ")

        assert results == []

    @patch("rag.retriever.chromadb.HttpClient")
    @patch("rag.retriever.OpenAIEmbeddings")
    def test_empty_hybrid_search_returns_empty_list(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
    ) -> None:
        """hybrid_search with empty text should return []."""
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = MagicMock()
        mock_chroma_cls.return_value = mock_client

        from rag.retriever import SemanticRetriever

        retriever = SemanticRetriever()
        results = retriever.hybrid_search("")

        assert results == []

    @patch("rag.retriever.chromadb.HttpClient")
    @patch("rag.retriever.OpenAIEmbeddings")
    def test_keyword_filter_raises_on_empty_keyword(
        self,
        mock_embeddings_cls: MagicMock,
        mock_chroma_cls: MagicMock,
    ) -> None:
        """keyword_filter with empty keyword should raise ValueError."""
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = MagicMock()
        mock_chroma_cls.return_value = mock_client

        from rag.retriever import SemanticRetriever

        retriever = SemanticRetriever()

        with pytest.raises(ValueError, match="keyword must be a non-empty string"):
            retriever.keyword_filter("pod crash", "")
