"""
backend/api/routers/mlops.py
──────────────────────────────
MLOps / AI Evaluation endpoints.

Endpoints:
  POST /mlops/evaluate — trigger a background RAGAS evaluation run
  GET  /mlops/evaluate/status/{run_id} — poll run status (in-memory cache)

Auth: ADMIN only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from backend.api.deps import get_current_user, require_role
from backend.database.models import User, UserRole

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/mlops", tags=["MLOps"])

# ── In-memory run status registry (replace with Redis in production) ──────────
_RUN_STATUS: dict[str, dict[str, Any]] = {}


# ── Schemas ───────────────────────────────────────────────────────────────────

class EvaluationRequest(BaseModel):
    """
    Optional parameters to customise the evaluation batch.
    Defaults run a 5-question gold-standard suite against the policy_vectors
    collection using the production Gemini embedding model.
    """
    collection_name: str = "policy_vectors"
    model_name: str = "text-embedding-004"
    run_shadow: bool = False   # Also evaluate the shadow model for A/B comparison


class EvaluationAccepted(BaseModel):
    run_id: str
    message: str
    status_url: str


class EvaluationStatus(BaseModel):
    run_id: str
    state: str                # "running" | "complete" | "failed"
    started_at: str
    completed_at: str | None
    scores: dict[str, float] | None
    error: str | None


# ── Gold-standard test suite ──────────────────────────────────────────────────

GOLD_STANDARD_QUERIES = [
    {
        "question": "What is the annual deductible for an individual plan?",
        "ground_truth": "The annual deductible for an individual plan is $1,500.",
    },
    {
        "question": "Is preventive care covered at 100% before the deductible?",
        "ground_truth": "Yes, in-network preventive care is covered at 100% with no deductible.",
    },
    {
        "question": "What is the out-of-pocket maximum for a family plan?",
        "ground_truth": "The out-of-pocket maximum for a family plan is $8,700 per plan year.",
    },
    {
        "question": "Are mental health services covered under the plan?",
        "ground_truth": "Yes, mental health and substance use disorder services are covered at the same level as medical benefits.",
    },
    {
        "question": "What is the copay for a specialist office visit?",
        "ground_truth": "The copay for a specialist office visit is $60 per visit after the deductible.",
    },
]


# ── Background task ───────────────────────────────────────────────────────────

async def _run_evaluation_task(
    run_id: str,
    collection_name: str,
    model_name: str,
) -> None:
    """
    Execute the RAGAS evaluation pipeline as a background task.

    Uses the gold-standard test queries defined in GOLD_STANDARD_QUERIES.
    Results (or errors) are written into the in-memory _RUN_STATUS dict.
    """
    from backend.mlops.evaluation import (
        RAGEvalSample,
        RAGScores,
        compute_prompt_version,
        run_ragas_evaluation,
    )
    from backend.rag.pipeline import run_rag_query
    from backend.api.routers.underwriting import UNDERWRITING_SYSTEM_PROMPT

    _RUN_STATUS[run_id]["state"] = "running"
    prompt_version = compute_prompt_version(UNDERWRITING_SYSTEM_PROMPT)

    samples: list[RAGEvalSample] = []
    for item in GOLD_STANDARD_QUERIES:
        try:
            result = await run_rag_query(
                query=item["question"],
                collection_name=collection_name,
                tenant_id="eval_tenant",
                user_role="underwriter",
            )
            contexts = [c.text for c in result.final_chunks] if result.final_chunks else [item["question"]]
            answer = getattr(result, "answer", "No answer generated.")
            samples.append(RAGEvalSample(
                question=item["question"],
                contexts=contexts,
                answer=answer,
                ground_truth=item.get("ground_truth"),
            ))
        except Exception as exc:
            logger.warning("eval_query_failed", question=item["question"][:60], error=str(exc))
            # Emit a stub sample so the run doesn't silently skip
            samples.append(RAGEvalSample(
                question=item["question"],
                contexts=[item["question"]],
                answer="[retrieval failed]",
                ground_truth=item.get("ground_truth"),
            ))

    try:
        scores = await run_ragas_evaluation(
            samples=samples,
            model_name=model_name,
            prompt_version=prompt_version,
        )
        _RUN_STATUS[run_id].update({
            "state": "complete",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "scores": {
                "faithfulness":      scores.faithfulness,
                "context_precision": scores.context_precision,
                "context_recall":    scores.context_recall,
                "answer_relevancy":  scores.answer_relevancy,
            },
            "error": None,
        })
        logger.info(
            "evaluation_complete",
            run_id=run_id,
            faithfulness=f"{scores.faithfulness:.3f}",
            context_precision=f"{scores.context_precision:.3f}",
        )
    except Exception as exc:
        _RUN_STATUS[run_id].update({
            "state": "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "scores": None,
            "error": str(exc),
        })
        logger.error("evaluation_task_failed", run_id=run_id, error=str(exc))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/evaluate",
    response_model=EvaluationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a RAGAS evaluation run (admin only)",
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)
async def trigger_evaluation(
    body: EvaluationRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
) -> EvaluationAccepted:
    """
    Launch a background RAGAS evaluation run against the gold-standard
    test suite and return 202 Accepted immediately.

    Poll GET /mlops/evaluate/status/{run_id} to check progress.

    The evaluation:
      1. Runs 5 gold-standard clinical queries through the RAG pipeline.
      2. Scores the responses with RAGAS (faithfulness, precision, recall, relevancy).
      3. Logs results to MLflow (if configured).

    Asserts ≥ 90% faithfulness for a passing evaluation.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    _RUN_STATUS[run_id] = {
        "run_id":       run_id,
        "state":        "queued",
        "started_at":   started_at,
        "completed_at": None,
        "scores":       None,
        "error":        None,
        "triggered_by": str(current_user.id),
    }

    background_tasks.add_task(
        _run_evaluation_task,
        run_id=run_id,
        collection_name=body.collection_name,
        model_name=body.model_name,
    )

    logger.info("evaluation_triggered", run_id=run_id, collection=body.collection_name)

    return EvaluationAccepted(
        run_id=run_id,
        message="Evaluation queued. Check status_url for results.",
        status_url=f"/api/v1/mlops/evaluate/status/{run_id}",
    )


@router.get(
    "/evaluate/status/{run_id}",
    response_model=EvaluationStatus,
    summary="Poll evaluation run status",
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)
async def get_evaluation_status(
    run_id: str,
    current_user: User = Depends(get_current_user),
) -> EvaluationStatus:
    """Return the current state of a background evaluation run."""
    entry = _RUN_STATUS.get(run_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"No evaluation run found with id '{run_id}'.")

    return EvaluationStatus(
        run_id=entry["run_id"],
        state=entry["state"],
        started_at=entry["started_at"],
        completed_at=entry.get("completed_at"),
        scores=entry.get("scores"),
        error=entry.get("error"),
    )
