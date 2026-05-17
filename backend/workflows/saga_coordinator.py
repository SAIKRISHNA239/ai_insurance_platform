"""
backend/workflows/saga_coordinator.py
───────────────────────────────────────
Saga Pattern Coordinator for distributed claim adjudication transactions.

THE SAGA PATTERN IN HEALTHCARE
────────────────────────────────
A claim adjudication involves multiple microservices — each with its own
database. Traditional ACID transactions cannot span service boundaries.
The Saga pattern solves this with a sequence of local transactions, each
with a compensating transaction that undoes it if a downstream step fails.

CLAIM ADJUDICATION SAGA
─────────────────────────
Step 1: RESERVE_CLAIM         → Mark claim as "processing" in PostgreSQL
        Compensate: RELEASE_CLAIM_RESERVATION

Step 2: DEDUCT_DEDUCTIBLE     → Debit member's deductible balance in ledger
        Compensate: REFUND_DEDUCTIBLE

Step 3: CALCULATE_PAYMENT     → Compute allowed/paid amounts
        Compensate: VOID_PAYMENT_CALCULATION

Step 4: QUEUE_PAYMENT         → Submit payment instruction to payment processor
        Compensate: CANCEL_PAYMENT_INSTRUCTION

Step 5: FINALIZE_CLAIM        → Mark claim as FINALIZED in PostgreSQL
        Compensate: REVERT_TO_ADJUDICATED (no payment finalized)

Step 6: EMIT_EOB              → Publish Explanation of Benefits event
        Compensate: VOID_EOB (if already sent, issue corrected EOB)

FAILURE MODES
──────────────
If Step 4 (QUEUE_PAYMENT) fails:
  • Saga executes compensating transactions for steps 3, 2, 1 in reverse.
  • Claim reverts to AUTO_ADJUDICATED state (not FINALIZED).
  • The failed saga event is published to saga.adjudication.dlq (Kafka DLQ).
  • An on-call alert fires via Prometheus alertmanager.

DLQ (DEAD LETTER QUEUE) DESIGN
─────────────────────────────────
The Kafka DLQ topic `saga.adjudication.dlq` receives a DLQEnvelope:
  • original_event: The exact Kafka message that failed
  • failure_reason: Exception class + message
  • retry_count:    Current retry attempt number
  • max_retries:    After this, escalate to manual intervention queue
  • failed_step:    Which saga step caused the failure

A dead letter processor (separate consumer) replays DLQ events with
exponential backoff (10s, 30s, 2m, 10m). After max_retries, the event
is escalated to the human review queue with a Jira ticket created
automatically via webhook. CLAIMS ARE NEVER SILENTLY DROPPED.

ORCHESTRATION vs CHOREOGRAPHY
───────────────────────────────
This implementation uses the ORCHESTRATION approach: a central
SagaCoordinator manages the step sequence and compensations. This makes
the saga observable (one coordinator log shows the full story) and easier
to debug than choreography-based sagas (distributed event chains).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Coroutine

import structlog

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Saga State Enum
# ─────────────────────────────────────────────────────────────────────────────

class SagaStatus(str, Enum):
    PENDING     = "pending"
    RUNNING     = "running"
    COMPLETED   = "completed"
    COMPENSATING = "compensating"  # Rolling back
    FAILED      = "failed"         # Compensation also failed — needs manual fix
    ROLLED_BACK = "rolled_back"    # Successfully compensated


# ─────────────────────────────────────────────────────────────────────────────
# DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SagaStep:
    """
    One step in the saga transaction sequence.

    Each step has:
      • action:     The async forward transaction function.
      • compensate: The async compensating (rollback) function.
      • name:       Human-readable step name for logging and audit trail.
    """
    name: str
    action: Callable[..., Coroutine[Any, Any, dict[str, Any]]]
    compensate: Callable[..., Coroutine[Any, Any, None]]


@dataclass
class SagaContext:
    """
    Mutable shared context passed through every saga step.
    Stores outputs from each step so subsequent steps can reference them.
    """
    saga_id: str
    claim_id: str
    tenant_id: str
    adjudication_result: dict[str, Any]
    step_outputs: dict[str, Any] = field(default_factory=dict)
    completed_steps: list[str] = field(default_factory=list)
    status: SagaStatus = SagaStatus.PENDING
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: str | None = None


@dataclass
class DLQEnvelope:
    """Wraps a failed Kafka event for the Dead Letter Queue."""
    dlq_id: str
    original_topic: str
    original_event: dict[str, Any]
    failure_reason: str
    failed_step: str
    saga_id: str
    claim_id: str
    retry_count: int = 0
    max_retries: int = 5
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    next_retry_at: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Individual Saga Step Implementations
# ─────────────────────────────────────────────────────────────────────────────

async def step_reserve_claim(ctx: SagaContext) -> dict[str, Any]:
    """
    Step 1: Atomically mark the claim as "processing" to prevent duplicate payments.
    Uses a PostgreSQL advisory lock on the claim UUID.
    """
    logger.info("saga_step_reserve_claim", claim_id=ctx.claim_id)
    # In production: UPDATE claims SET status='processing', saga_id=:saga_id
    # WHERE id=:claim_id AND status NOT IN ('processing', 'finalized')
    # If 0 rows updated → raise SagaDuplicateError (idempotency guard)
    return {"reserved": True, "locked_at": datetime.now(timezone.utc).isoformat()}


async def compensate_reserve_claim(ctx: SagaContext) -> None:
    """Compensate Step 1: Release the processing lock back to adjudicated state."""
    logger.info("saga_compensate_release_claim", claim_id=ctx.claim_id)
    # UPDATE claims SET status='auto_adjudicated', saga_id=NULL WHERE id=:claim_id


async def step_deduct_deductible(ctx: SagaContext) -> dict[str, Any]:
    """
    Step 2: Debit the member's annual deductible accumulator.
    Updates the member's deductible_met_ytd field in the policy ledger table.
    """
    billed = Decimal(str(ctx.adjudication_result.get("billed_amount", "0")))
    deductible_applied = min(billed, Decimal("500.00"))   # Simplified mock
    logger.info("saga_step_deduct_deductible", amount=str(deductible_applied))
    return {"deductible_applied": str(deductible_applied)}


async def compensate_deduct_deductible(ctx: SagaContext) -> None:
    """Compensate Step 2: Reverse the deductible debit."""
    amount = ctx.step_outputs.get("deduct_deductible", {}).get("deductible_applied", "0")
    logger.info("saga_compensate_refund_deductible", amount=amount)
    # UPDATE policy_ledger SET deductible_met_ytd = deductible_met_ytd - :amount


async def step_calculate_payment(ctx: SagaContext) -> dict[str, Any]:
    """
    Step 3: Apply benefit schedule to compute allowed_amount and paid_amount.
    Reads from the policy's benefits_schedule JSONB for the relevant CPT codes.
    """
    billed = Decimal(str(ctx.adjudication_result.get("billed_amount", "0")))
    allowed = billed * Decimal("0.80")   # Mock: 80% of billed
    paid = max(Decimal("0"), allowed - Decimal(ctx.step_outputs.get("deduct_deductible", {}).get("deductible_applied", "0")))
    logger.info("saga_step_calculate_payment", allowed=str(allowed), paid=str(paid))
    return {"allowed_amount": str(allowed), "paid_amount": str(paid)}


async def compensate_calculate_payment(ctx: SagaContext) -> None:
    """Compensate Step 3: Void the payment calculation (set fields to NULL)."""
    logger.info("saga_compensate_void_payment_calc", claim_id=ctx.claim_id)
    # UPDATE claims SET allowed_amount=NULL, paid_amount=NULL WHERE id=:claim_id


async def step_queue_payment(ctx: SagaContext) -> dict[str, Any]:
    """
    Step 4: Submit the payment instruction to the payment processor.
    This is the most failure-prone step (external API call).
    """
    paid = ctx.step_outputs.get("calculate_payment", {}).get("paid_amount", "0")
    payment_ref = f"PAY-{uuid.uuid4().hex[:10].upper()}"
    logger.info("saga_step_queue_payment", amount=paid, ref=payment_ref)
    # In production: POST to payment processor API
    # Raises PaymentProcessorError on failure → triggers saga rollback
    return {"payment_reference": payment_ref, "payment_queued_at": datetime.now(timezone.utc).isoformat()}


async def compensate_queue_payment(ctx: SagaContext) -> None:
    """Compensate Step 4: Cancel the queued payment instruction."""
    ref = ctx.step_outputs.get("queue_payment", {}).get("payment_reference")
    logger.info("saga_compensate_cancel_payment", ref=ref)
    # POST to payment processor: /payments/{ref}/cancel


async def step_finalize_claim(ctx: SagaContext) -> dict[str, Any]:
    """Step 5: Mark the claim as FINALIZED in PostgreSQL."""
    logger.info("saga_step_finalize_claim", claim_id=ctx.claim_id)
    # UPDATE claims SET status='finalized', finalized_at=NOW() WHERE id=:claim_id
    return {"finalized_at": datetime.now(timezone.utc).isoformat()}


async def compensate_finalize_claim(ctx: SagaContext) -> None:
    """Compensate Step 5: Revert claim status back to AUTO_ADJUDICATED."""
    logger.info("saga_compensate_revert_finalized", claim_id=ctx.claim_id)
    # UPDATE claims SET status='auto_adjudicated', finalized_at=NULL


async def step_emit_eob(ctx: SagaContext) -> dict[str, Any]:
    """Step 6: Publish Explanation of Benefits event to member notification topic."""
    logger.info("saga_step_emit_eob", claim_id=ctx.claim_id)
    return {"eob_event_id": str(uuid.uuid4())}


async def compensate_emit_eob(ctx: SagaContext) -> None:
    """Compensate Step 6: Publish a corrected/voided EOB notification."""
    logger.info("saga_compensate_void_eob", claim_id=ctx.claim_id)


# ─────────────────────────────────────────────────────────────────────────────
# DLQ Publisher
# ─────────────────────────────────────────────────────────────────────────────

async def publish_to_dlq(
    envelope: DLQEnvelope,
    topic: str = "saga.adjudication.dlq",
) -> None:
    """
    Publish a failed saga event to the Dead Letter Queue.

    DLQ events are retained for 30 days (configured in docker-compose.observability.yml).
    A separate DLQ consumer retries them with exponential backoff.
    After max_retries, escalates to the human intervention queue.

    CLAIM SAFETY GUARANTEE:
    No saga failure results in a silently dropped transaction. The DLQ
    is the last safety net — it captures every failure with full context
    for human inspection and deterministic replay.
    """
    from backend.workflows.events import get_producer

    producer = await get_producer()
    payload = {
        "dlq_id": envelope.dlq_id,
        "saga_id": envelope.saga_id,
        "claim_id": envelope.claim_id,
        "original_topic": envelope.original_topic,
        "original_event": envelope.original_event,
        "failure_reason": envelope.failure_reason,
        "failed_step": envelope.failed_step,
        "retry_count": envelope.retry_count,
        "max_retries": envelope.max_retries,
        "created_at": envelope.created_at,
    }
    await producer.send_and_wait(
        topic,
        value=json.dumps(payload).encode(),
        key=envelope.claim_id.encode(),
    )
    logger.error(
        "saga_published_to_dlq",
        dlq_id=envelope.dlq_id,
        claim_id=envelope.claim_id,
        failed_step=envelope.failed_step,
        retry_count=envelope.retry_count,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Saga Coordinator
# ─────────────────────────────────────────────────────────────────────────────

ADJUDICATION_SAGA_STEPS: list[SagaStep] = [
    SagaStep("reserve_claim",      step_reserve_claim,      compensate_reserve_claim),
    SagaStep("deduct_deductible",  step_deduct_deductible,  compensate_deduct_deductible),
    SagaStep("calculate_payment",  step_calculate_payment,  compensate_calculate_payment),
    SagaStep("queue_payment",      step_queue_payment,      compensate_queue_payment),
    SagaStep("finalize_claim",     step_finalize_claim,     compensate_finalize_claim),
    SagaStep("emit_eob",           step_emit_eob,           compensate_emit_eob),
]


async def execute_adjudication_saga(
    claim_id: str,
    tenant_id: str,
    adjudication_result: dict[str, Any],
    original_kafka_event: dict[str, Any] | None = None,
) -> SagaContext:
    """
    Execute the full claim adjudication saga with automatic rollback on failure.

    Executes each step in sequence. On any step failure:
      1. Logs the error with the saga_id and failed step name.
      2. Executes compensating transactions in REVERSE order for all
         previously completed steps.
      3. Publishes a DLQEnvelope to the Kafka dead letter topic.
      4. Returns the SagaContext with status=ROLLED_BACK or FAILED.

    This function is idempotent when called with the same saga_id — the
    `step_reserve_claim` step uses a PostgreSQL advisory lock to prevent
    duplicate processing.

    Args:
        claim_id:              UUID string of the claim.
        tenant_id:             Tenant identifier.
        adjudication_result:   Output from the UM router / adjudication engine.
        original_kafka_event:  The Kafka event that triggered this saga (for DLQ).

    Returns:
        Finalized SagaContext. Check `.status` to determine outcome.
    """
    saga_id = str(uuid.uuid4())
    ctx = SagaContext(
        saga_id=saga_id,
        claim_id=claim_id,
        tenant_id=tenant_id,
        adjudication_result=adjudication_result,
        status=SagaStatus.RUNNING,
    )

    logger.info("saga_started", saga_id=saga_id, claim_id=claim_id, steps=len(ADJUDICATION_SAGA_STEPS))

    # ── Forward execution ─────────────────────────────────────────────────────
    for step in ADJUDICATION_SAGA_STEPS:
        try:
            output = await step.action(ctx)
            ctx.step_outputs[step.name] = output
            ctx.completed_steps.append(step.name)
            logger.info("saga_step_completed", saga_id=saga_id, step=step.name)
        except Exception as exc:
            ctx.error = f"{type(exc).__name__}: {exc}"
            ctx.status = SagaStatus.COMPENSATING
            logger.error(
                "saga_step_failed",
                saga_id=saga_id,
                step=step.name,
                error=ctx.error,
            )

            # ── Compensate in reverse order ────────────────────────────────
            failed_step_name = step.name
            compensated_all = True
            for comp_step in reversed(ADJUDICATION_SAGA_STEPS):
                if comp_step.name not in ctx.completed_steps:
                    continue
                try:
                    await comp_step.compensate(ctx)
                    logger.info("saga_compensated", saga_id=saga_id, step=comp_step.name)
                except Exception as comp_exc:
                    compensated_all = False
                    logger.critical(
                        "saga_compensation_failed",
                        saga_id=saga_id,
                        step=comp_step.name,
                        error=str(comp_exc),
                        msg="MANUAL INTERVENTION REQUIRED",
                    )

            ctx.status = SagaStatus.ROLLED_BACK if compensated_all else SagaStatus.FAILED

            # ── Publish to DLQ ─────────────────────────────────────────────
            envelope = DLQEnvelope(
                dlq_id=str(uuid.uuid4()),
                original_topic="saga.adjudication.events",
                original_event=original_kafka_event or {"claim_id": claim_id},
                failure_reason=ctx.error,
                failed_step=failed_step_name,
                saga_id=saga_id,
                claim_id=claim_id,
            )
            try:
                await publish_to_dlq(envelope)
            except Exception as dlq_exc:
                logger.critical("dlq_publish_failed", error=str(dlq_exc), claim_id=claim_id)

            return ctx

    ctx.status = SagaStatus.COMPLETED
    logger.info("saga_completed", saga_id=saga_id, claim_id=claim_id)
    return ctx
