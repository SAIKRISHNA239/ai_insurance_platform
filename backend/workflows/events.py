"""
backend/workflows/events.py
────────────────────────────
Kafka Event Publisher and Utilization Management (UM) Router.

EVENT-DRIVEN ARCHITECTURE — WHY KAFKA?
────────────────────────────────────────
After SNIP validation passes, the API endpoint must return immediately.
It cannot wait for:
  • UM routing decision (~10-50ms for rule evaluation)
  • RAG medical necessity check (~2-5 seconds for LLM)
  • Fraud scoring (~50-200ms for ML model)
  • Payment calculation

These downstream steps are decoupled via Kafka. The API publishes one event
to `claims.validated` and returns 202 Accepted. Downstream consumers process
asynchronously. This gives us:
  1. Sub-100ms API response time regardless of claim complexity.
  2. Independent scaling of each processing stage.
  3. Durable message storage — if the UM service crashes, Kafka retains the
     event for replay when it recovers. CLAIMS ARE NEVER DROPPED.

FAILURE SAFETY — NO CLAIM DROPPED
────────────────────────────────────
The Kafka producer uses `acks="all"` (all in-sync replicas must acknowledge)
for the `claims.validated` topic. This ensures no event is silently lost even
if the leader broker fails immediately after the produce call.

If the Kafka broker is unavailable, `publish_claim_validated` raises
`KafkaPublishError`. The API layer catches this and:
  1. The claim remains in VALIDATED state in PostgreSQL.
  2. A dead-letter record is written to the `failed_events` table.
  3. A background retry task (Celery beat or cron) re-publishes failed events.

UM ROUTING LOGIC
─────────────────
The Utilization Management (UM) router applies deterministic rule-based
routing. This is intentionally NOT an LLM decision — LLMs are non-deterministic
and cannot be the primary gating mechanism for clinical routing in a regulated
environment. The LLM (via RAG) is consulted AFTER routing as an advisory tool
for the human reviewer.

HIGH-COST / HIGH-COMPLEXITY TRIGGERS:
  • CPT code range 10000-69999 = surgical procedures
  • CPT codes in COMPLEX_CPT_SET = known high-cost procedures
  • Total charge ≥ $10,000 (configurable threshold)
  • Inpatient place of service (21, 51, 61) with high billed amount
  • 3+ diagnosis codes (multi-morbidity indicator)

Any single trigger routes to PENDING_CLINICAL_REVIEW.
All clean claims route to AUTO_ADJUDICATED (STP).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from backend.claims.schemas import EDIClaimPayload
from backend.claims.state_machine import (
    AdjudicationState,
    ClaimEvent,
    ClaimTransitionRecord,
    build_transition_record,
)

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Kafka Topic Constants
# ─────────────────────────────────────────────────────────────────────────────

TOPIC_CLAIMS_VALIDATED       = "claims.validated"
TOPIC_CLAIMS_CLINICAL_REVIEW = "claims.clinical_review"
TOPIC_CLAIMS_STP             = "claims.stp"
TOPIC_CLAIMS_DEAD_LETTER     = "claims.dead_letter"


# ─────────────────────────────────────────────────────────────────────────────
# UM Routing Configuration
# ─────────────────────────────────────────────────────────────────────────────

# CPT codes requiring mandatory clinical review regardless of cost.
# Source: CMS Prior Authorization List + internal medical policy.
COMPLEX_CPT_SET: frozenset[str] = frozenset({
    # Cardiac
    "33533", "33534", "33535", "33536",  # CABG
    "92920", "92924", "92928", "92933",  # PCI / coronary intervention
    # Orthopedic
    "27447", "27130",                    # Total knee / hip replacement
    "22612", "22630", "22633",           # Spinal fusion
    # Oncology
    "77261", "77262", "77263",           # Radiation therapy planning
    "96413", "96415",                    # Chemotherapy infusion
    # Neurology
    "61510", "61512", "61518", "61520",  # Craniotomy
    # Transplant
    "47135", "33945", "50360",           # Liver / heart / kidney transplant
    # High-cost imaging
    "70553", "70552",                    # MRI Brain with contrast
    "78816",                             # PET scan
})

# Inpatient / facility place of service codes
INPATIENT_POS_CODES: frozenset[str] = frozenset({"21", "51", "61", "62"})

# Cost thresholds (USD)
HIGH_COST_THRESHOLD = Decimal("10000.00")
INPATIENT_HIGH_COST_THRESHOLD = Decimal("5000.00")


# ─────────────────────────────────────────────────────────────────────────────
# Event Envelope DTO
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ClaimValidatedEvent:
    """
    Event payload published to `claims.validated` Kafka topic.

    The envelope follows CloudEvents 1.0 spec conventions (id, source, type,
    specversion) for interoperability with downstream consumers that may be
    implemented in other languages or services.
    """
    specversion: str = "1.0"
    id: str = ""
    source: str = "insurance-platform/claims-api"
    type: str = "com.insurance.claims.validated"
    datacontenttype: str = "application/json"
    time: str = ""
    data: dict[str, Any] = None  # type: ignore

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.time:
            self.time = datetime.utcnow().isoformat() + "Z"


def _serialize_decimal(obj: Any) -> Any:
    """JSON serializer that handles Decimal and datetime types."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


