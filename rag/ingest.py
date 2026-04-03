"""
rag/ingest.py
-------------
RAG ingestion pipeline for the Agentic SRE Platform.

Loads all Markdown runbooks from the rag/runbooks/ directory, splits them into
overlapping chunks, embeds them with OpenAI, and stores the resulting vectors
in a ChromaDB collection named ``sre_runbooks``.

Usage::

    python -m rag.ingest
    # or
    python rag/ingest.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RUNBOOKS_DIR: Path = Path(__file__).parent / "runbooks"
CHROMA_HOST: str = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT: int = int(os.getenv("CHROMA_PORT", "8000"))
COLLECTION_NAME: str = "sre_runbooks"

CHUNK_SIZE: int = 500
CHUNK_OVERLAP: int = 50

# Mapping from filename stems to category tags
_CATEGORY_MAP: dict[str, str] = {
    "crashloopbackoff": "kubernetes",
    "high-cpu-usage": "performance",
    "pod-oom-killed": "kubernetes",
    "deployment-failed": "kubernetes",
    "node-not-ready": "kubernetes",
    "pvc-pending": "kubernetes",
    "service-unavailable": "networking",
    "etcd-backup": "infrastructure",
    "alert-fatigue": "observability",
    "database-connection": "database",
}

# Mapping from filename stems to severity
_SEVERITY_MAP: dict[str, str] = {
    "crashloopbackoff": "high",
    "high-cpu-usage": "medium",
    "pod-oom-killed": "high",
    "deployment-failed": "high",
    "node-not-ready": "critical",
    "pvc-pending": "medium",
    "service-unavailable": "high",
    "etcd-backup": "critical",
    "alert-fatigue": "low",
    "database-connection": "high",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_severity_from_content(content: str) -> str:
    """
    Attempt to extract a severity level from the Markdown front matter or body.

    Looks for patterns like ``severity: high`` or ``**Severity:** medium``.

    Args:
        content: Raw Markdown text of the runbook.

    Returns:
        Severity string (critical|high|medium|low) or ``"medium"`` as default.
    """
    pattern = re.compile(
        r"severity[:\s]+(['\"]?)(critical|high|medium|low)\1",
        re.IGNORECASE,
    )
    match = pattern.search(content)
    if match:
        return match.group(2).lower()
    return "medium"


def _extract_category_from_content(content: str) -> str:
    """
    Attempt to extract a category from the Markdown body.

    Looks for patterns like ``category: kubernetes``.

    Args:
        content: Raw Markdown text.

    Returns:
        Category string or ``"general"`` as default.
    """
    pattern = re.compile(r"category[:\s]+(['\"]?)(\w[\w-]*)\1", re.IGNORECASE)
    match = pattern.search(content)
    if match:
        return match.group(2).lower()
    return "general"


def load_runbooks(runbooks_dir: Path = RUNBOOKS_DIR) -> list[dict[str, Any]]:
    """
    Load all ``.md`` files from *runbooks_dir* and return a list of document dicts.

    Each dict has keys:
        - ``text``: raw Markdown content
        - ``runbook_name``: filename stem (e.g. ``"crashloopbackoff"``)
        - ``severity``: extracted or mapped severity level
        - ``category``: extracted or mapped category

    Args:
        runbooks_dir: Directory containing Markdown runbook files.

    Returns:
        List of document dicts, one per runbook file.

    Raises:
        FileNotFoundError: If *runbooks_dir* does not exist.
    """
    if not runbooks_dir.exists():
        raise FileNotFoundError(f"Runbooks directory not found: {runbooks_dir}")

    documents: list[dict[str, Any]] = []
    md_files = sorted(runbooks_dir.glob("*.md"))

    if not md_files:
        logger.warning("No .md files found in {}", runbooks_dir)
        return documents

    for md_path in md_files:
        logger.info("Loading runbook: {}", md_path.name)
        try:
            content = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read {}: {}", md_path, exc)
            continue

        stem = md_path.stem  # e.g. "crashloopbackoff"

        # Prefer explicit map; fall back to content extraction
        severity = _SEVERITY_MAP.get(stem) or _extract_severity_from_content(content)
        category = _CATEGORY_MAP.get(stem) or _extract_category_from_content(content)

        documents.append(
            {
                "text": content,
                "runbook_name": stem,
                "severity": severity,
                "category": category,
            }
        )

    logger.info("Loaded {} runbook(s) from {}", len(documents), runbooks_dir)
    return documents


def split_documents(
    documents: list[dict[str, Any]],
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[dict[str, Any]]:
    """
    Split runbook documents into overlapping text chunks.

    Each input document produces one or more chunks. Metadata (runbook_name,
    severity, category) is preserved on every chunk.

    Args:
        documents: List of document dicts as returned by :func:`load_runbooks`.
        chunk_size: Maximum number of characters per chunk.
        chunk_overlap: Number of characters of overlap between consecutive chunks.

    Returns:
        List of chunk dicts with keys: ``text``, ``runbook_name``, ``severity``,
        ``category``, ``chunk_index``.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n## ", "\n### ", "\n\n", "\n", " ", ""],
    )

    chunks: list[dict[str, Any]] = []
    for doc in documents:
        raw_chunks = splitter.split_text(doc["text"])
        for idx, chunk_text in enumerate(raw_chunks):
            chunks.append(
                {
                    "text": chunk_text,
                    "runbook_name": doc["runbook_name"],
                    "severity": doc["severity"],
                    "category": doc["category"],
                    "chunk_index": idx,
                }
            )

    logger.info(
        "Split {} document(s) into {} chunk(s) (chunk_size={}, overlap={})",
        len(documents),
        len(chunks),
        chunk_size,
        chunk_overlap,
    )
    return chunks


