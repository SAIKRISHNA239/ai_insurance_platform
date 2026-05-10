"""
backend/underwriting/service.py
────────────────────────────────
Underwriting domain service.

Orchestrates the AI-powered risk assessment workflow:
  1. Parse health questionnaire data from Application
  2. Retrieve relevant underwriting guidelines via RAG pipeline
  3. Call LLM for risk narrative generation
  4. Compute composite underwriting score
  5. Map score to risk tier and suggested premium band
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Application, RiskTier

logger = structlog.get_logger(__name__)

# Risk tier thresholds — will be configurable via DB settings table in future
RISK_TIER_THRESHOLDS: dict[RiskTier, tuple[float, float]] = {
    RiskTier.PREFERRED:    (80.0, 100.0),
    RiskTier.STANDARD:     (50.0, 79.9),
    RiskTier.SUBSTANDARD:  (25.0, 49.9),
    RiskTier.DECLINE:      (0.0,  24.9),
}


def score_to_risk_tier(score: float) -> RiskTier:
    """Map a composite underwriting score [0–100] to a RiskTier."""
    for tier, (low, high) in RISK_TIER_THRESHOLDS.items():
        if low <= score <= high:
            return tier
    return RiskTier.DECLINE


async def run_underwriting_pipeline(
    application: Application,
    db: AsyncSession,
) -> dict:
    """
    Entry point for the AI underwriting workflow.

    Args:
        application: The Application ORM instance.
        db: Active async DB session.

    Returns:
        dict with keys: underwriting_score, risk_tier, ai_notes, suggested_premium
    """
    logger.info(
        "underwriting_pipeline_started",
        app_id=str(application.id),
        policy_type=application.policy_type,
    )

    # TODO: Step 1 — embed health_questionnaire and retrieve relevant guidelines
    # TODO: Step 2 — call llm/client.py for risk narrative
    # TODO: Step 3 — parse structured score from LLM response

    # Placeholder result
    score = 70.0
    tier = score_to_risk_tier(score)

    logger.info(
        "underwriting_pipeline_complete",
        app_id=str(application.id),
        score=score,
        tier=tier,
    )
    return {
        "underwriting_score": score,
        "risk_tier": tier,
        "ai_notes": "Underwriting pipeline placeholder — AI integration pending.",
        "suggested_premium": None,
    }