# ─────────────────────────────────────────────────────────────────────────────
# Kafka Producer (aiokafka with MockProducer fallback)
# ─────────────────────────────────────────────────────────────────────────────

class KafkaPublishError(Exception):
    """Raised when event publishing fails. Caller must write dead-letter record."""
    pass


class _MockKafkaProducer:
    """
    In-process mock Kafka producer for development and testing.

    Records all published messages in a class-level list so tests can
    assert on published events without a real Kafka broker.
    Published messages are also logged at INFO level for local debugging.
    """
    _published: list[dict[str, Any]] = []

    async def start(self) -> None:
        logger.info("mock_kafka_producer_started")

    async def stop(self) -> None:
        logger.info("mock_kafka_producer_stopped")

    async def send_and_wait(
        self, topic: str, value: bytes, key: bytes | None = None
    ) -> None:
        record = {
            "topic": topic,
            "key": key.decode() if key else None,
            "value": json.loads(value.decode()),
            "published_at": datetime.utcnow().isoformat(),
        }
        self._published.append(record)
        logger.info(
            "mock_kafka_message_published",
            topic=topic,
            key=record["key"],
            event_type=record["value"].get("type"),
        )

    @classmethod
    def get_published(cls) -> list[dict[str, Any]]:
        return list(cls._published)

    @classmethod
    def clear(cls) -> None:
        cls._published.clear()


# Module-level producer singleton (initialized at startup)
_producer: Any = None


async def get_producer() -> Any:
    """
    Return the active Kafka producer, initializing it if necessary.

    Tries aiokafka first (production). Falls back to MockKafkaProducer
    if aiokafka is not installed or KAFKA_BOOTSTRAP_SERVERS is not configured.
    """
    global _producer
    if _producer is not None:
        return _producer

    from backend.config import get_settings
    settings = get_settings()

    kafka_servers = getattr(settings, "kafka_bootstrap_servers", "")

    if not kafka_servers:
        logger.warning(
            "kafka_not_configured",
            msg="Using MockKafkaProducer. Set KAFKA_BOOTSTRAP_SERVERS for production.",
        )
        _producer = _MockKafkaProducer()
        await _producer.start()
        return _producer

    try:
        from aiokafka import AIOKafkaProducer
        _producer = AIOKafkaProducer(
            bootstrap_servers=kafka_servers,
            acks="all",           # All in-sync replicas must acknowledge
            enable_idempotence=True,  # Exactly-once producer semantics
            compression_type="gzip",
            max_batch_size=16384,
            linger_ms=5,          # Small batching window for throughput
        )
        await _producer.start()
        logger.info("aiokafka_producer_started", servers=kafka_servers)
    except ImportError:
        logger.warning("aiokafka_not_installed", msg="Falling back to MockKafkaProducer.")
        _producer = _MockKafkaProducer()
        await _producer.start()

    return _producer


async def shutdown_producer() -> None:
    """Gracefully stop the Kafka producer. Call from FastAPI lifespan shutdown."""
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None
        logger.info("kafka_producer_stopped")