def embed_and_store(
    chunks: list[dict[str, Any]],
    collection_name: str = COLLECTION_NAME,
    chroma_host: str = CHROMA_HOST,
    chroma_port: int = CHROMA_PORT,
) -> int:
    """
    Embed text chunks with OpenAI and upsert them into ChromaDB.

    Embeddings are generated in a single batch call. Each chunk is assigned a
    deterministic ID based on ``runbook_name`` and ``chunk_index`` so that
    re-running ingestion is idempotent.

    Args:
        chunks: List of chunk dicts as returned by :func:`split_documents`.
        collection_name: Name of the ChromaDB collection to upsert into.
        chroma_host: Hostname of the ChromaDB server.
        chroma_port: Port of the ChromaDB server.

    Returns:
        Number of chunks successfully upserted.

    Raises:
        ValueError: If *chunks* is empty.
        chromadb.errors.ChromaError: On ChromaDB connectivity issues.
    """
    if not chunks:
        raise ValueError("No chunks to embed — aborting.")

    logger.info(
        "Connecting to ChromaDB at {}:{} — collection={}",
        chroma_host,
        chroma_port,
        collection_name,
    )

    client = chromadb.HttpClient(
        host=chroma_host,
        port=chroma_port,
        settings=Settings(anonymized_telemetry=False),
    )

    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )

    # Generate embeddings
    embeddings_model = OpenAIEmbeddings(
        model="text-embedding-3-small",
        api_key=os.getenv("OPENAI_API_KEY"),
    )

    texts = [c["text"] for c in chunks]
    logger.info("Generating embeddings for {} chunks …", len(texts))
    vectors = embeddings_model.embed_documents(texts)
    logger.info("Embeddings generated — shape: {}×{}", len(vectors), len(vectors[0]))

    # Build ChromaDB upsert payload
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, Any]] = []
    embeddings: list[list[float]] = []

    for chunk, vector in zip(chunks, vectors):
        chunk_id = f"{chunk['runbook_name']}__chunk_{chunk['chunk_index']:04d}"
        ids.append(chunk_id)
        documents.append(chunk["text"])
        metadatas.append(
            {
                "runbook_name": chunk["runbook_name"],
                "severity": chunk["severity"],
                "category": chunk["category"],
                "chunk_index": chunk["chunk_index"],
            }
        )
        embeddings.append(vector)

    # Upsert in batches of 100 to avoid request size limits
    batch_size = 100
    total_upserted = 0
    for start in range(0, len(ids), batch_size):
        end = start + batch_size
        collection.upsert(
            ids=ids[start:end],
            documents=documents[start:end],
            metadatas=metadatas[start:end],
            embeddings=embeddings[start:end],
        )
        batch_count = len(ids[start:end])
        total_upserted += batch_count
        logger.info(
            "Upserted batch {}/{} ({} chunks)",
            start // batch_size + 1,
            (len(ids) + batch_size - 1) // batch_size,
            batch_count,
        )

    logger.success(
        "Ingestion complete — {} chunks stored in collection '{}'",
        total_upserted,
        collection_name,
    )
    return total_upserted


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Orchestrate the full RAG ingestion pipeline.

    Steps:
        1. Load all ``.md`` runbooks from ``rag/runbooks/``.
        2. Split into overlapping chunks.
        3. Embed with OpenAI ``text-embedding-3-small``.
        4. Upsert into ChromaDB collection ``sre_runbooks``.

    Environment variables:
        OPENAI_API_KEY: Required for embedding generation.
        CHROMA_HOST: ChromaDB hostname (default: ``localhost``).
        CHROMA_PORT: ChromaDB port (default: ``8000``).
    """
    logger.info("=== SRE RAG Ingestion Pipeline starting ===")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY environment variable is not set — aborting.")
        raise EnvironmentError("OPENAI_API_KEY is required for embedding generation.")

    # Step 1 — Load
    documents = load_runbooks()
    if not documents:
        logger.error("No runbooks found — nothing to ingest.")
        return

    # Step 2 — Split
    chunks = split_documents(documents)

    # Step 3 & 4 — Embed + Store
    total = embed_and_store(chunks)

    logger.success("=== Pipeline finished — {} chunks ingested ===", total)


if __name__ == "__main__":
    main()
