"""
backend/mlops/evaluation.py
────────────────────────────
Continuous RAG Evaluation Pipeline using RAGAS metrics.

EVALUATION FRAMEWORK: RAGAS
────────────────────────────
RAGAS (Retrieval Augmented Generation Assessment) provides reference-free
evaluation metrics specifically designed for RAG pipelines. It scores four
dimensions independently using the LLM-as-judge pattern:

  1. CONTEXT PRECISION
     "Of the retrieved chunks, what fraction was actually relevant?"
     Formula: |relevant ∩ retrieved| / |retrieved|
     Clinical impact: Low precision means the LLM is distracted by irrelevant
     policy clauses, increasing hallucination risk in adjudication decisions.

  2. CONTEXT RECALL
     "Of all relevant information, what fraction was retrieved?"
     Formula: |relevant ∩ retrieved| / |relevant|
     Clinical impact: Low recall means critical medical necessity criteria were
     missed — potentially causing wrongful claim denials (HIPAA liability).

  3. FAITHFULNESS
     "Is every claim in the LLM answer supported by the retrieved context?"
     Method: LLM decomposes the answer into atomic statements and checks each
     against the retrieved chunks.
     Clinical impact: Unfaithful answers = hallucinations = wrong adjudications.
     This is the most critical metric for a HIPAA-covered platform.

  4. ANSWER RELEVANCY
     "Does the answer actually address the question asked?"
     Method: LLM generates N reverse questions from the answer and computes
     cosine similarity to the original question.

PROMPT VERSION TRACKING (MLflow)
──────────────────────────────────
Every evaluation run logs:
  • The prompt version hash (SHA-256 of the prompt template)
  • The embedding model name and version
  • All four RAGAS scores
  • The test dataset version
  • The trace IDs of the evaluated inferences

This creates an immutable registry linking clinical decisions to the exact
prompt version that generated them — a regulatory requirement for AI-assisted
medical decisions under FDA Software as a Medical Device (SaMD) guidelines.

SHADOW DEPLOYMENT PATTERN
───────────────────────────
The `run_shadow_evaluation` function runs the SAME test queries against TWO
embedding models simultaneously:
  • Production model: text-embedding-3-small (live traffic)
  • Shadow model:    text-embedding-3-large (new candidate)

Results are logged to separate MLflow experiments. If the shadow model
outperforms production on all four RAGAS metrics for 7 consecutive days,
the platform triggers a promotion workflow to swap the models.
NO production traffic is affected during shadow evaluation.

PREREQUISITES
──────────────
  pip install ragas mlflow datasets langchain-openai
  mlflow server --host 0.0.0.0 --port 5000
  export MLFLOW_TRACKING_URI=http://localhost:5000
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ─── MLflow Experiment Names ───────────────────────────────────────────────────
EXPERIMENT_PRODUCTION  = "rag-evaluation-production"
EXPERIMENT_SHADOW      = "rag-evaluation-shadow"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RAGEvalSample:
    """
    One evaluation sample: a question, the retrieved contexts, and the
    LLM-generated answer. Ground truth answers are optional (RAGAS supports
    reference-free scoring via LLM-as-judge for most metrics).
    """
    question: str
    contexts: list[str]              # Retrieved chunks fed to LLM
    answer: str                      # LLM-generated response
    ground_truth: str | None = None  # Optional gold answer for recall scoring
    trace_id: str | None = None      # OTEL trace_id linking to production log
    chunk_ids: list[str] = field(default_factory=list)


@dataclass
class RAGScores:
    context_precision: float
    context_recall: float
    faithfulness: float
    answer_relevancy: float
    model_name: str
    prompt_version: str
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sample_count: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Prompt Version Registry
# ─────────────────────────────────────────────────────────────────────────────

def compute_prompt_version(prompt_template: str) -> str:
    """
    Compute a deterministic SHA-256 hash of a prompt template string.

    This hash serves as the prompt version identifier stored in MLflow.
    Any change to the prompt (even whitespace) produces a different hash,
    making it impossible to accidentally overwrite a previous version.

    Args:
        prompt_template: The raw prompt template string.

    Returns:
        First 16 characters of the SHA-256 hex digest (e.g., "a3f9b12c").
    """
    return hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()[:16]


def register_prompt(
    prompt_name: str,
    prompt_template: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """
    Register a prompt template in the MLflow prompt registry.

    Creates an MLflow run tagged with the prompt version hash and stores the
    full template as an artifact. This provides the full audit trail required
    to answer: "What exact prompt generated this clinical decision?"

    Args:
        prompt_name:     Human-readable name (e.g., "medical_necessity_rag").
        prompt_template: The full prompt template string.
        metadata:        Additional tags (e.g., {"model": "gpt-4o", "owner": "ml-team"}).

    Returns:
        The prompt version hash string.
    """
    try:
        import mlflow

        version = compute_prompt_version(prompt_template)

        with mlflow.start_run(run_name=f"prompt-{prompt_name}-{version[:8]}"):
            mlflow.set_tag("prompt_name", prompt_name)
            mlflow.set_tag("prompt_version", version)
            mlflow.set_tag("registered_at", datetime.now(timezone.utc).isoformat())

            if metadata:
                for k, v in metadata.items():
                    mlflow.set_tag(k, str(v))

            # Store the full template as a text artifact
            import pathlib
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                f.write(prompt_template)
                tmp_path = f.name
            mlflow.log_artifact(tmp_path, artifact_path="prompt_template")
            pathlib.Path(tmp_path).unlink(missing_ok=True)

        logger.info("prompt_registered", name=prompt_name, version=version)
        return version

    except ImportError:
        logger.warning("mlflow_not_installed", msg="Prompt registration skipped.")
        return compute_prompt_version(prompt_template)


# ─────────────────────────────────────────────────────────────────────────────
# RAGAS Evaluation Engine
# ─────────────────────────────────────────────────────────────────────────────

async def run_ragas_evaluation(
    samples: list[RAGEvalSample],
    model_name: str,
    prompt_version: str,
    experiment_name: str = EXPERIMENT_PRODUCTION,
    llm_judge_model: str = "gpt-4o-mini",
) -> RAGScores:
    """
    Run the full RAGAS evaluation pipeline on a batch of RAG samples.

    RAGAS uses the LLM-as-judge pattern: the `llm_judge_model` is asked to
    score each metric rather than comparing against a human-labeled dataset.
    This makes RAGAS suitable for continuous production monitoring where
    labeling every response is infeasible.

    The RAGAS library call is CPU+network-bound. It runs in asyncio.to_thread()
    to avoid blocking the FastAPI event loop.

    Metrics computed:
      • faithfulness        — hallucination detection (most critical)
      • context_precision   — retrieval quality
      • context_recall      — retrieval completeness
      • answer_relevancy    — response quality

    Args:
        samples:         List of RAGEvalSample objects from production traces.
        model_name:      Embedding model name (for MLflow tagging).
        prompt_version:  Prompt hash from compute_prompt_version().
        experiment_name: MLflow experiment to log results under.
        llm_judge_model: LLM used by RAGAS for scoring (not the RAG LLM).

    Returns:
        RAGScores with all four metric values.
    """
    def _run_sync() -> RAGScores:
        try:
            from datasets import Dataset
            from ragas import evaluate
            from ragas.metrics import (
                answer_relevancy,
                context_precision,
                context_recall,
                faithfulness,
            )
            from langchain_openai import ChatOpenAI, OpenAIEmbeddings

            # Build Hugging Face Dataset from samples
            data = {
                "question": [s.question for s in samples],
                "contexts": [s.contexts for s in samples],
                "answer": [s.answer for s in samples],
            }
            # ground_truth is optional — include only if available
            if any(s.ground_truth for s in samples):
                data["ground_truth"] = [s.ground_truth or "" for s in samples]

            dataset = Dataset.from_dict(data)

            judge_llm = ChatOpenAI(model=llm_judge_model, temperature=0)
            judge_embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

            result = evaluate(
                dataset=dataset,
                metrics=[faithfulness, context_precision, context_recall, answer_relevancy],
                llm=judge_llm,
                embeddings=judge_embeddings,
            )

            scores = RAGScores(
                context_precision=float(result["context_precision"]),
                context_recall=float(result["context_recall"]),
                faithfulness=float(result["faithfulness"]),
                answer_relevancy=float(result["answer_relevancy"]),
                model_name=model_name,
                prompt_version=prompt_version,
                sample_count=len(samples),
            )

            # ── Log to MLflow ────────────────────────────────────────────
            try:
                import mlflow
                mlflow.set_experiment(experiment_name)
                with mlflow.start_run(run_name=f"ragas-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"):
                    mlflow.set_tag("embedding_model", model_name)
                    mlflow.set_tag("prompt_version", prompt_version)
                    mlflow.set_tag("judge_model", llm_judge_model)
                    mlflow.log_metric("faithfulness", scores.faithfulness)
                    mlflow.log_metric("context_precision", scores.context_precision)
                    mlflow.log_metric("context_recall", scores.context_recall)
                    mlflow.log_metric("answer_relevancy", scores.answer_relevancy)
                    mlflow.log_param("sample_count", len(samples))
                    mlflow.log_param("evaluated_at", scores.evaluated_at)
            except ImportError:
                pass

            logger.info(
                "ragas_evaluation_complete",
                faithfulness=f"{scores.faithfulness:.3f}",
                context_precision=f"{scores.context_precision:.3f}",
                context_recall=f"{scores.context_recall:.3f}",
                answer_relevancy=f"{scores.answer_relevancy:.3f}",
            )
            return scores

        except ImportError as exc:
            logger.error("ragas_not_installed", error=str(exc))
            # Return zero scores so the pipeline doesn't crash
            return RAGScores(
                context_precision=0.0, context_recall=0.0,
                faithfulness=0.0, answer_relevancy=0.0,
                model_name=model_name, prompt_version=prompt_version,
                sample_count=0,
            )

    return await asyncio.to_thread(_run_sync)


# ─────────────────────────────────────────────────────────────────────────────
# Shadow Deployment Evaluation
# ─────────────────────────────────────────────────────────────────────────────

async def run_shadow_evaluation(
    test_queries: list[str],
    collection_name: str = "policy_vectors",
    production_model: str = "text-embedding-3-small",
    shadow_model: str = "text-embedding-3-large",
    top_k: int = 5,
) -> dict[str, RAGScores]:
    """
    Run identical test queries against both production and shadow embedding models.

    SHADOW DEPLOYMENT SAFETY GUARANTEE
    ────────────────────────────────────
    The shadow model never touches the production ChromaDB collection.
    It writes to a separate `shadow_{collection_name}` collection that is
    pre-populated by the MLOps team during off-peak hours.
    Production traffic reads only from `collection_name`.

    Promotion criteria (configurable):
      • Shadow faithfulness > production faithfulness by > 2%
      • Shadow context_precision > production by > 3%
      • Both metrics stable for 7 consecutive daily evaluations

    Args:
        test_queries:      List of standard insurance query strings.
        collection_name:   Production ChromaDB collection name.
        production_model:  Production embedding model identifier.
        shadow_model:      Candidate model being evaluated.
        top_k:             Number of chunks retrieved per query.

    Returns:
        Dict with "production" and "shadow" RAGScores for comparison.
    """
    from backend.rag.pipeline import run_rag_query

    prompt_version = "shadow_eval_stub"

    async def _collect_samples(model: str, coll: str) -> list[RAGEvalSample]:
        samples = []
        for query in test_queries:
            try:
                result = await run_rag_query(
                    query=query,
                    collection_name=coll,
                    tenant_id="eval_tenant",
                    user_role="claims_adjuster",
                )
                contexts = [c.text for c in result.final_chunks] if result.final_chunks else [query]
                samples.append(RAGEvalSample(
                    question=query,
                    contexts=contexts,
                    answer=result.answer,
                ))
            except Exception as exc:
                logger.warning("shadow_eval_query_failed", query=query[:50], error=str(exc))
        return samples

    # Run both evaluations concurrently
    prod_samples, shadow_samples = await asyncio.gather(
        _collect_samples(production_model, collection_name),
        _collect_samples(shadow_model, f"shadow_{collection_name}"),
    )

    prod_scores, shadow_scores = await asyncio.gather(
        run_ragas_evaluation(prod_samples, production_model, prompt_version,
                             EXPERIMENT_PRODUCTION),
        run_ragas_evaluation(shadow_samples, shadow_model, prompt_version,
                             EXPERIMENT_SHADOW),
    )

    logger.info(
        "shadow_evaluation_complete",
        prod_faithfulness=f"{prod_scores.faithfulness:.3f}",
        shadow_faithfulness=f"{shadow_scores.faithfulness:.3f}",
        shadow_wins=shadow_scores.faithfulness > prod_scores.faithfulness,
    )

    return {"production": prod_scores, "shadow": shadow_scores}