# ─────────────────────────────────────────────────────────────────────────────
# Event Publisher
# ─────────────────────────────────────────────────────────────────────────────

async def publish_claim_validated(
    claim_id: str,
    payload: EDIClaimPayload,
    snip_result_dict: dict[str, Any],
    transition_record: ClaimTransitionRecord,
) -> None:
    """
    Publish a validated claim event to the `claims.validated` Kafka topic.

    Message key = claim_id (ensures all events for one claim go to the
    same partition, preserving ordering for the downstream UM consumer).

    Failure handling:
    ─────────────────
    If the Kafka broker is unreachable or the produce call times out,
    this function raises KafkaPublishError. The API layer catches this and:
      1. Writes a dead-letter record to PostgreSQL (failed_events table).
      2. Returns HTTP 202 — the claim IS persisted; publishing failed.
      3. A background retry cron re-publishes within 60 seconds.

    CLAIMS ARE NEVER DROPPED on Kafka failure because the canonical record
    is always in PostgreSQL, not in Kafka. Kafka is the delivery mechanism,
    not the system of record.

    Args:
        claim_id:           UUID string of the claim.
        payload:            Validated EDI claim payload.
        snip_result_dict:   Serialized SNIPResult dict for downstream context.
        transition_record:  State transition from RECEIVED → VALIDATED.
    """
    producer = await get_producer()

    # Build procedure codes in a JSON-serializable format
    proc_lines = [
        {
            "line_number": l.line_number,
            "procedure_code": l.procedure_code,
            "modifier": l.modifier,
            "units": l.units,
            "charge_amount": str(l.charge_amount),
        }
        for l in payload.procedure_lines
    ]

    event = ClaimValidatedEvent(
        data={
            "claim_id": claim_id,
            "transaction_set": payload.transaction_set,
            "interchange_control_number": payload.interchange_control_number,
            "billing_provider_npi": payload.billing_provider_npi,
            "patient_id": str(payload.patient_id),
            "policy_id": str(payload.policy_id),
            "service_date_start": payload.service_date_start.isoformat(),
            "service_date_end": payload.service_date_end.isoformat()
                if payload.service_date_end else None,
            "diagnosis_codes": payload.diagnosis_codes,
            "procedure_lines": proc_lines,
            "total_charge": str(payload.total_charge),
            "place_of_service": payload.place_of_service,
            "snip_result": snip_result_dict,
            "transition": {
                "from_state": transition_record.from_state.value,
                "to_state": transition_record.to_state.value,
                "event": transition_record.event.value,
                "triggered_by": transition_record.triggered_by,
                "timestamp": transition_record.timestamp.isoformat(),
            },
        }
    )

    try:
        message_bytes = json.dumps(asdict(event), default=_serialize_decimal).encode()
        key_bytes = claim_id.encode()
        await producer.send_and_wait(
            TOPIC_CLAIMS_VALIDATED,
            value=message_bytes,
            key=key_bytes,
        )
        logger.info(
            "claim_validated_event_published",
            claim_id=claim_id,
            topic=TOPIC_CLAIMS_VALIDATED,
            event_id=event.id,
        )
    except Exception as exc:
        logger.error(
            "kafka_publish_failed",
            claim_id=claim_id,
            topic=TOPIC_CLAIMS_VALIDATED,
            error=str(exc),
        )
        raise KafkaPublishError(
            f"Failed to publish claim {claim_id} to {TOPIC_CLAIMS_VALIDATED}: {exc}"
        ) from exc


# ─────────────────────────────────────────────────────────────────────────────
# UM Router
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UMRoutingDecision:
    """Output of the UM routing evaluation."""
    claim_id: str
    route: str                          # "clinical_review" | "stp"
    event: ClaimEvent                   # ClaimEvent to apply to state machine
    triggers: list[str]                 # Which rules fired (for audit log)
    requires_rag: bool                  # Whether RAG pipeline should be invoked
    risk_score: float                   # Composite risk [0.0–1.0]


