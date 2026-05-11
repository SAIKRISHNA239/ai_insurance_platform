"""
backend/config.py
─────────────────
Centralised, type-validated application settings powered by pydantic-settings.
All values are read from environment variables (or a .env file).
"""

from functools import lru_cache
from typing import Literal

from pydantic import AnyUrl, Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────
    app_name: str = "AI Healthcare Insurance Intelligence Platform"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    # ── Database ────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://insurance_user:insurance_pass@localhost:5432/insurance_db",
        description="Async SQLAlchemy DSN for PostgreSQL via asyncpg",
    )
    # Connection pool tuning
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_pre_ping: bool = True
    db_echo: bool = False  # set True to log all SQL queries

    # ── JWT / Auth ─────────────────────────────────────────────────────────
    secret_key: str = Field(..., description="HS256 signing secret — must be set in .env")
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # ── ChromaDB ───────────────────────────────────────────────────────────
    chroma_host: str = "chromadb"
    chroma_port: int = 8000
    chroma_collection_claims: str = "claims_vectors"
    chroma_collection_policies: str = "policy_vectors"

    # ── LLM Provider ──────────────────────────────────────────────────────
    llm_provider: Literal["openai", "gemini", "bedrock"] = "openai"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # ── Embedding ─────────────────────────────────────────────────────────
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536


    # ── Redis (Semantic Cache) ─────────────────────────────────────────────
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0
    cache_ttl_seconds: int = 3600
    cache_similarity_threshold: float = 0.92
    cache_max_entries_per_tenant: int = 10000
    cache_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # ── Hybrid Search ─────────────────────────────────────────────────────
    hybrid_dense_top_k: int = 20
    hybrid_sparse_top_k: int = 20
    hybrid_rrf_top_k: int = 10
    hybrid_rrf_k: int = 60

    # ── Cross-Encoder Reranker ────────────────────────────────────────────
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_score_threshold: float = 0.0
    reranker_final_top_k: int = 5

    # ── Query Expansion ───────────────────────────────────────────────────
    query_expansion_max_variants: int = 3
    query_expansion_backend: str = "dictionary"  # 'dictionary' | 'llm'

    # ── Sanitizer ─────────────────────────────────────────────────────────
    sanitizer_backend: str = "regex"  # 'regex' | 'presidio'

    # ── Derived helpers ───────────────────────────────────────────────────
    @property
    def chroma_url(self) -> str:
        return f"http://{self.chroma_host}:{self.chroma_port}"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
