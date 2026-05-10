"""
backend/policies/service.py
────────────────────────────
Policies domain service.

Future responsibilities:
  • Policy number generation (format: POL-YYYY-TYPE-XXXXXXXX)
  • Policy renewal and lapse detection
  • Premium recalculation on life event changes
  • Coverage validation for claim eligibility checks
"""
from __future__ import annotations
import datetime, uuid, structlog
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database.models import Policy

logger = structlog.get_logger(__name__)


async def generate_policy_number(policy_type: str) -> str:
    """Generate a unique, formatted policy number."""
    year = datetime.date.today().year
    short_id = str(uuid.uuid4()).upper().replace("-", "")[:8]
    return f"POL-{year}-{policy_type[:3].upper()}-{short_id}"


async def check_policy_lapse(policy: Policy, db: AsyncSession) -> bool:
    """Return True if the policy should transition to LAPSED status."""
    if policy.expiry_date < datetime.date.today():
        logger.info("policy_lapsed", policy_id=str(policy.id))
        return True
    return False