def evaluate_um_routing(
    claim_id: str,
    payload: EDIClaimPayload,
) -> UMRoutingDecision:
    """
    Deterministic Utilization Management routing decision.

    Evaluates the claim against a set of rule triggers to determine whether
    it requires clinical review or can proceed via Straight-Through Processing.

    REGULATORY NOTE: This routing function is deterministic and auditable.
    It must NOT call an LLM directly — LLM outputs are advisory (used in
    the clinical review step), never the primary routing gate. This design
    satisfies CMS requirements that medical necessity determinations involve
    qualified clinicians, not solely automated systems.

    Args:
        claim_id: UUID string for the claim.
        payload:  Validated EDI claim payload.

    Returns:
        UMRoutingDecision with route, triggering rules, and risk metadata.
    """
    triggers: list[str] = []
    risk_points: float = 0.0

    procedure_codes = {l.procedure_code.strip() for l in payload.procedure_lines}

    # ── Rule 1: Complex surgical CPT codes ────────────────────────────────
    complex_codes_found = procedure_codes & COMPLEX_CPT_SET
    if complex_codes_found:
        triggers.append(f"complex_cpt:{','.join(sorted(complex_codes_found))}")
        risk_points += 0.5

    # ── Rule 2: Surgical CPT range (10000–69999) ──────────────────────────
    surgical_codes = {c for c in procedure_codes if c.isdigit() and 10000 <= int(c) <= 69999}
    if surgical_codes and not complex_codes_found:
        triggers.append(f"surgical_cpt_range:{len(surgical_codes)}_codes")
        risk_points += 0.3

    # ── Rule 3: High total charge ──────────────────────────────────────────
    if payload.total_charge >= HIGH_COST_THRESHOLD:
        triggers.append(f"high_cost:${payload.total_charge}")
        risk_points += 0.3

    # ── Rule 4: Inpatient facility + elevated cost ─────────────────────────
    if (
        payload.place_of_service in INPATIENT_POS_CODES
        and payload.total_charge >= INPATIENT_HIGH_COST_THRESHOLD
    ):
        triggers.append(f"inpatient_high_cost:pos={payload.place_of_service}")
        risk_points += 0.25

    # ── Rule 5: Multi-morbidity (3+ diagnoses) ────────────────────────────
    if len(payload.diagnosis_codes) >= 3:
        triggers.append(f"multi_morbidity:{len(payload.diagnosis_codes)}_dx")
        risk_points += 0.1

    # ── Routing Decision ──────────────────────────────────────────────────
    risk_score = min(risk_points, 1.0)
    requires_clinical = bool(triggers)

    if requires_clinical:
        route = "clinical_review"
        event = ClaimEvent.UM_ROUTE_CLINICAL
    else:
        route = "stp"
        event = ClaimEvent.UM_ROUTE_STP

    logger.info(
        "um_routing_decision",
        claim_id=claim_id,
        route=route,
        triggers=triggers,
        risk_score=f"{risk_score:.2f}",
    )

    return UMRoutingDecision(
        claim_id=claim_id,
        route=route,
        event=event,
        triggers=triggers,
        requires_rag=requires_clinical,
        risk_score=risk_score,
    )


