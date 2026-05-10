"""
backend/vectorstore/service.py
───────────────────────────────
ChromaDB CRUD abstractions for the semantic retrieval layer.

Provides upsert, query, and delete operations against named collections.
All methods take plain Python types (not ORM models) to keep this module
decoupled from SQLAlchemy.

Usage pattern:
  1. Embed a document via backend.embeddings.service.embed_text()
  2. Call vectorstore.service.upsert_document() with the embedding
  3. Query with vectorstore.service.query_similar() during RAG retrieval
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.database.vector_client import get_or_create_collection

logger = structlog.get_logger(__name__)


async def upsert_document(
    collection_name: str,
    document_id: str,
    text: str,
    embedding: list[float],
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Upsert a document + embedding into a ChromaDB collection.

    Args:
        collection_name: Target ChromaDB collection name.
        document_id: Unique identifier for this document (e.g., claim UUID).
        text: The raw text content (stored alongside the vector).
        embedding: Pre-computed embedding vector.
        metadata: Arbitrary key-value metadata for filtering.
    """
    collection = await get_or_create_collection(collection_name)
    await collection.upsert(
        ids=[document_id],
        documents=[text],
        embeddings=[embedding],
        metadatas=[metadata or {}],
    )
    logger.debug(
        "vector_upserted",
        collection=collection_name,
        document_id=document_id,
    )


async def query_similar(
    collection_name: str,
    query_embedding: list[float],
    n_results: int = 5,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Query a ChromaDB collection for the most similar documents.

    Args:
        collection_name: Collection to search.
        query_embedding: The query vector.
        n_results: Number of top results to return.
        where: Optional ChromaDB metadata filter (e.g., {"status": "approved"}).

    Returns:
        List of dicts with keys: id, document, metadata, distance.
    """
    collection = await get_or_create_collection(collection_name)
    results = await collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    output = []
    for i, doc_id in enumerate(results["ids"][0]):
        output.append({
            "id": doc_id,
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i],
        })

    logger.debug(
        "vector_query_complete",
        collection=collection_name,
        n_results=len(output),
    )
    return output


async def delete_document(collection_name: str, document_id: str) -> None:
    """Delete a single document from a ChromaDB collection by ID."""
    collection = await get_or_create_collection(collection_name)
    await collection.delete(ids=[document_id])
    logger.info("vector_deleted", collection=collection_name, document_id=document_id)
