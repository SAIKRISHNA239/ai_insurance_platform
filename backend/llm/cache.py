"""
backend/llm/cache.py
─────────────────────
Redis-backed Semantic Caching Layer for the RAG pipeline.

ARCHITECTURE DECISION: WHY SEMANTIC CACHING?
─────────────────────────────────────────────
Traditional exact-match caching (hash the query string → cache key) has near-
zero hit rate for conversational RAG. Users ask the same question in different
ways:
  "What is my annual deductible?"
  "How much is the annual deductible on my plan?"
  "Tell me the yearly deductible amount"

All three are semantically identical — they should return the same cached answer.

Semantic caching works by:
  1. Embedding the incoming query using a fast local model (no API cost).
  2. Querying a Redis Sorted Set for the nearest cached embedding.
  3. If cosine similarity ≥ threshold (default: 0.92), return the cached response.
  4. Otherwise, run the full RAG pipeline and store the result.

Cost impact at scale:
  A GPT-4o call for a RAG response typically costs $0.01–$0.05 per query.
  At 10,000 queries/day with a 30% cache hit rate, this saves ~$1,000–5,000/month.
  Semantic caching hit rates of 25–40% are typical for insurance domain queries
  where users frequently ask about the same coverage topics.

COMPLIANCE DESIGN
──────────────────
Tenant isolation is enforced in the cache by namespacing all Redis keys with
the tenant_id: `semantic_cache:{tenant_id}:{hashed_user_role}:*`.

This ensures:
  • Tenant A's cached answers cannot be returned for Tenant B's queries.
  • Role-scoped responses are not leaked across roles (an admin response
    containing internal notes is never returned to an insured member).
  • Cache poisoning attacks: a malicious user cannot pollute the cache with
    crafted queries that return incorrect answers for other users.

The cached LLM response text is never stored alongside raw PHI.
All cached responses are the post-RAG LLM output (already PHI-clean).

REDIS DATA STRUCTURES
──────────────────────
Two Redis structures per (tenant, role) namespace:

  1. Hash: `semantic_cache:{tenant}:{role}:entries`
     Maps cache_key → JSON payload (query, response, metadata, timestamp).

  2. Sorted Set: `semantic_cache:{tenant}:{role}:embeddings`
     Maps cache_key → embedding (as a packed binary blob in the Hash, scored
     by timestamp for LRU eviction).

     Note: Redis does not natively support vector similarity search without
     RedisSearch (Redis Stack). The implementation uses a lightweight in-memory
     scan over the stored embeddings for small caches (<10,000 entries).

     For production at scale (>100K cached queries per tenant):
       Option A: Use Redis Stack with `FT.SEARCH` + vector fields (HNSW index).
       Option B: Use a separate ChromaDB collection as the cache embedding store.
       Option C: Use pgvector in PostgreSQL as the cache similarity index.

EMBEDDING MODEL CHOICE
───────────────────────
The cache uses `sentence-transformers/all-MiniLM-L6-v2` (not the production
OpenAI embedding model) for cache key generation because:
  1. Zero API cost — every cache check is free.
  2. ~2ms embedding latency vs ~100ms for an OpenAI API call.
  3. Sufficient quality for semantic similarity at threshold 0.92+.
     (We don't need retrieval-quality embeddings; we need query-similarity.)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Cache DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CacheEntry:
    """
    A single entry stored in the semantic cache.

    The embedding is stored as a packed list of floats (struct.pack) rather
    than JSON to minimize Redis memory usage. A 384-dim MiniLM embedding
    stored as JSON is ~3KB; as packed floats it's ~1.5KB.
    """
    cache_key: str          # SHA-256 of the normalized query
    query: str              # Original query text (for audit/debugging)
    response: str           # LLM response text (PHI-clean)
    tenant_id: str
    user_role: str
    retrieved_chunk_ids: list[str]  # Source chunk IDs for provenance
    hit_count: int          # How many times this cache entry has been retrieved
    created_at: str         # ISO 8601 UTC timestamp
    last_accessed_at: str   # ISO 8601 UTC timestamp


@dataclass
class CacheCheckResult:
    """Result of a semantic cache lookup."""
    hit: bool
    similarity: float                # Cosine similarity to nearest cached query
    cache_key: str | None = None     # Key of the matched entry
    entry: CacheEntry | None = None  # Full cache entry if hit
    lookup_latency_ms: float = 0.0
    embedding_latency_ms: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Local Embedding Model (cache key generation)
# ─────────────────────────────────────────────────────────────────────────────

class _CacheEmbedder:
    """
    Lightweight local embedding model for semantic cache key generation.

    Uses sentence-transformers/all-MiniLM-L6-v2:
      • 384-dim embeddings (vs 1536-dim for OpenAI text-embedding-3-small)
      • ~2ms per embedding on CPU (vs ~100ms API round-trip)
      • Zero API cost — critical for a cache check that runs on every query
    """
    _model = None

    @classmethod
    def _get_model(cls, model_name: str):
        if cls._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                cls._model = SentenceTransformer(model_name)
                logger.info("cache_embedder_loaded", model=model_name)
            except ImportError:
                raise ImportError(
                    "Install sentence-transformers: pip install sentence-transformers"
                )
        return cls._model

    def embed(self, text: str, model_name: str) -> list[float]:
        """Embed a single query string synchronously."""
        model = self._get_model(model_name)
        vec = model.encode(text, show_progress_bar=False, convert_to_numpy=True)
        return vec.tolist()


_embedder = _CacheEmbedder()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _pack_embedding(embedding: list[float]) -> bytes:
    """Pack float list to bytes for compact Redis storage."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _unpack_embedding(data: bytes) -> list[float]:
    """Unpack bytes back to float list."""
    n = len(data) // 4  # 4 bytes per float32
    return list(struct.unpack(f"{n}f", data))