async def process_validated_claim(
    claim_id: str,
    payload: EDIClaimPayload,
    current_state: AdjudicationState = AdjudicationState.VALIDATED,
) -> tuple[ClaimTransitionRecord, UMRoutingDecision]:
    """
    Downstream consumer logic: apply UM routing to a validated claim.

    This function is the consumer of the `claims.validated` Kafka topic event.
    In production it runs in a separate Celery worker or aiokafka consumer
    group process. For the current phase it is called directly by the API
    to demonstrate end-to-end flow.

    Pipeline:
      1. Evaluate UM routing rules against the claim payload.
      2. Apply the routing event to the state machine.
      3. If clinical review: invoke RAG pipeline for medical necessity context.
      4. Publish the outcome to the appropriate downstream topic.
      5. Return the transition record for DB persistence.

    Failure handling:
    ─────────────────
    If the RAG pipeline call fails (LLM timeout, ChromaDB unavailable), the
    claim transitions to PENDING_CLINICAL_REVIEW without AI context. The
    reviewer will see a note that AI assistance is unavailable. The claim
    is NEVER stuck — human review can proceed without RAG output.

    Args:
        claim_id:      UUID string for the claim.
        payload:       Validated EDI claim payload (from Kafka event).
        current_state: Current state of the claim (should be VALIDATED).

    Returns:
        Tuple of (ClaimTransitionRecord, UMRoutingDecision).
    """
    # ── Step 1: UM Routing evaluation ─────────────────────────────────────
    routing = evaluate_um_routing(claim_id, payload)

    # ── Step 2: Apply state transition ────────────────────────────────────
    reason = (
        f"UM routing triggers: {routing.triggers}" if routing.triggers
        else "No clinical triggers — clean STP claim"
    )
    transition = build_transition_record(
        claim_id=claim_id,
        from_state=current_state,
        event=routing.event,
        triggered_by="um_router_service",
        reason=reason,
        metadata={
            "triggers": routing.triggers,
            "risk_score": routing.risk_score,
            "requires_rag": routing.requires_rag,
        },
    )

    # ── Step 3: Invoke RAG for clinical review claims ──────────────────────
    rag_context: str | None = None
    if routing.requires_rag:
        rag_context = await _invoke_rag_for_medical_necessity(claim_id, payload)

    # ── Step 4: Publish routing event ─────────────────────────────────────
    producer = await get_producer()
    routing_topic = (
        TOPIC_CLAIMS_CLINICAL_REVIEW if routing.route == "clinical_review"
        else TOPIC_CLAIMS_STP
    )

    routing_event_data = {
        "claim_id": claim_id,
        "new_state": transition.to_state.value,
        "route": routing.route,
        "triggers": routing.triggers,
        "risk_score": routing.risk_score,
        "rag_context_preview": rag_context[:200] if rag_context else None,
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        await producer.send_and_wait(
            routing_topic,
            value=json.dumps(routing_event_data).encode(),
            key=claim_id.encode(),
        )
        logger.info(
            "routing_event_published",
            claim_id=claim_id,
            topic=routing_topic,
            state=transition.to_state.value,
        )
    except Exception as exc:
        # Non-fatal: log and continue. The state transition is the source of truth.
        logger.warning(
            "routing_event_publish_failed",
            claim_id=claim_id,
            error=str(exc),
        )

    return transition, routing


async def _invoke_rag_for_medical_necessity(
    claim_id: str,
    payload: EDIClaimPayload,
) -> str | None:
    """
    Invoke the RAG pipeline to retrieve medical necessity context for a claim.

    Constructs a clinical query from the claim's diagnosis and procedure codes,
    then retrieves relevant policy sections and clinical guidelines via the
    RAG retriever.

    Failure mode: If the RAG pipeline fails for any reason, this function
    returns None rather than raising. The claim proceeds to PENDING_CLINICAL_REVIEW
    without AI context — a human reviewer can still adjudicate the claim.
    This ensures the EDA pipeline is never blocked by a RAG failure.

    Args:
        claim_id: UUID string for the claim.
        payload:  Validated EDI claim payload.

    Returns:
        RAG-retrieved context string, or None if retrieval fails.
    """
    try:
        dx_codes = ", ".join(payload.diagnosis_codes[:3])
        cpt_codes = ", ".join(
            l.procedure_code for l in payload.procedure_lines[:3]
        )
        query = (
            f"Medical necessity and coverage policy for procedures {cpt_codes} "
            f"with diagnoses {dx_codes}. "
            f"Is this covered under the standard benefit plan?"
        )

        # Import here to avoid circular dependency
        from backend.rag.pipeline import run_rag_pipeline
        result = await run_rag_pipeline(
            query=query,
            collection_name="policy_vectors",
            tenant_id="system",
            user_role="claims_adjuster",
        )
        logger.info(
            "rag_medical_necessity_retrieved",
            claim_id=claim_id,
            context_length=len(result.get("answer", "")),
        )
        return result.get("answer", "")
    except Exception as exc:
        logger.warning(
            "rag_medical_necessity_failed",
            claim_id=claim_id,
            error=str(exc),
            msg="Claim will proceed to clinical review without RAG context.",
        )
        return None
