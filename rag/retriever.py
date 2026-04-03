"""
rag/retriever.py
----------------
ChromaDB-backed semantic and hybrid retriever for SRE runbooks.

Provides :class:`SemanticRetriever` with three retrieval modes:

- :meth:`~SemanticRetriever.query` — pure semantic (vector) search.
- :meth:`~SemanticRetriever.keyword_filter` — semantic search with a mandatory
  keyword present in the document text.
- :meth:`~SemanticRetriever.hybrid_search` — semantic search with an optional
  keyword filter; combines both methods transparently.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import chromadb
from chromadb.config import Settings
from langchain_openai import OpenAIEmbeddings
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHROMA_HOST: str = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT: int = int(os.getenv("CHROMA_PORT", "8000"))
COLLECTION_NAME: str = "sre_runbooks"


# ---------------------------------------------------------------------------
# SemanticRetriever
# ---------------------------------------------------------------------------


class SemanticRetriever:
    """
    Retriever that queries ChromaDB using semantic (vector) search, with an
    optional keyword pre-filter for hybrid retrieval.

    Args:
        host: Hostname of the ChromaDB HTTP server.
        port: Port of the ChromaDB HTTP server.
        collection_name: Name of the ChromaDB collection to query.
        openai_api_key: Optional OpenAI API key override; falls back to the
            ``OPENAI_API_KEY`` environment variable.

    Example::

        retriever = SemanticRetriever()
        results = retriever.query("pod is crashing with OOMKilled", n_results=3)
        for r in results:
            print(r["runbook_name"], r["score"], r["content"][:80])
    """

    def __init__(
        self,
        host: str = CHROMA_HOST,
        port: int = CHROMA_PORT,
        collection_name: str = COLLECTION_NAME,
        openai_api_key: Optional[str] = None,
    ) -> None:
        api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self._embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small",
            api_key=api_key,
        )
        self._client = chromadb.HttpClient(
            host=host,
            port=port,
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            "SemanticRetriever ready — collection='{}' chroma={}:{}",
            collection_name,
            host,
            port,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> list[float]:
        """
        Generate an embedding vector for *text*.

        Args:
            text: Query string to embed.

        Returns:
            List of floats representing the embedding vector.
        """
        return self._embeddings.embed_query(text)

    @staticmethod
    def _format_results(
        documents: list[str],
        metadatas: list[dict[str, Any]],
        distances: list[float],
    ) -> list[dict[str, Any]]:
        """
        Convert raw ChromaDB result arrays into a list of result dicts.

        Args:
            documents: List of document text chunks.
            metadatas: List of metadata dicts parallel to *documents*.
            distances: List of cosine distances parallel to *documents*.

        Returns:
            List of dicts with keys: ``content``, ``runbook_name``, ``severity``,
            ``category``, ``chunk_index``, ``score``.
        """
        results: list[dict[str, Any]] = []
        for doc, meta, dist in zip(documents, metadatas, distances):
            results.append(
                {
                    "content": doc,
                    "runbook_name": meta.get("runbook_name", "unknown"),
                    "severity": meta.get("severity", "medium"),
                    "category": meta.get("category", "general"),
                    "chunk_index": meta.get("chunk_index", 0),
                    # cosine distance → similarity score
                    "score": round(1.0 - float(dist), 4),
                }
            )
        return results

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(self, text: str, n_results: int = 3) -> list[dict[str, Any]]:
        """
        Perform a pure semantic (vector cosine) search against ChromaDB.

        Args:
            text: Natural-language query string (e.g. an alert description).
            n_results: Maximum number of results to return.

        Returns:
            List of result dicts sorted by descending similarity score.  Each
            dict has keys: ``content``, ``runbook_name``, ``severity``,
            ``category``, ``chunk_index``, ``score``.

        Raises:
            chromadb.errors.ChromaError: If the ChromaDB query fails.
        """
        if not text or not text.strip():
            logger.warning("query() called with empty text — returning []")
            return []

        logger.debug("query | text={!r} n_results={}", text[:80], n_results)

        vector = self._embed(text)
        raw = self._collection.query(
            query_embeddings=[vector],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )

        results = self._format_results(
            documents=raw["documents"][0],
            metadatas=raw["metadatas"][0],
            distances=raw["distances"][0],
        )

        logger.info("query | returned {} result(s) for: {!r}", len(results), text[:60])
        return results

    def keyword_filter(self, text: str, keyword: str) -> list[dict[str, Any]]:
        """
        Semantic search restricted to documents containing *keyword*.

        Uses ChromaDB's ``where_document`` filter so only chunks whose text
        contains the exact *keyword* string (case-sensitive) are considered.

        Args:
            text: Natural-language query string.
            keyword: Substring that must appear in the document text.

        Returns:
            List of result dicts (same schema as :meth:`query`), possibly empty
            if no documents contain the keyword.

        Raises:
            ValueError: If *keyword* is empty.
        """
        if not keyword or not keyword.strip():
            raise ValueError("keyword must be a non-empty string")

        logger.debug(
            "keyword_filter | text={!r} keyword={!r}", text[:60], keyword
        )

        vector = self._embed(text)
        try:
            raw = self._collection.query(
                query_embeddings=[vector],
                n_results=10,
                include=["documents", "metadatas", "distances"],
                where_document={"$contains": keyword},
            )
        except Exception as exc:
            # ChromaDB raises if zero documents match the where_document filter
            logger.warning(
                "keyword_filter | ChromaDB error (likely zero matches): {}", exc
            )
            return []

        results = self._format_results(
            documents=raw["documents"][0],
            metadatas=raw["metadatas"][0],
            distances=raw["distances"][0],
        )

        logger.info(
            "keyword_filter | {} result(s) for keyword={!r}",
            len(results),
            keyword,
        )
        return results

    def hybrid_search(
        self,
        text: str,
        keyword: Optional[str] = None,
        n_results: int = 3,
    ) -> list[dict[str, Any]]:
        """
        Hybrid retrieval: semantic search with an optional keyword filter.

        When *keyword* is provided the method calls :meth:`keyword_filter` and
        returns up to *n_results* hits.  If no keyword-filtered results are
        found it transparently falls back to pure semantic search.  When no
        *keyword* is given it delegates directly to :meth:`query`.

        Args:
            text: Natural-language query string.
            keyword: Optional keyword that must appear in returned documents.
            n_results: Maximum number of results to return.

        Returns:
            List of result dicts (same schema as :meth:`query`), sorted by
            descending similarity score.
        """
        if not text or not text.strip():
            logger.warning("hybrid_search() called with empty text — returning []")
            return []

        logger.debug(
            "hybrid_search | text={!r} keyword={!r} n_results={}",
            text[:60],
            keyword,
            n_results,
        )

        if keyword:
            results = self.keyword_filter(text, keyword)
            if results:
                # Sort by score descending, cap at n_results
                results.sort(key=lambda r: r["score"], reverse=True)
                logger.info(
                    "hybrid_search | keyword path returned {} result(s)", len(results)
                )
                return results[:n_results]

            # Fallback: keyword filter returned nothing — use pure semantic
            logger.info(
                "hybrid_search | keyword={!r} produced 0 results — falling back to semantic",
                keyword,
            )

        # Pure semantic path
        results = self.query(text, n_results=n_results)
        logger.info(
            "hybrid_search | semantic path returned {} result(s)", len(results)
        )
        return results