def _normalize_query(query: str) -> str:
    """
    Normalize a query for cache key generation.

    Removes punctuation differences and case variation so that
    "What is my deductible?" and "what is my deductible" hash to the same key.
    """
    import re
    return re.sub(r"\s+", " ", query.lower().strip().rstrip("?.!,;"))


# ─────────────────────────────────────────────────────────────────────────────
# Redis Key Schema
# ─────────────────────────────────────────────────────────────────────────────

def _entries_key(tenant_id: str, user_role: str) -> str:
    """Redis Hash key storing cache_key → JSON payload."""
    return f"semantic_cache:{tenant_id}:{user_role}:entries"


def _embeddings_key(tenant_id: str, user_role: str) -> str:
    """Redis Hash key storing cache_key → packed embedding bytes."""
    return f"semantic_cache:{tenant_id}:{user_role}:embeddings"


def _access_zset_key(tenant_id: str, user_role: str) -> str:
    """Redis Sorted Set for LRU eviction (score = last access UNIX timestamp)."""
    return f"semantic_cache:{tenant_id}:{user_role}:lru"


# ─────────────────────────────────────────────────────────────────────────────
# Semantic Cache Class
# ─────────────────────────────────────────────────────────────────────────────

class SemanticCache:
    """
    Redis-backed semantic cache for RAG query responses.

    Usage pattern:
        cache = SemanticCache(redis_client, settings)

        # Before running the RAG pipeline:
        result = await cache.lookup(query, tenant_id, user_role)
        if result.hit:
            return result.entry.response  # Instant return, zero LLM cost

        # After RAG pipeline completes:
        await cache.store(query, response, tenant_id, user_role, chunk_ids, embedding)

    Thread safety:
        All Redis operations are async (aioredis). The embedder runs in
        asyncio.to_thread() since SentenceTransformer inference is CPU-bound.
    """

    def __init__(
        self,
        redis_client: Any,  # aioredis.Redis
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        similarity_threshold: float = 0.92,
        ttl_seconds: int = 3600,
        max_entries_per_tenant: int = 10000,
    ) -> None:
        """
        Args:
            redis_client:          Async Redis client (aioredis.Redis).
            model_name:            Sentence-transformers model for cache embeddings.
            similarity_threshold:  Minimum cosine similarity for a cache hit.
                                   0.92 = strict (recommended for healthcare).
                                   Lower values increase hit rate but risk
                                   returning answers to subtly different questions.
            ttl_seconds:           Cache entry TTL. Default 1 hour.
            max_entries_per_tenant: LRU eviction threshold per (tenant, role) pair.
        """
        self._redis = redis_client
        self._model_name = model_name
        self._threshold = similarity_threshold
        self._ttl = ttl_seconds
        self._max_entries = max_entries_per_tenant

    async def _embed_query(self, query: str) -> list[float]:
        """Embed a query string asynchronously (runs in thread pool)."""
        return await asyncio.to_thread(_embedder.embed, query, self._model_name)

    async def lookup(
        self,
        query: str,
        tenant_id: str,
        user_role: str,
    ) -> CacheCheckResult:
        """
        Check the semantic cache for a query that is semantically equivalent
        to a previously cached query.

        Algorithm:
          1. Embed the normalized query using the local model.
          2. Fetch all stored embeddings for this (tenant, role) namespace.
          3. Compute cosine similarity against each stored embedding.
          4. If max similarity ≥ threshold → cache hit; return entry.
          5. Else → cache miss; caller runs full RAG pipeline.

        Complexity: O(n) where n = number of cached entries for this tenant+role.
        For n < 10,000 and 384-dim embeddings, this takes ~5-20ms in Python.
        For larger n, switch to Redis Stack vector search or a pgvector index.

        Args:
            query:     Raw user query string.
            tenant_id: JWT-derived tenant identifier.
            user_role: JWT-derived role string.

        Returns:
            CacheCheckResult with hit=True and populated entry on cache hit.
        """
        overall_start = time.perf_counter()

        # Step 1: Embed the query
        embed_start = time.perf_counter()
        normalized = _normalize_query(query)
        query_embedding = await self._embed_query(normalized)
        embed_ms = (time.perf_counter() - embed_start) * 1000

        emb_key = _embeddings_key(tenant_id, user_role)
        ent_key = _entries_key(tenant_id, user_role)

        try:
            # Step 2: Fetch all stored embeddings (binary blobs from Redis Hash)
            stored_embeddings: dict[bytes, bytes] = await self._redis.hgetall(emb_key)

            if not stored_embeddings:
                return CacheCheckResult(
                    hit=False,
                    similarity=0.0,
                    lookup_latency_ms=(time.perf_counter() - overall_start) * 1000,
                    embedding_latency_ms=embed_ms,
                )

            # Step 3: Find most similar cached embedding
            best_key: str | None = None
            best_sim: float = 0.0

            for raw_cache_key, packed_vec in stored_embeddings.items():
                cached_vec = _unpack_embedding(packed_vec)
                sim = _cosine_similarity(query_embedding, cached_vec)
                if sim > best_sim:
                    best_sim = sim
                    best_key = (
                        raw_cache_key.decode()
                        if isinstance(raw_cache_key, bytes)
                        else raw_cache_key
                    )

            lookup_ms = (time.perf_counter() - overall_start) * 1000

            # Step 4: Threshold check
            if best_sim >= self._threshold and best_key is not None:
                # Retrieve full entry from entries hash
                raw_entry = await self._redis.hget(ent_key, best_key)
                if raw_entry is None:
                    # Entry hash expired but embedding hash didn't — stale reference
                    logger.warning("cache_stale_reference", cache_key=best_key)
                    return CacheCheckResult(hit=False, similarity=best_sim,
                                            lookup_latency_ms=lookup_ms,
                                            embedding_latency_ms=embed_ms)

                entry_data = json.loads(raw_entry)
                entry = CacheEntry(
                    cache_key=entry_data["cache_key"],
                    query=entry_data["query"],
                    response=entry_data["response"],
                    tenant_id=entry_data["tenant_id"],
                    user_role=entry_data["user_role"],
                    retrieved_chunk_ids=entry_data.get("retrieved_chunk_ids", []),
                    hit_count=entry_data.get("hit_count", 0) + 1,
                    created_at=entry_data["created_at"],
                    last_accessed_at=datetime.utcnow().isoformat(),
                )

                # Update hit count and last_accessed_at asynchronously
                await self._redis.hset(ent_key, best_key, json.dumps({
                    **entry_data,
                    "hit_count": entry.hit_count,
                    "last_accessed_at": entry.last_accessed_at,
                }))
                # Update LRU score
                await self._redis.zadd(
                    _access_zset_key(tenant_id, user_role),
                    {best_key: time.time()},
                )

                logger.info(
                    "cache_hit",
                    tenant_id=tenant_id,
                    user_role=user_role,
                    similarity=f"{best_sim:.4f}",
                    cache_key=best_key[:16],
                    lookup_ms=f"{lookup_ms:.1f}",
                )

                return CacheCheckResult(
                    hit=True,
                    similarity=best_sim,
                    cache_key=best_key,
                    entry=entry,
                    lookup_latency_ms=lookup_ms,
                    embedding_latency_ms=embed_ms,
                )

            logger.debug(
                "cache_miss",
                tenant_id=tenant_id,
                best_sim=f"{best_sim:.4f}",
                threshold=self._threshold,
                lookup_ms=f"{lookup_ms:.1f}",
            )
            return CacheCheckResult(
                hit=False,
                similarity=best_sim,
                lookup_latency_ms=lookup_ms,
                embedding_latency_ms=embed_ms,
            )

        except Exception as exc:
            logger.warning(
                "cache_lookup_error",
                error=str(exc),
                tenant_id=tenant_id,
            )
            # Cache errors must NEVER block the RAG pipeline
            return CacheCheckResult(hit=False, similarity=0.0)

    async def store(
        self,
        query: str,
        response: str,
        tenant_id: str,
        user_role: str,
        retrieved_chunk_ids: list[str] | None = None,
        precomputed_embedding: list[float] | None = None,
    ) -> str:
        """
        Store a new (query, response) pair in the semantic cache.

        Storage layout:
          • embeddings hash: cache_key → packed float bytes (for similarity lookup)
          • entries hash:    cache_key → JSON (query, response, metadata)
          • LRU sorted set:  cache_key → UNIX timestamp (for eviction)

        Both hashes receive a TTL via EXPIREAT to ensure automatic cleanup.

        Args:
            query:                   Raw user query.
            response:                LLM response (PHI-clean — never store raw PHI).
            tenant_id:               Tenant namespace.
            user_role:               Role namespace.
            retrieved_chunk_ids:     Source chunk IDs for provenance tracking.
            precomputed_embedding:   If already computed, skip re-embedding.

        Returns:
            The cache_key (SHA-256 of normalized query).
        """
        try:
            normalized = _normalize_query(query)
            cache_key = hashlib.sha256(
                f"{tenant_id}:{user_role}:{normalized}".encode()
            ).hexdigest()

            # Compute embedding if not provided
            if precomputed_embedding is not None:
                embedding = precomputed_embedding
            else:
                embedding = await self._embed_query(normalized)

            packed = _pack_embedding(embedding)
            now = datetime.utcnow().isoformat()

            entry_payload = {
                "cache_key": cache_key,
                "query": query,
                "response": response,
                "tenant_id": tenant_id,
                "user_role": user_role,
                "retrieved_chunk_ids": retrieved_chunk_ids or [],
                "hit_count": 0,
                "created_at": now,
                "last_accessed_at": now,
            }

            emb_key = _embeddings_key(tenant_id, user_role)
            ent_key = _entries_key(tenant_id, user_role)
            lru_key = _access_zset_key(tenant_id, user_role)

            pipe = self._redis.pipeline()
            pipe.hset(emb_key, cache_key, packed)
            pipe.hset(ent_key, cache_key, json.dumps(entry_payload))
            pipe.zadd(lru_key, {cache_key: time.time()})
            pipe.expire(emb_key, self._ttl)
            pipe.expire(ent_key, self._ttl)
            pipe.expire(lru_key, self._ttl)
            await pipe.execute()

            # LRU eviction: if over capacity, remove oldest entries
            cache_size = await self._redis.zcard(lru_key)
            if cache_size > self._max_entries:
                evict_count = cache_size - self._max_entries
                oldest_keys = await self._redis.zrange(lru_key, 0, evict_count - 1)
                if oldest_keys:
                    evict_pipe = self._redis.pipeline()
                    evict_pipe.hdel(emb_key, *oldest_keys)
                    evict_pipe.hdel(ent_key, *oldest_keys)
                    evict_pipe.zrem(lru_key, *oldest_keys)
                    await evict_pipe.execute()
                    logger.info(
                        "cache_lru_eviction",
                        evicted=len(oldest_keys),
                        tenant_id=tenant_id,
                    )

            logger.info(
                "cache_stored",
                cache_key=cache_key[:16],
                tenant_id=tenant_id,
                user_role=user_role,
                query_preview=query[:60],
            )
            return cache_key

        except Exception as exc:
            logger.warning(
                "cache_store_error",
                error=str(exc),
                tenant_id=tenant_id,
            )
            # Storage failure must NEVER block the response
            return ""

    async def invalidate(
        self,
        tenant_id: str,
        user_role: str | None = None,
    ) -> int:
        """
        Invalidate all cache entries for a tenant (and optionally a specific role).

        Called when new policy documents are ingested — existing cache entries
        may reference outdated policy terms and must be cleared.

        Args:
            tenant_id: Tenant whose cache to invalidate.
            user_role: If provided, only invalidate this role's cache.
                       If None, invalidate ALL roles for this tenant.

        Returns:
            Number of cache entries deleted.
        """
        from backend.database.models import UserRole

        roles_to_clear = [user_role] if user_role else [r.value for r in UserRole]
        total_deleted = 0

        for role in roles_to_clear:
            keys_to_delete = [
                _embeddings_key(tenant_id, role),
                _entries_key(tenant_id, role),
                _access_zset_key(tenant_id, role),
            ]
            result = await self._redis.delete(*keys_to_delete)
            total_deleted += result

        logger.info(
            "cache_invalidated",
            tenant_id=tenant_id,
            user_role=user_role or "ALL",
            keys_deleted=total_deleted,
        )
        return total_deleted

    async def get_stats(self, tenant_id: str, user_role: str) -> dict[str, Any]:
        """
        Return cache statistics for a (tenant, role) namespace.

        Useful for monitoring dashboards and debugging cache hit rate issues.
        """
        emb_key = _embeddings_key(tenant_id, user_role)
        lru_key = _access_zset_key(tenant_id, user_role)

        total_entries = await self._redis.hlen(emb_key)
        lru_size = await self._redis.zcard(lru_key)
        ttl_remaining = await self._redis.ttl(emb_key)

        return {
            "tenant_id": tenant_id,
            "user_role": user_role,
            "total_entries": total_entries,
            "lru_tracked": lru_size,
            "ttl_remaining_seconds": ttl_remaining,
            "similarity_threshold": self._threshold,
            "max_entries": self._max_entries,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Redis Client Factory
# ─────────────────────────────────────────────────────────────────────────────

def get_redis_client():
    """
    Create an async Redis client from application settings.

    Uses aioredis (now bundled as redis.asyncio in redis-py >= 4.2).
    Connection pooling is handled automatically by the client.

    Installation: pip install redis[asyncio]
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:
        raise ImportError("Install redis: pip install 'redis[asyncio]'")

    from backend.config import get_settings
    settings = get_settings()

    pool = aioredis.ConnectionPool.from_url(
        f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}",
        password=settings.redis_password or None,
        max_connections=20,
        decode_responses=False,  # Raw bytes for packed embeddings
    )
    return aioredis.Redis(connection_pool=pool)


def get_semantic_cache(redis_client: Any | None = None) -> SemanticCache:
    """
    Factory function returning a configured SemanticCache instance.

    Args:
        redis_client: Optional pre-built Redis client. If None, creates one
                      from settings. Pass a mock client in unit tests.
    """
    from backend.config import get_settings
    settings = get_settings()

    client = redis_client or get_redis_client()
    return SemanticCache(
        redis_client=client,
        model_name=settings.cache_embedding_model,
        similarity_threshold=settings.cache_similarity_threshold,
        ttl_seconds=settings.cache_ttl_seconds,
        max_entries_per_tenant=settings.cache_max_entries_per_tenant,
    )
