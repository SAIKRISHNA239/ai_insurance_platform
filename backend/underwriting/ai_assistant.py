"""
backend/underwriting/ai_assistant.py
──────────────────────────────────────
LLM-powered Underwriting Assistant (HITL second brain).

ROLE OF THE LLM IN UNDERWRITING
───────────────────────────────
The LLM is NOT an autonomous decision maker for complex or declined cases.
Insurers cannot legally deny coverage solely based on black-box AI output
due to Fair Credit Reporting Act (FCRA) and state DOI transparency rules.

Instead, the LLM acts as an Actuarial Analyst (a "Second Brain"). When a
case hits the manual review queue (score > 100 or missing critical data),
the LLM:
  1. Ingests the applicant's unstructured medical narrative.
  2. Synthesizes it with the structured MIB and IntelliScript flags.
  3. Generates a concise, highly structured clinical summary for the human
     Underwriter.
  4. Proposes a decision (Decline vs. Table Rating) with specific reasoning.

The human underwriter makes the final, legally binding decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, Field

from backend.llm.client import get_llm_client
from backend.underwriting.scoring import ApplicantRiskProfile, ScoringLedger

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# AI Output Schema (Structured Generation)
# ─────────────────────────────────────────────────────────────────────────────

class AINarrativeSummary(BaseModel):
    """
    Structured output forced from the LLM using JSON mode or tool calling.
    Ensures the underwriter gets a consistent, scannable format.
    """
    clinical_summary: str = Field(
        description="A 2-3 sentence summary of the applicant's overall health profile."
    )
    key_impairments: list[str] = Field(
        description="Bullet points of the major morbidity drivers (e.g., 'Uncontrolled Type 2 Diabetes')."
    )
    data_discrepancies: list[str] = Field(
        description="Contradictions between self-reported data and external MIB/Rx data. Empty if none."
    )
    proposed_decision: str = Field(
        description="Must be exactly one of: 'DECLINE', 'TABLE_RATING', or 'POSTPONE'."
    )
    reasoning: str = Field(
        description="Actuarial justification for the proposed decision."
    )
    suggested_requirements: list[str] = Field(
        description="e.g., 'Order Attending Physician Statement (APS)', 'Require current A1c lab'."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt Engineering
# ─────────────────────────────────────────────────────────────────────────────

_UNDERWRITER_SYSTEM_PROMPT = """
You are an expert Chief Medical Underwriter for a major US life and health insurer.
Your task is to analyze an applicant's risk profile and generate a concise clinical
summary for a human underwriter who will make the final binding decision.

You will be provided with:
1. The applicant's basic demographics and vitals.
2. The mathematical debit/credit score already computed by the actuarial engine.
3. External data flags from the Medical Information Bureau (MIB) and prescription history.
4. An unstructured self-reported medical history narrative.

YOUR OBJECTIVES:
1. Synthesize the clinical picture.
2. Identify discrepancies (e.g., applicant denied tobacco, but MIB flag shows tobacco use).
3. Propose a decision:
   - DECLINE: Severe, uninsurable risk (e.g., active cancer, severe heart failure).
   - TABLE_RATING: Substandard risk that can be insured with a premium surcharge.
   - POSTPONE: Temporarily uninsurable (e.g., awaiting surgery, pregnant with complications).
4. Suggest what additional medical evidence the human underwriter should order (APS, labs, etc.).

CONSTRAINTS:
- Be highly concise. Actuaries process dozens of cases a day.
- Do not use flowery language. Use standard clinical terminology.
- You must return valid JSON matching the requested schema.
"""


def _build_prompt_context(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> str:
    """Format the applicant data into a clean text block for the LLM."""
    lines = [
        f"--- APPLICANT VITALS ---",
        f"Age: {profile.age}",
        f"Sex: {profile.sex}",
        f"BMI: {profile.bmi:.1f}" if profile.bmi else "BMI: Not provided",
        f"Tobacco Status: {profile.tobacco_status or 'Not reported'}",
        f"Blood Pressure: {profile.systolic_bp}/{profile.diastolic_bp}" if profile.systolic_bp else "BP: Not reported",
        "",
        f"--- ACTUARIAL ENGINE SCORE ---",
        f"Net Score: {ledger.net_score} debits",
        f"Computed Table Rating: {ledger.table_rating}",
        f"Permanent Exclusions Triggered: {', '.join(ledger.permanent_exclusions) or 'None'}",
        "",
        f"--- EXTERNAL DATA (VERIFIED) ---",
        f"MIB Flags: {', '.join(profile.mib_flags) or 'None'}",
        f"IntelliScript Rx Flags: {', '.join(profile.prescription_risk_flags) or 'None'}",
        "",
        f"--- SELF-REPORTED NARRATIVE ---",
        f"{profile.medical_history_narrative or 'No additional narrative provided.'}",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def generate_hitl_summary(
    profile: ApplicantRiskProfile,
    ledger: ScoringLedger,
) -> dict[str, Any]:
    """
    Generate the AI Underwriting Assistant narrative for the manual review queue.

    Called ONLY when the decision router sends a case to the HITL queue.
    Uses JSON mode to guarantee structured output.

    Args:
        profile: The full applicant risk profile.
        ledger:  The finalized scoring ledger produced by the actuarial engine.

    Returns:
        A dictionary matching the AINarrativeSummary Pydantic schema.
    """
    context = _build_prompt_context(profile, ledger)

    llm = get_llm_client()

    logger.info("requesting_underwriting_ai_summary", application_id=profile.application_id)

    try:
        # Request JSON output using the system prompt to enforce schema
        raw_response = await llm.complete(
            system_prompt=_UNDERWRITER_SYSTEM_PROMPT,
            user_message=context,
            temperature=0.1,  # Low temperature for highly deterministic/analytical output
            response_format={"type": "json_object"},
        )

        # Parse and validate through Pydantic to ensure safety
        data = json.loads(raw_response)
        summary = AINarrativeSummary.model_validate(data)

        logger.info(
            "ai_summary_generated",
            application_id=profile.application_id,
            proposed_decision=summary.proposed_decision,
        )
        return summary.model_dump()

    except Exception as exc:
        logger.error(
            "ai_summary_generation_failed",
            application_id=profile.application_id,
            error=str(exc),
        )
        # Graceful degradation: If LLM fails, return a safe fallback so the
        # human reviewer still gets the case in their queue, just without AI notes.
        return {
            "clinical_summary": "AI summarization failed. Manual review of raw application data required.",
            "key_impairments": ["Error generating impairments."],
            "data_discrepancies": [],
            "proposed_decision": "MANUAL_REVIEW_REQUIRED",
            "reasoning": "System error during LLM generation.",
            "suggested_requirements": ["Review raw application and external reports manually."],
        }
