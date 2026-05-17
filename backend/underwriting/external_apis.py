"""
backend/underwriting/external_apis.py
───────────────────────────────────────
Mock async clients for external underwriting data sources.

REGULATORY CONTEXT
───────────────────
Insurers in the United States are permitted to query two primary external
data sources during individual underwriting:

1. MEDICAL INFORMATION BUREAU (MIB)
   The MIB (https://www.mib.com) is a non-profit cooperative owned by ~450
   North American insurers. When an applicant has previously applied for life,
   health, disability, or long-term care insurance, the insurer codes any
   medically significant findings and submits them to MIB. Future insurers
   can then query the bureau to detect undisclosed conditions or
   misrepresentations on new applications.

   Regulatory basis: MIB use is disclosed in the application's Notice of
   Insurance Information Practices (NIIIP). Results are coded in MIB's own
   proprietary code system (not ICD-10) and require the insurer to
   independently verify any adverse finding before acting on it.

2. MILLIMAN INTELLISCRIPT (Prescription Profiling)
   IntelliScript (now operated by Milliman) provides prescription drug history
   data compiled from pharmacy benefit managers (PBMs). It reveals medications
   an applicant is taking (or has taken), which often reveals conditions not
   self-reported on the application.

   Example: An applicant denies diabetes, but IntelliScript shows a 2-year
   history of metformin fills → underwriter applies diabetes debit.

   Regulatory basis: FCRA-covered consumer report. Applicant must be notified
   and given adverse action notice if IntelliScript data contributes to a
   denial or rating.

MOCK IMPLEMENTATION
────────────────────
Both clients are mocked with realistic response structures and simulated
network latency (asyncio.sleep). In production, replace the mock methods
with authenticated HTTPS calls to the actual APIs.

The mock data is keyed on applicant_id mod 3 to produce deterministic
but varied results for development and testing.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

import structlog

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Response DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MIBRecord:
    """
    A single MIB coded entry.

    MIB codes are proprietary (not ICD-10). They indicate categories of
    significant medical findings. The insurer must independently verify before
    acting (e.g., request Attending Physician Statement).
    """
    mib_code: str        # Proprietary MIB code (e.g., "030" = cardiovascular)
    code_description: str
    disclosure_date: str  # When the original insurer submitted this code
    severity: str         # "mild" | "moderate" | "severe"
    requires_aps: bool    # Whether an APS is required to verify


@dataclass
class MIBResponse:
    """Full MIB query response for one applicant."""
    applicant_id: str
    records: list[MIBRecord]
    hit: bool            # True if any MIB record was found
    query_timestamp: str
    latency_ms: float
    # Risk flags in plain terms for scoring.py
    risk_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PrescriptionRecord:
    """
    A single prescription fill record from IntelliScript.

    Represents one NDC/drug category dispensed in the look-back period.
    """
    drug_category: str       # Therapeutic category (e.g., "antidiabetic")
    drug_name: str           # Generic name (e.g., "metformin")
    ndc_code: str | None     # National Drug Code
    fill_date: str
    days_supply: int
    quantity: float
    prescriber_specialty: str | None


@dataclass
class IntelliScriptResponse:
    """Full IntelliScript query response for one applicant."""
    applicant_id: str
    prescriptions: list[PrescriptionRecord]
    hit: bool
    look_back_years: int
    query_timestamp: str
    latency_ms: float
    # Normalized risk flags for scoring.py
    risk_flags: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# MIB Mock Client
# ─────────────────────────────────────────────────────────────────────────────

# Proprietary MIB code → (description, severity, requires_aps, risk_flag)
_MIB_CODE_LIBRARY: dict[str, tuple[str, str, bool, str]] = {
    "030": ("Cardiovascular system impairment", "moderate", True, "cardiovascular_mib"),
    "040": ("Diabetes mellitus", "moderate", False, "diabetes_mib"),
    "045": ("Overweight / obesity", "mild", False, "obesity_mib"),
    "060": ("Respiratory system impairment", "moderate", True, "respiratory_mib"),
    "080": ("Cancer history", "severe", True, "cancer_mib"),
    "100": ("Mental/nervous system impairment", "moderate", True, "mental_health_mib"),
    "120": ("Musculoskeletal impairment", "mild", False, "musculoskeletal_mib"),
    "200": ("Tobacco use reported", "mild", False, "tobacco_mib"),
}

_MOCK_MIB_SCENARIOS: list[list[str]] = [
    [],                  # Scenario 0: Clean — no MIB records
    ["030", "200"],      # Scenario 1: Cardiovascular + tobacco
    ["040", "045"],      # Scenario 2: Diabetes + obesity
    ["080"],             # Scenario 3: Cancer history (severe)
    ["030", "060"],      # Scenario 4: CV + respiratory
]


class MIBClient:
    """
    Mock client for the Medical Information Bureau (MIB) query API.

    Production replacement:
        Replace `query()` with an authenticated HTTPS POST to:
        https://api.mib.com/v2/inquiry
        Headers: Authorization: Bearer {mib_api_token}
        Body: applicant SSN hash, DOB, name (per MIB data use agreement)
    """

    def __init__(self, simulated_latency_ms: float = 180.0) -> None:
        self._latency = simulated_latency_ms / 1000.0

    async def query(
        self,
        applicant_id: str,
        date_of_birth: date | None = None,
        ssn_last4: str | None = None,
    ) -> MIBResponse:
        """
        Query the MIB for any coded disclosures on this applicant.

        Args:
            applicant_id: Internal applicant UUID.
            date_of_birth: Used with name hash to match MIB records.
            ssn_last4:    Last 4 of SSN for disambiguation.

        Returns:
            MIBResponse with all found records and normalized risk_flags.
        """
        import time
        t0 = time.perf_counter()

        # Simulate network latency
        await asyncio.sleep(self._latency + random.uniform(-0.02, 0.05))

        # Deterministic scenario selection based on applicant_id hash
        scenario_idx = hash(applicant_id) % len(_MOCK_MIB_SCENARIOS)
        codes_found = _MOCK_MIB_SCENARIOS[scenario_idx]

        records: list[MIBRecord] = []
        risk_flags: list[str] = []

        for code in codes_found:
            desc, severity, aps, flag = _MIB_CODE_LIBRARY[code]
            records.append(MIBRecord(
                mib_code=code,
                code_description=desc,
                disclosure_date=(date.today() - timedelta(days=random.randint(180, 900))).isoformat(),
                severity=severity,
                requires_aps=aps,
            ))
            risk_flags.append(flag)

        latency_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "mib_query_complete",
            applicant_id=applicant_id,
            hit=bool(records),
            record_count=len(records),
            latency_ms=f"{latency_ms:.0f}",
        )

        return MIBResponse(
            applicant_id=applicant_id,
            records=records,
            hit=bool(records),
            query_timestamp=date.today().isoformat(),
            latency_ms=latency_ms,
            risk_flags=risk_flags,
        )


# ─────────────────────────────────────────────────────────────────────────────
# IntelliScript Mock Client
# ─────────────────────────────────────────────────────────────────────────────

# Drug category → (generic_name, ndc_code, risk_flag)
_RX_LIBRARY: dict[str, tuple[str, str, str]] = {
    "antidiabetic":       ("metformin",       "00093-1048-01", "antihypertensive"),
    "insulin":            ("insulin glargine", "00088-2220-33", "insulin_analog"),
    "antihypertensive":   ("lisinopril",       "00093-3145-01", "antihypertensive"),
    "statin":             ("atorvastatin",     "00069-0157-30", "statin"),
    "antidepressant":     ("sertraline",       "00025-7210-31", "antidepressant"),
    "anticoagulant":      ("warfarin",         "00056-0173-70", "anticoagulant"),
    "specialty_biologic": ("adalimumab",       "00074-9374-01", "specialty_biologic"),
    "chemotherapy":       ("capecitabine",     "00004-1100-09", "chemotherapy"),
    "immunosuppressant":  ("tacrolimus",       "00469-3017-73", "immunosuppressant"),
    "antiretroviral":     ("tenofovir",        "61958-1701-1",  "antiretroviral"),
}

_MOCK_RX_SCENARIOS: list[list[str]] = [
    [],                                    # Clean
    ["antihypertensive", "statin"],        # Mild CV risk
    ["antidiabetic", "antihypertensive"],  # Diabetes + HTN
    ["insulin", "antidiabetic"],           # Insulin-dependent diabetes
    ["specialty_biologic"],                # High-cost specialty drug
    ["antidepressant"],                    # Mental health
    ["chemotherapy"],                      # Active cancer treatment
]


class IntelliScriptClient:
    """
    Mock client for Milliman IntelliScript prescription profiling.

    Production replacement:
        Replace `query()` with an FCRA-compliant authenticated HTTPS call to:
        https://api.milliman.com/intelliscript/v3/inquiry
        Requires prior adverse action notification setup and permissible purpose.
    """

    LOOK_BACK_YEARS: int = 3

    def __init__(self, simulated_latency_ms: float = 250.0) -> None:
        self._latency = simulated_latency_ms / 1000.0

    async def query(
        self,
        applicant_id: str,
        date_of_birth: date | None = None,
    ) -> IntelliScriptResponse:
        """
        Pull prescription fill history for the applicant.

        Returns up to LOOK_BACK_YEARS of fill data. Each unique drug category
        is reported once (aggregated from all fills within the period).

        Args:
            applicant_id: Internal applicant UUID.
            date_of_birth: Required by IntelliScript for identity matching.

        Returns:
            IntelliScriptResponse with prescription records and risk_flags.
        """
        import time
        t0 = time.perf_counter()

        await asyncio.sleep(self._latency + random.uniform(-0.03, 0.08))

        scenario_idx = (hash(applicant_id) + 1) % len(_MOCK_RX_SCENARIOS)
        categories = _MOCK_RX_SCENARIOS[scenario_idx]

        records: list[PrescriptionRecord] = []
        risk_flags: list[str] = []

        for category in categories:
            name, ndc, flag = _RX_LIBRARY[category]
            records.append(PrescriptionRecord(
                drug_category=category,
                drug_name=name,
                ndc_code=ndc,
                fill_date=(date.today() - timedelta(days=random.randint(30, 730))).isoformat(),
                days_supply=30,
                quantity=random.choice([30.0, 60.0, 90.0]),
                prescriber_specialty=None,
            ))
            risk_flags.append(flag)

        latency_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "intelliscript_query_complete",
            applicant_id=applicant_id,
            hit=bool(records),
            drug_categories=categories,
            latency_ms=f"{latency_ms:.0f}",
        )

        return IntelliScriptResponse(
            applicant_id=applicant_id,
            prescriptions=records,
            hit=bool(records),
            look_back_years=self.LOOK_BACK_YEARS,
            query_timestamp=date.today().isoformat(),
            latency_ms=latency_ms,
            risk_flags=risk_flags,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent External Data Enrichment
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_external_underwriting_data(
    applicant_id: str,
    date_of_birth: date | None = None,
    ssn_last4: str | None = None,
) -> tuple[MIBResponse, IntelliScriptResponse]:
    """
    Concurrently query MIB and IntelliScript for one applicant.

    Both queries run in parallel via asyncio.gather — the total latency is
    max(mib_latency, intelliscript_latency) rather than the sum.
    Typical combined latency: ~250ms (dominated by IntelliScript).

    Args:
        applicant_id:  Internal applicant UUID string.
        date_of_birth: Applicant DOB for identity matching.
        ssn_last4:     Last 4 SSN digits for MIB disambiguation.

    Returns:
        Tuple of (MIBResponse, IntelliScriptResponse).
    """
    mib_client = MIBClient()
    rx_client = IntelliScriptClient()

    mib_result, rx_result = await asyncio.gather(
        mib_client.query(applicant_id, date_of_birth, ssn_last4),
        rx_client.query(applicant_id, date_of_birth),
    )

    logger.info(
        "external_data_enrichment_complete",
        applicant_id=applicant_id,
        mib_hit=mib_result.hit,
        rx_hit=rx_result.hit,
        total_flags=len(mib_result.risk_flags) + len(rx_result.risk_flags),
    )

    return mib_result, rx_result
