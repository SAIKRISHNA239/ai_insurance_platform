"""
backend/workflows/claims_workflow.py
──────────────────────────────────────
Async multi-step claims processing workflow (DAG).

Each step is an independent async function. The orchestrator runs them
in the correct dependency order, passing results forward.

Workflow DAG:
  submit_claim()
       │
       ▼
  [Step 1] validate_edi()       — structural/business rule validation
       │
       ▼
  [Step 2] check_eligibility()  — verify policy is active & covers service date
       │
       ▼
  [Step 3] score_fraud()        — ML fraud probability scoring
       │
       ├── fraud_score > threshold → flag_for_review()
       │
       ▼
  [Step 4] run_rag_adjudication() — RAG: "Is this covered by the policy?"
       │
       ▼
  [Step 5] calculate_payment()   — apply deductible, coinsurance, copay rules
       │
       ▼
  [Step 6] update_claim_status() — persist final status + AI notes
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Claim, ClaimStatus, Policy, PolicyStatus

logger = structlog.get_logger(__name__)

FRAUD_FLAG_THRESHOLD = 0.75  # Claims above this score are flagged for manual review


@dataclass
class WorkflowContext:
    """Shared mutable context passed through each workflow step."""
    claim_id: uuid.UUID
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fraud_score: float | None = None
    is_flagged_for_review: bool = False
    rag_coverage_answer: str | None = None
    calculated_payment: Decimal | None = None
    final_status: ClaimStatus = ClaimStatus.IN_REVIEW


async def validate_edi(claim: Claim, ctx: WorkflowContext) -> bool:
    """Step 1: Validate required EDI fields are present and well-formed."""
    errors = []
    if not claim.billing_provider_npi or len(claim.billing_provider_npi) != 10:
        errors.append("billing_provider_npi must be a 10-digit NPI.")
    if not claim.diagnosis_codes:
        errors.append("At least one diagnosis code is required.")
    if not claim.procedure_codes:
        errors.append("At least one procedure line is required.")

    if errors:
        ctx.errors.extend(errors)
        logger.warning("edi_validation_failed", claim_id=str(ctx.claim_id), errors=errors)
        return False

    logger.debug("edi_validation_passed", claim_id=str(ctx.claim_id))
    return True


async def check_eligibility(
    claim: Claim, policy: Policy, ctx: WorkflowContext
) -> bool:
    """Step 2: Verify policy is active and service date falls within coverage period."""
    if policy.status != PolicyStatus.ACTIVE:
        ctx.errors.append(f"Policy is not active. Current status: {policy.status}")
        return False

    service_date: date = claim.service_date_start
    if not (policy.effective_date <= service_date <= policy.expiry_date):
        ctx.errors.append(
            f"Service date {service_date} is outside policy coverage period "
            f"{policy.effective_date} – {policy.expiry_date}."
        )
        return False

    logger.debug("eligibility_check_passed", claim_id=str(ctx.claim_id))
    return True


async def score_fraud(claim: Claim, ctx: WorkflowContext) -> None:
    """Step 3: Compute ML fraud score and flag if above threshold."""
    # TODO: Replace with real fraud scoring pipeline
    ctx.fraud_score = 0.05  # Placeholder

    if ctx.fraud_score and ctx.fraud_score >= FRAUD_FLAG_THRESHOLD:
        ctx.is_flagged_for_review = True
        ctx.warnings.append(
            f"High fraud probability detected: {ctx.fraud_score:.2%}. Flagged for manual review."
        )
        logger.warning(
            "fraud_flag_triggered",
            claim_id=str(ctx.claim_id),
            score=ctx.fraud_score,
        )


async def run_rag_adjudication(claim: Claim, ctx: WorkflowContext) -> None:
    """Step 4: Use RAG to determine policy coverage for this claim."""
    # TODO: Import and call rag.pipeline.run_rag_query()
    ctx.rag_coverage_answer = "RAG adjudication pending — LLM integration not yet active."
    logger.debug("rag_adjudication_placeholder", claim_id=str(ctx.claim_id))


async def calculate_payment(
    claim: Claim, policy: Policy, ctx: WorkflowContext
) -> None:
    """Step 5: Apply deductible, coinsurance, and coverage limit to compute payment."""
    billed = claim.billed_amount

    # Simplified payment calculation — real logic involves EOB rules
    deductible_applied = min(billed, policy.deductible)
    after_deductible = billed - deductible_applied
    # Assume 80/20 coinsurance
    insurance_portion = after_deductible * Decimal("0.80")
    paid = min(insurance_portion, policy.coverage_limit)

    ctx.calculated_payment = paid
    logger.debug(
        "payment_calculated",
        claim_id=str(ctx.claim_id),
        billed=str(billed),
        paid=str(paid),
    )


async def execute_claims_workflow(
    claim_id: uuid.UUID,
    db: AsyncSession,
) -> WorkflowContext:
    """
    Orchestrate the full claims processing DAG.

    Args:
        claim_id: UUID of the Claim to process.
        db: Active async database session.

    Returns:
        Populated WorkflowContext with results from each step.
    """
    ctx = WorkflowContext(claim_id=claim_id)

    # Load claim + policy
    claim_result = await db.execute(select(Claim).where(Claim.id == claim_id))
    claim = claim_result.scalar_one_or_none()
    if claim is None:
        ctx.errors.append("Claim not found.")
        return ctx

    policy_result = await db.execute(select(Policy).where(Policy.id == claim.policy_id))
    policy = policy_result.scalar_one_or_none()
    if policy is None:
        ctx.errors.append("Associated policy not found.")
        return ctx

    # Execute DAG steps
    if not await validate_edi(claim, ctx):
        ctx.final_status = ClaimStatus.DENIED
        return ctx

    if not await check_eligibility(claim, policy, ctx):
        ctx.final_status = ClaimStatus.DENIED
        return ctx

    await score_fraud(claim, ctx)
    if ctx.is_flagged_for_review:
        ctx.final_status = ClaimStatus.IN_REVIEW
        return ctx

    await run_rag_adjudication(claim, ctx)
    await calculate_payment(claim, policy, ctx)

    ctx.final_status = ClaimStatus.APPROVED
    logger.info("claims_workflow_complete", claim_id=str(claim_id), status=ctx.final_status)
    return ctx
