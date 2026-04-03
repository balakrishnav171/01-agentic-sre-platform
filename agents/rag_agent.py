"""
RAG retrieval agent for SRE runbook lookup using ChromaDB.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import List, Optional
import chromadb
from chromadb.config import Settings
from langchain_openai import OpenAIEmbeddings
from loguru import logger


@dataclass
class RunbookResult:
    title: str
    content: str
    similarity_score: float
    category: str
    severity: str
    runbook_name: str


class RAGAgent:
    """Agent that retrieves relevant runbooks from ChromaDB for SRE alerts."""

    def __init__(self, host: str = "localhost", port: int = 8000, collection: str = "sre_runbooks"):
        self.client = chromadb.HttpClient(host=host, port=port)
        self.collection = self.client.get_or_create_collection(collection)
        self.embeddings = OpenAIEmbeddings()
        logger.info(f"RAGAgent initialized with collection={collection}")

    def query_runbooks(self, alert_description: str, n_results: int = 3) -> List[RunbookResult]:
        """Semantic search over runbooks for a given alert description."""
        embedding = self.embeddings.embed_query(alert_description)
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        runbooks = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            runbooks.append(
                RunbookResult(
                    title=meta.get("runbook_name", "Unknown"),
                    content=doc,
                    similarity_score=1.0 - dist,
                    category=meta.get("category", "general"),
                    severity=meta.get("severity", "medium"),
                    runbook_name=meta.get("runbook_name", "unknown"),
                )
            )
        logger.info(f"Retrieved {len(runbooks)} runbooks for: {alert_description[:60]}")
        return runbooks

    def get_remediation_steps(self, issue_type: str) -> List[str]:
        """Get step-by-step remediation for a known issue type."""
        results = self.query_runbooks(issue_type, n_results=1)
        if not results:
            return ["No runbook found. Escalate to on-call engineer."]
        lines = results[0].content.split("\n")
        steps = [
            l.strip()
            for l in lines
            if l.strip().startswith(("-", "*", "1", "2", "3", "4", "5", "6", "7", "8", "9"))
        ]
        return steps or [results[0].content[:500]]

    def hybrid_search(
        self,
        query: str,
        keyword: Optional[str] = None,
        n_results: int = 5,
    ) -> List[RunbookResult]:
        """Hybrid search: semantic + optional keyword filter."""
        embedding = self.embeddings.embed_query(query)
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
            where_document={"$contains": keyword} if keyword else None,
        )
        runbooks = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            runbooks.append(
                RunbookResult(
                    title=meta.get("runbook_name", "Unknown"),
                    content=doc,
                    similarity_score=1.0 - dist,
                    category=meta.get("category", "general"),
                    severity=meta.get("severity", "medium"),
                    runbook_name=meta.get("runbook_name", "unknown"),
                )
            )
        return runbooks
