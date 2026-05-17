"""
backend/claims/state_machine.py
────────────────────────────────
Claim lifecycle state machine using Python enums and a pure-function
transition table.

STATE MACHINE DESIGN
─────────────────────
The claim adjudication lifecycle is modelled as a Deterministic Finite
Automaton (DFA). Every valid state and every legal transition is declared
explicitly. Any attempt to apply an illegal transition raises
`IllegalTransitionError` — this is a hard guard against workflow bugs
silently corrupting claim state in the database.

Why a custom state machine instead of a library like `transitions`?
  • Zero extra dependency for a small, well-bounded state space.
  • Fully typed: states and events are Python enums → IDE/mypy-checked.
  • Auditable: the transition table is a plain dict — fully readable in code
    review without knowledge of a third-party DSL.
  • The transition function is a pure function (stateless) → trivially unit-testable.

HIPAA / CMS Regulatory Context
────────────────────────────────
CMS mandates that payers maintain an auditable claim processing trail.
Each transition in this machine must be logged with:
  • Who/what triggered it (user_id or system process name)
  • When it occurred (UTC timestamp)
  • Why it occurred (reason string)

The `ClaimTransitionRecord` DTO captures this for the audit log.
It must be persisted to the database alongside every state change — never
just applied silently.

SNIP Integration
─────────────────
The SNIP (Sequentially Numbered Information Process) validation protocol
defines 7 tiers of claim validation. This state machine integrates SNIP
outcomes at the first state boundary:

  RECEIVED → SNIP_REJECTED   (any SNIP tier fails)
  RECEIVED → VALIDATED        (all SNIP tiers pass)

A SNIP-rejected claim is TERMINAL from this state machine's perspective.
It cannot be transitioned forward without being re-submitted as a new claim
(corrected claim). This mirrors real payer EDI processing where a SNIP-rejected
X12 interchange generates a 999 acknowledgement (rejected) rather than a 277.

Straight-Through Processing (STP)
───────────────────────────────────
Low-complexity claims that pass all SNIP tiers and UM (Utilization Management)
routing checks follow the STP path:

  RECEIVED → VALIDATED → AUTO_ADJUDICATED → FINALIZED

STP claims never enter PENDING_CLINICAL_REVIEW. They are processed entirely
by deterministic rules and ML scoring, with no human reviewer in the loop.
This mirrors industry STP rates of 70-85% for clean commercial claims.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


# ─────────────────────────────────────────────────────────────────────────────
# State Enumeration
# ─────────────────────────────────────────────────────────────────────────────

class AdjudicationState(str, enum.Enum):
    """
    Complete set of states in the claim adjudication lifecycle.

    String-valued so the enum value can be stored directly in PostgreSQL
    and compared to JSONB event payloads without serialization.
    """
    # ── Intake States ───────────────────────────────────────────────
    RECEIVED            = "received"
    """Claim payload accepted by the API. No validation performed yet."""

    SNIP_REJECTED       = "snip_rejected"
    """Claim failed one or more SNIP validation tiers. TERMINAL — re-submit required."""

    # ── Validation Pass ─────────────────────────────────────────────
    VALIDATED           = "validated"
    """All SNIP tiers passed. Claim is structurally correct and published to Kafka."""

    # ── Routing States ──────────────────────────────────────────────
    PENDING_CLINICAL_REVIEW = "pending_clinical_review"
    """
    UM router identified high-complexity CPT codes or high-cost triggers.
    Routed to RAG pipeline for medical necessity determination by a reviewer.
    """

    AUTO_ADJUDICATED    = "auto_adjudicated"
    """
    Clean claim cleared Straight-Through Processing (STP) logic.
    All rules passed and fraud score is below threshold — no human review needed.
    """

    # ── Terminal States ─────────────────────────────────────────────
    FINALIZED           = "finalized"
    """
    Final payment calculation complete and claim closed.
    Reachable from AUTO_ADJUDICATED (STP) or PENDING_CLINICAL_REVIEW (after review).
    """

    DENIED              = "denied"
    """Claim denied after clinical review or coverage determination. TERMINAL."""

    APPEALED            = "appealed"
    """Denied claim has an open member appeal. Re-opens the workflow."""

    # Convenience
    @property
    def is_terminal(self) -> bool:
        """True if no further transitions are possible from this state."""
        return self in _TERMINAL_STATES


# States from which no further automatic transition is allowed
_TERMINAL_STATES: frozenset[AdjudicationState] = frozenset({
    AdjudicationState.SNIP_REJECTED,
    AdjudicationState.DENIED,
    AdjudicationState.FINALIZED,
})


# ─────────────────────────────────────────────────────────────────────────────
# Event (Trigger) Enumeration
# ─────────────────────────────────────────────────────────────────────────────

class ClaimEvent(str, enum.Enum):
    """
    Events that drive state transitions in the adjudication machine.

    Each event corresponds to a real-world action or system decision.
    """
    SNIP_PASS           = "snip_pass"
    """All 7 SNIP validation tiers completed successfully."""

    SNIP_FAIL           = "snip_fail"
    """One or more SNIP tiers rejected the claim payload."""

    UM_ROUTE_CLINICAL   = "um_route_clinical"
    """UM router determined the claim requires clinical review."""

    UM_ROUTE_STP        = "um_route_stp"
    """UM router cleared the claim for Straight-Through Processing."""

    CLINICAL_APPROVE    = "clinical_approve"
    """Clinical reviewer approved the claim after medical necessity review."""

    CLINICAL_DENY       = "clinical_deny"
    """Clinical reviewer denied the claim (no medical necessity)."""

    FINALIZE            = "finalize"
    """Payment calculation complete; claim is closed."""

    APPEAL_FILED        = "appeal_filed"
    """Member filed a formal appeal against a DENIED claim."""

    APPEAL_UPHELD       = "appeal_upheld"
    """Appeal resolved in favor of the payer — claim remains denied."""

    APPEAL_OVERTURNED   = "appeal_overturned"
    """Appeal resolved in favor of the member — claim reopened for payment."""


# ─────────────────────────────────────────────────────────────────────────────
# Transition Table
# ─────────────────────────────────────────────────────────────────────────────

# Maps (current_state, event) → next_state
# Any (state, event) pair NOT present in this table is an illegal transition.
TRANSITION_TABLE: dict[tuple[AdjudicationState, ClaimEvent], AdjudicationState] = {
    # Intake → SNIP
    (AdjudicationState.RECEIVED,                ClaimEvent.SNIP_PASS):          AdjudicationState.VALIDATED,
    (AdjudicationState.RECEIVED,                ClaimEvent.SNIP_FAIL):          AdjudicationState.SNIP_REJECTED,

    # Validated → UM Routing
    (AdjudicationState.VALIDATED,               ClaimEvent.UM_ROUTE_CLINICAL):  AdjudicationState.PENDING_CLINICAL_REVIEW,
    (AdjudicationState.VALIDATED,               ClaimEvent.UM_ROUTE_STP):       AdjudicationState.AUTO_ADJUDICATED,

    # STP path
    (AdjudicationState.AUTO_ADJUDICATED,        ClaimEvent.FINALIZE):           AdjudicationState.FINALIZED,

    # Clinical review path
    (AdjudicationState.PENDING_CLINICAL_REVIEW, ClaimEvent.CLINICAL_APPROVE):   AdjudicationState.AUTO_ADJUDICATED,
    (AdjudicationState.PENDING_CLINICAL_REVIEW, ClaimEvent.CLINICAL_DENY):      AdjudicationState.DENIED,

    # Appeals
    (AdjudicationState.DENIED,                  ClaimEvent.APPEAL_FILED):       AdjudicationState.APPEALED,
    (AdjudicationState.APPEALED,                ClaimEvent.APPEAL_UPHELD):      AdjudicationState.DENIED,
    (AdjudicationState.APPEALED,                ClaimEvent.APPEAL_OVERTURNED):  AdjudicationState.PENDING_CLINICAL_REVIEW,
}


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class IllegalTransitionError(ValueError):
    """
    Raised when a ClaimEvent is applied to an incompatible AdjudicationState.

    This is a programming error or a sign of a race condition — it must
    NEVER be silently swallowed. Let it propagate to the caller so the
    invalid transition attempt can be logged and investigated.

    Example:
        apply_transition(AdjudicationState.SNIP_REJECTED, ClaimEvent.SNIP_PASS)
        → IllegalTransitionError: Cannot apply SNIP_PASS to SNIP_REJECTED
    """
    def __init__(self, current: AdjudicationState, event: ClaimEvent) -> None:
        super().__init__(
            f"Cannot apply event '{event.value}' to claim in state "
            f"'{current.value}'. No transition defined in TRANSITION_TABLE."
        )
        self.current_state = current
        self.event = event


# ─────────────────────────────────────────────────────────────────────────────
# Audit Record DTO
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClaimTransitionRecord:
    """
    Immutable audit record of a single state transition.

    HIPAA Compliance: Every transition must generate a ClaimTransitionRecord
    that is persisted to the `claim_audit_log` table (or appended to the
    `ai_metadata` JSONB field on the Claim row for simpler implementations).

    The record enables:
      • Claims auditing for CMS compliance reporting
      • Dispute resolution for member appeals
      • Forensic investigation of adjudication decisions
    """
    claim_id: str
    from_state: AdjudicationState
    to_state: AdjudicationState
    event: ClaimEvent
    triggered_by: str          # "system", user_id, or service name
    reason: str                # Human-readable rationale
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Pure Transition Function
# ─────────────────────────────────────────────────────────────────────────────

def apply_transition(
    current_state: AdjudicationState,
    event: ClaimEvent,
) -> AdjudicationState:
    """
    Apply a ClaimEvent to the current state and return the next state.

    This is a PURE FUNCTION — it has no side effects. It does not write to
    the database or emit events. The caller is responsible for:
      1. Persisting the new state to the database.
      2. Creating a ClaimTransitionRecord for the audit log.
      3. Publishing any downstream events.

    This design makes the state machine trivially testable:
        assert apply_transition(RECEIVED, SNIP_PASS) == VALIDATED

    Args:
        current_state: The claim's current AdjudicationState.
        event:         The ClaimEvent to apply.

    Returns:
        The next AdjudicationState after the transition.

    Raises:
        IllegalTransitionError: If no transition is defined for (state, event).
    """
    next_state = TRANSITION_TABLE.get((current_state, event))
    if next_state is None:
        raise IllegalTransitionError(current_state, event)
    return next_state


def get_valid_events(state: AdjudicationState) -> list[ClaimEvent]:
    """
    Return all ClaimEvents that can legally be applied in `state`.

    Useful for API responses and UI state management — shows the caller
    which actions are currently valid for a given claim.
    """
    return [event for (s, event) in TRANSITION_TABLE if s == state]


def build_transition_record(
    claim_id: str,
    from_state: AdjudicationState,
    event: ClaimEvent,
    triggered_by: str,
    reason: str,
    metadata: dict | None = None,
) -> ClaimTransitionRecord:
    """
    Factory function that applies a transition and constructs its audit record atomically.

    Ensures the audit record's `to_state` is always the result of a real
    transition — not a manually typed string — preventing audit log spoofing.

    Args:
        claim_id:     UUID string of the claim being transitioned.
        from_state:   Current state before the transition.
        event:        The event triggering the transition.
        triggered_by: Identifier of who/what triggered the transition.
        reason:       Human-readable reason for the transition.
        metadata:     Optional structured data (e.g., SNIP tier results).

    Returns:
        ClaimTransitionRecord ready for persistence.

    Raises:
        IllegalTransitionError: If the transition is not defined.
    """
    to_state = apply_transition(from_state, event)
    return ClaimTransitionRecord(
        claim_id=claim_id,
        from_state=from_state,
        to_state=to_state,
        event=event,
        triggered_by=triggered_by,
        reason=reason,
        metadata=metadata or {},
    )
