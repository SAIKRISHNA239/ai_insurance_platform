"""
backend/underwriting/decision_router.py
────────────────────────────────────────
Underwriting decision routing engine: STP vs Conditional vs Manual Review.

ROUTING ARCHITECTURE
────────────────────
After the debit/credit scoring engine produces the ScoringLedger, this
router determines the final path for the application.

1. TRUE STRAIGHT-THROUGH PROCESSING (STP)
   - Condition: Net score 0–25 AND no permanent exclusions.
   - Action: Autonomously approve at Standard or Preferred rates.
   - Human Touch: 0

2. CONDITIONAL DECISIONING ("The Missing Middle")
   - Condition: Net score 26–100 OR single permanent exclusion.
   - Action: Autonomously issue with calculated Table Rating (premium surcharge)
             or specific exclusion riders (e.g., "Covered, except for pre-existing back pain").
   - Human Touch: 0 (This is the primary ROI driver of the AI platform).

3. MANUAL REVIEW QUEUE (HITL)
   - Condition: Net score > 100 OR complex MIB/Rx discrepancies OR incomplete data.
   - Action: Route to a human underwriter. Invoke the AI Assistant to pre-summarize.
   - Human Touch: 1 (Human makes final decision, aided by AI).

PREMIUM CALCULATION
────────────────────
Base Premium is modified by the Table Rating.
Each Table (25 debits) adds exactly 25% to the base premium.
Base premium = $100.
Table B (2) = +50% = $150 final premium.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import structlog

from backend.database.models import RiskTier
from backend.underwriting.ai_assistant import generate_hitl_summary
from backend.underwriting.scoring import ApplicantRiskProfile, ScoringLedger

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Router Thresholds & Constants
# ─────────────────────────────────────────────────────────────────────────────

# Maximum score allowed for STP auto-approval
STP_MAX_SCORE = 25

# Maximum score allowed for autonomous conditional issuance (Table Rating)
# Scores above this go to the manual review queue.
CONDITIONAL_MAX_SCORE = 100

# Base premium calculation variables (mock actuarial factors)
BASE_MONTHLY_RATE_PER_100K = Decimal("15.50")


# ─────────────────────────────────────────────────────────────────────────────
# Result DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UnderwritingDecision:
    """The final autonomous decision or routing recommendation."""
    application_id: str
    route: str                 # "stp_approved" | "conditional_approved" | "manual_review"
    risk_tier: RiskTier
    net_score: int
    table_rating: int
    suggested_premium: Decimal | None
    permanent_exclusions: list[str]
    ai_assistant_summary: dict[str, Any] | None = None
    routing_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "route": self.route,
            "risk_tier": self.risk_tier.value,
            "net_score": self.net_score,
            "table_rating": self.table_rating,
            "suggested_premium": str(self.suggested_premium) if self.suggested_premium else None,
            "permanent_exclusions": self.permanent_exclusions,
            "ai_assistant_summary": self.ai_assistant_summary,
            "routing_reason": self.routing_reason,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Premium Calculation
# ─────────────────────────────────────────────────────────────────────────────

def calculate_premium(
    requested_coverage: Decimal,
    table_rating: int,
    base_rate_per_100k: Decimal = BASE_MONTHLY_RATE_PER_100K,
) -> Decimal:
    """
    Calculate final monthly premium based on coverage amount and Table Rating.

    Math:
      Base Premium = (Coverage / 100,000) * base_rate
      Table Loading = 1.0 + (table_rating * 0.25)
      Final Premium = Base Premium * Table Loading

    Uses strict Decimal arithmetic rounded to 2 decimal places.
    """
    coverage_units = requested_coverage / Decimal("100000")
    base_premium = coverage_units * base_rate_per_100k

    # Each table adds 25% (0.25) loading
    loading_factor = Decimal("1.00") + (Decimal(table_rating) * Decimal("0.25"))

    final_premium = base_premium * loading_factor
    return final_premium.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ─────────────────────────────────────────────────────────────────────────────
# Routing Logic
# ─────────────────────────────────────────────────────────────────────────────

async def route_application(
    profile: ApplicantRiskProfile,
    ledger: ScoringLedger,
    requested_coverage_limit: Decimal,
) -> UnderwritingDecision:
    """
    Determine the application's path based on the finalized ScoringLedger.

    Args:
        profile: The applicant's assembled risk profile.
        ledger: The finalized scoring ledger.
        requested_coverage_limit: Target policy coverage in USD.

    Returns:
        UnderwritingDecision object determining the next workflow step.
    """
    score = ledger.net_score
    exclusions = ledger.permanent_exclusions

    # Calculate base premium assuming standard or substandard issue
    suggested_premium = calculate_premium(
        requested_coverage=requested_coverage_limit,
        table_rating=ledger.table_rating,
    )

    # ── Path 1: TRUE STRAIGHT-THROUGH PROCESSING (STP) ──────────────────────
    if score <= STP_MAX_SCORE and not exclusions:
        # Check if score is negative enough for Preferred tier
        tier = RiskTier.PREFERRED if score <= 0 else RiskTier.STANDARD

        logger.info(
            "routing_stp_approved",
            application_id=profile.application_id,
            score=score,
            tier=tier.value,
        )
        return UnderwritingDecision(
            application_id=profile.application_id,
            route="stp_approved",
            risk_tier=tier,
            net_score=score,
            table_rating=0,
            suggested_premium=suggested_premium,
            permanent_exclusions=[],
            routing_reason=f"Clean application (score {score}). Auto-approved via STP.",
        )

    # ── Path 2: CONDITIONAL AUTONOMOUS DECISIONING ──────────────────────────
    # Missing Middle: Handle substandard cases autonomously without human touch
    if score <= CONDITIONAL_MAX_SCORE:
        # Can be standard (with exclusions) or substandard (Table Rated)
        tier = RiskTier.SUBSTANDARD if score > 25 else RiskTier.STANDARD

        reason = (
            f"Autonomous substandard issue. "
            f"Table rating: {ledger.table_rating} (+{ledger.table_rating * 25}% loading). "
            f"Exclusions: {len(exclusions)}."
        )

        logger.info(
            "routing_conditional_approved",
            application_id=profile.application_id,
            score=score,
            tier=tier.value,
            table_rating=ledger.table_rating,
        )
        return UnderwritingDecision(
            application_id=profile.application_id,
            route="conditional_approved",
            risk_tier=tier,
            net_score=score,
            table_rating=ledger.table_rating,
            suggested_premium=suggested_premium,
            permanent_exclusions=exclusions,
            routing_reason=reason,
        )

    # ── Path 3: MANUAL REVIEW QUEUE (HITL) ──────────────────────────────────
    # Severe risk or highly complex case. Requires human actuary.
    # We invoke the LLM to summarize the case for the human.
    logger.info(
        "routing_manual_review",
        application_id=profile.application_id,
        score=score,
    )

    ai_summary = await generate_hitl_summary(profile, ledger)

    return UnderwritingDecision(
        application_id=profile.application_id,
        route="manual_review",
        risk_tier=RiskTier.DECLINE,  # Initial assumption until human approves
        net_score=score,
        table_rating=ledger.table_rating,
        suggested_premium=None,      # Let the human set the final premium
        permanent_exclusions=exclusions,
        ai_assistant_summary=ai_summary,
        routing_reason=f"Score {score} exceeds conditional max ({CONDITIONAL_MAX_SCORE}). Routing to human underwriter.",
    )
