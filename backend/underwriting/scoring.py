"""
backend/underwriting/scoring.py
────────────────────────────────
Actuarial debit/credit morbidity scoring engine.

ACTUARIAL METHODOLOGY: DEBIT/CREDIT SYSTEM
────────────────────────────────────────────
The debit/credit method is the industry-standard approach for individual
life and health underwriting. Each applicant starts at a baseline "standard"
risk (net score = 0). Risk factors add "debits" (positive points) and
favorable attributes subtract "credits" (negative points).

The net morbidity score drives tier assignment:
  • Net score  0–25  → PREFERRED (STP auto-approve)
  • Net score 26–100 → SUBSTANDARD (conditional loading / table rating)
  • Net score > 100  → DECLINE / MANUAL (HITL queue)

SCORING UNIT CONVENTION
─────────────────────────
1 debit point ≈ 1% additional expected morbidity above standard.
A score of 50 debits means the applicant is expected to generate claims
50% above the standard population — which maps to a premium loading
of approximately 1.5× the standard rate (see decision_router.py).

TABLE RATING SYSTEM
────────────────────
The insurance industry uses "Table Ratings" (Table A through Table P,
or Tables 1–16) to express substandard risk levels. Each table represents
25 additional debits and maps to a 25% premium surcharge:
  Table A (1): 26–50 debits  → +25% loading
  Table B (2): 51–75 debits  → +50% loading
  Table C (3): 76–100 debits → +75% loading
  Table D (4): > 100 debits  → decline or +100%

DECIMAL ARITHMETIC
───────────────────
All financial calculations use Python Decimal to prevent floating-point
rounding errors in premium loading computations — a regulatory requirement
for rate-filed insurance products.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Debit/Credit Tables — Actuarial Reference Data
# ─────────────────────────────────────────────────────────────────────────────

class RiskFactor(str, enum.Enum):
    """Categories of morbidity risk factors assessed in underwriting."""
    BMI              = "bmi"
    TOBACCO          = "tobacco"
    BLOOD_PRESSURE   = "blood_pressure"
    DIABETES         = "diabetes"
    CARDIOVASCULAR   = "cardiovascular"
    CANCER_HISTORY   = "cancer_history"
    MENTAL_HEALTH    = "mental_health"
    MUSCULOSKELETAL  = "musculoskeletal"
    RESPIRATORY      = "respiratory"
    RENAL            = "renal"
    NEUROLOGICAL     = "neurological"
    FAMILY_HISTORY   = "family_history"
    AGE              = "age"
    PRESCRIPTION     = "prescription"
    MIB_FLAG         = "mib_flag"


# ── BMI Debit Table (per actuarial manual) ────────────────────────────────────
# Based on Build Study data: excess mortality/morbidity by BMI band.
BMI_DEBITS: list[tuple[float, float, int]] = [
    # (bmi_min, bmi_max, debits)
    (0.0,   18.4,  20),   # Underweight — increased morbidity risk
    (18.5,  24.9,   0),   # Normal weight — standard (no debit)
    (25.0,  29.9,  10),   # Overweight — mild debit
    (30.0,  34.9,  25),   # Obese Class I — Table A risk
    (35.0,  39.9,  50),   # Obese Class II — Table B risk
    (40.0,  49.9,  75),   # Obese Class III — Table C risk
    (50.0, 999.0, 125),   # Severe obesity — decline territory
]

# ── ICD-10 Diagnosis Debit Table ──────────────────────────────────────────────
# Maps ICD-10 code PREFIXES to (debits, description, permanent_exclusion).
# Prefix matching allows "E11" to match E11, E11.0, E11.9, etc.
ICD10_DEBIT_TABLE: dict[str, tuple[int, str, bool]] = {
    # Cardiovascular
    "I10":  (25, "Hypertension", False),
    "I11":  (50, "Hypertensive heart disease", False),
    "I20":  (75, "Angina pectoris", False),
    "I21":  (100, "Acute myocardial infarction", True),
    "I25":  (75, "Chronic ischemic heart disease", False),
    "I48":  (50, "Atrial fibrillation", False),
    "I50":  (100, "Heart failure", True),
    # Diabetes
    "E10":  (75, "Type 1 diabetes", False),
    "E11":  (50, "Type 2 diabetes", False),
    "E13":  (60, "Other diabetes mellitus", False),
    # Respiratory
    "J44":  (40, "COPD", False),
    "J45":  (25, "Asthma", False),
    # Oncology
    "C34":  (125, "Malignant neoplasm of bronchus/lung", True),
    "C50":  (75, "Malignant neoplasm of breast", False),
    "C61":  (50, "Malignant neoplasm of prostate", False),
    # Mental Health
    "F32":  (15, "Major depressive episode", False),
    "F31":  (30, "Bipolar disorder", False),
    "F20":  (75, "Schizophrenia", True),
    # Neurological
    "G20":  (75, "Parkinson's disease", True),
    "G35":  (75, "Multiple sclerosis", True),
    "G40":  (40, "Epilepsy", False),
    # Renal
    "N18":  (75, "Chronic kidney disease", False),
    # Musculoskeletal
    "M05":  (25, "Rheumatoid arthritis", False),
    "M54":  (10, "Dorsalgia / back pain", False),
}

# ── Favorable Credit Table ─────────────────────────────────────────────────────
# Credits REDUCE the net score, rewarding applicants with excellent health markers.
CREDIT_TABLE: dict[str, tuple[int, str]] = {
    "excellent_blood_pressure":    (-15, "BP < 120/80 mmHg consistently"),
    "non_smoker_5yr":              (-10, "Non-smoker for 5+ years"),
    "regular_exercise":            (-10, "Documented regular aerobic exercise"),
    "normal_bmi":                  (-5,  "BMI 18.5–24.9"),
    "no_family_cardiac_history":   (-5,  "No first-degree cardiac family history"),
    "controlled_diabetes_a1c":     (-15, "Diabetic with HbA1c < 7.0% (excellent control)"),
    "cancer_5yr_remission":        (-20, "Cancer in remission > 5 years"),
    "excellent_lipid_panel":       (-10, "Total cholesterol < 200, HDL > 60"),
}

# ── Tobacco Debit Schedule ─────────────────────────────────────────────────────
TOBACCO_DEBITS: dict[str, int] = {
    "current_smoker":      50,
    "current_chewer":      30,
    "quit_less_than_1yr":  35,
    "quit_1_to_3yr":       20,
    "quit_3_to_5yr":       10,
    "quit_over_5yr":        0,
    "never":                0,
}

# ── Age Debits ─────────────────────────────────────────────────────────────────
# Age is the single largest morbidity predictor. These debits are additive.
AGE_DEBITS: list[tuple[int, int, int]] = [
    # (age_min, age_max, debits)
    (18,  29,   0),
    (30,  39,   5),
    (40,  49,  15),
    (50,  59,  30),
    (60,  69,  50),
    (70,  79,  75),
    (80, 120, 100),
]


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Input Models
# ─────────────────────────────────────────────────────────────────────────────

class ApplicantRiskProfile(BaseModel):
    """
    Structured risk profile assembled from self-reported data,
    MIB records, and prescription profiling (external_apis.py).

    All fields are optional — missing fields do not score, but trigger
    a "data incomplete" flag for the HITL reviewer if the score is borderline.
    """
    application_id: str
    age: int | None = Field(None, ge=0, le=120)
    sex: str | None = Field(None, pattern="^(M|F|X)$")
    height_cm: float | None = Field(None, gt=0)
    weight_kg: float | None = Field(None, gt=0)
    bmi: float | None = Field(None, ge=10.0, le=80.0)

    # Tobacco
    tobacco_status: str | None = None  # Key into TOBACCO_DEBITS

    # Clinical
    diagnosis_codes: list[str] = Field(default_factory=list)
    systolic_bp: int | None = None
    diastolic_bp: int | None = None
    hba1c: float | None = None         # For diabetics: HbA1c %
    total_cholesterol: int | None = None
    hdl_cholesterol: int | None = None

    # Lifestyle
    regular_exercise: bool = False
    family_cardiac_history: bool = False

    # External data (populated by external_apis.py)
    mib_flags: list[str] = Field(default_factory=list)
    prescription_risk_flags: list[str] = Field(default_factory=list)

    # Unstructured narrative (for AI assistant)
    medical_history_narrative: str | None = None

    @field_validator("bmi", mode="before")
    @classmethod
    def _compute_bmi_if_missing(cls, v: Any, info: Any) -> Any:
        """Auto-compute BMI from height/weight if not directly provided."""
        return v  # BMI computation handled in score_applicant()


@dataclass(frozen=True)
class DebitCreditEntry:
    """Single debit or credit entry in the scoring ledger."""
    factor: RiskFactor
    code: str           # Specific code or key (e.g., "E11", "current_smoker")
    points: int         # Positive = debit, negative = credit
    description: str
    permanent_exclusion: bool = False   # Triggers exclusion rider if True


@dataclass
class ScoringLedger:
    """
    Complete debit/credit ledger for one applicant.

    Immutable after `finalize()` is called. The ledger is the auditable
    paper trail of every scoring decision — required for re-insurance
    submissions and state insurance department filings.
    """
    application_id: str
    entries: list[DebitCreditEntry] = field(default_factory=list)
    _finalized: bool = field(default=False, init=False, repr=False)

    @property
    def net_score(self) -> int:
        """Net morbidity score = sum of all debits + sum of all credits."""
        return sum(e.points for e in self.entries)

    @property
    def total_debits(self) -> int:
        return sum(e.points for e in self.entries if e.points > 0)

    @property
    def total_credits(self) -> int:
        return sum(e.points for e in self.entries if e.points < 0)

    @property
    def permanent_exclusions(self) -> list[str]:
        """ICD-10 conditions flagged for permanent exclusion riders."""
        return [e.description for e in self.entries if e.permanent_exclusion]

    @property
    def table_rating(self) -> int:
        """
        Insurance table rating (1–16). Each table = 25 debits.
        Table rating 0 = preferred / standard (no loading).
        """
        net = max(0, self.net_score)
        return min(16, net // 25)

    def add(self, entry: DebitCreditEntry) -> None:
        if self._finalized:
            raise RuntimeError("Cannot modify a finalized ScoringLedger.")
        self.entries.append(entry)

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "net_score": self.net_score,
            "total_debits": self.total_debits,
            "total_credits": self.total_credits,
            "table_rating": self.table_rating,
            "permanent_exclusions": self.permanent_exclusions,
            "entries": [
                {
                    "factor": e.factor.value,
                    "code": e.code,
                    "points": e.points,
                    "description": e.description,
                }
                for e in self.entries
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Scoring Functions
# ─────────────────────────────────────────────────────────────────────────────

def _score_bmi(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> None:
    """Apply BMI debits. Auto-compute BMI from height/weight if needed."""
    bmi = profile.bmi
    if bmi is None and profile.height_cm and profile.weight_kg:
        height_m = profile.height_cm / 100.0
        bmi = profile.weight_kg / (height_m ** 2)

    if bmi is None:
        return

    for bmi_min, bmi_max, debits in BMI_DEBITS:
        if bmi_min <= bmi <= bmi_max:
            if debits == 0:
                ledger.add(DebitCreditEntry(
                    factor=RiskFactor.BMI,
                    code=f"bmi_{bmi:.1f}",
                    points=CREDIT_TABLE.get("normal_bmi", (0,))[0],
                    description="BMI within normal range (credit)",
                ))
            else:
                ledger.add(DebitCreditEntry(
                    factor=RiskFactor.BMI,
                    code=f"bmi_{bmi:.1f}",
                    points=debits,
                    description=f"BMI {bmi:.1f} — elevated morbidity risk",
                ))
            break


def _score_age(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> None:
    if profile.age is None:
        return
    for age_min, age_max, debits in AGE_DEBITS:
        if age_min <= profile.age <= age_max:
            if debits > 0:
                ledger.add(DebitCreditEntry(
                    factor=RiskFactor.AGE,
                    code=f"age_{profile.age}",
                    points=debits,
                    description=f"Age {profile.age} — actuarial age loading",
                ))
            break


def _score_tobacco(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> None:
    if not profile.tobacco_status:
        return
    debits = TOBACCO_DEBITS.get(profile.tobacco_status, 0)
    if debits > 0:
        ledger.add(DebitCreditEntry(
            factor=RiskFactor.TOBACCO,
            code=profile.tobacco_status,
            points=debits,
            description=f"Tobacco use: {profile.tobacco_status}",
        ))
    elif profile.tobacco_status == "non_smoker_5yr":
        credit_pts, desc = CREDIT_TABLE["non_smoker_5yr"]
        ledger.add(DebitCreditEntry(
            factor=RiskFactor.TOBACCO,
            code="non_smoker_5yr",
            points=credit_pts,
            description=desc,
        ))


def _score_diagnoses(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> None:
    """
    Score ICD-10 diagnosis codes using prefix matching.

    Prefix matching ensures that E11.65 (Type 2 DM with hyperglycemia)
    correctly matches the "E11" table entry. This mirrors how real
    underwriting manuals apply ratings to diagnosis code families rather
    than individual codes (which number in the tens of thousands).

    De-duplication: If multiple codes match the same prefix, only the
    highest-debit entry is applied to avoid double-counting a single condition.
    """
    applied_prefixes: set[str] = set()

    # Sort codes so highest-debit prefix wins on de-duplication
    sorted_table = sorted(
        ICD10_DEBIT_TABLE.items(), key=lambda x: x[1][0], reverse=True
    )

    for dx_code in profile.diagnosis_codes:
        dx_upper = dx_code.strip().upper()
        for prefix, (debits, description, exclusion) in sorted_table:
            if dx_upper.startswith(prefix) and prefix not in applied_prefixes:
                applied_prefixes.add(prefix)
                ledger.add(DebitCreditEntry(
                    factor=RiskFactor.CARDIOVASCULAR if prefix.startswith("I")
                           else RiskFactor.DIABETES if prefix.startswith("E")
                           else RiskFactor.CANCER_HISTORY if prefix.startswith("C")
                           else RiskFactor.MENTAL_HEALTH if prefix.startswith("F")
                           else RiskFactor.RESPIRATORY if prefix.startswith("J")
                           else RiskFactor.NEUROLOGICAL if prefix.startswith("G")
                           else RiskFactor.RENAL if prefix.startswith("N")
                           else RiskFactor.MUSCULOSKELETAL,
                    code=dx_code,
                    points=debits,
                    description=f"{description} ({dx_code})",
                    permanent_exclusion=exclusion,
                ))
                break


def _score_blood_pressure(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> None:
    sys_bp = profile.systolic_bp
    dia_bp = profile.diastolic_bp
    if sys_bp is None or dia_bp is None:
        return

    if sys_bp < 120 and dia_bp < 80:
        credit_pts, desc = CREDIT_TABLE["excellent_blood_pressure"]
        ledger.add(DebitCreditEntry(
            factor=RiskFactor.BLOOD_PRESSURE,
            code="bp_excellent",
            points=credit_pts,
            description=desc,
        ))
    elif sys_bp >= 140 or dia_bp >= 90:
        # Stage 2 hypertension — not already captured by ICD-10 I10?
        # Apply if no I10 was already scored
        ledger.add(DebitCreditEntry(
            factor=RiskFactor.BLOOD_PRESSURE,
            code="bp_stage2",
            points=25,
            description=f"Stage 2 hypertension ({sys_bp}/{dia_bp} mmHg)",
        ))
    elif sys_bp >= 130 or dia_bp >= 80:
        ledger.add(DebitCreditEntry(
            factor=RiskFactor.BLOOD_PRESSURE,
            code="bp_stage1",
            points=10,
            description=f"Stage 1 hypertension ({sys_bp}/{dia_bp} mmHg)",
        ))


def _score_metabolic(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> None:
    """Score HbA1c and cholesterol panel."""
    # HbA1c credit for well-controlled diabetes
    if profile.hba1c is not None and profile.hba1c < 7.0:
        if any(dx.startswith("E1") for dx in profile.diagnosis_codes):
            credit_pts, desc = CREDIT_TABLE["controlled_diabetes_a1c"]
            ledger.add(DebitCreditEntry(
                factor=RiskFactor.DIABETES,
                code="hba1c_controlled",
                points=credit_pts,
                description=desc,
            ))

    # Lipid panel credit
    if (
        profile.total_cholesterol is not None
        and profile.hdl_cholesterol is not None
        and profile.total_cholesterol < 200
        and profile.hdl_cholesterol > 60
    ):
        credit_pts, desc = CREDIT_TABLE["excellent_lipid_panel"]
        ledger.add(DebitCreditEntry(
            factor=RiskFactor.CARDIOVASCULAR,
            code="lipid_excellent",
            points=credit_pts,
            description=desc,
        ))


def _score_lifestyle(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> None:
    if profile.regular_exercise:
        credit_pts, desc = CREDIT_TABLE["regular_exercise"]
        ledger.add(DebitCreditEntry(
            factor=RiskFactor.AGE,
            code="regular_exercise",
            points=credit_pts,
            description=desc,
        ))
    if not profile.family_cardiac_history:
        credit_pts, desc = CREDIT_TABLE["no_family_cardiac_history"]
        ledger.add(DebitCreditEntry(
            factor=RiskFactor.FAMILY_HISTORY,
            code="no_family_cardiac",
            points=credit_pts,
            description=desc,
        ))


def _score_mib_flags(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> None:
    """
    Score Medical Information Bureau (MIB) flags.

    MIB flags are coded conditions disclosed by other insurers.
    Each flag adds a 25-debit penalty (Table A minimum) and triggers
    a request for attending physician statement (APS).
    """
    for flag in profile.mib_flags:
        ledger.add(DebitCreditEntry(
            factor=RiskFactor.MIB_FLAG,
            code=f"mib_{flag}",
            points=25,
            description=f"MIB flag: {flag} — APS required",
        ))


def _score_prescriptions(profile: ApplicantRiskProfile, ledger: ScoringLedger) -> None:
    """
    Score IntelliScript prescription risk flags.

    Prescription history reveals conditions applicants may not self-report.
    High-cost specialty drug flags add 50 debits; maintenance drug flags add 15.
    """
    HIGH_COST_RX_TRIGGERS = {
        "insulin_analog", "chemotherapy", "immunosuppressant",
        "antiretroviral", "specialty_biologic",
    }
    MAINTENANCE_RX_TRIGGERS = {
        "antidepressant", "antihypertensive", "statin", "anticoagulant",
    }

    for flag in profile.prescription_risk_flags:
        if flag in HIGH_COST_RX_TRIGGERS:
            ledger.add(DebitCreditEntry(
                factor=RiskFactor.PRESCRIPTION,
                code=f"rx_{flag}",
                points=50,
                description=f"High-cost prescription detected: {flag}",
            ))
        elif flag in MAINTENANCE_RX_TRIGGERS:
            ledger.add(DebitCreditEntry(
                factor=RiskFactor.PRESCRIPTION,
                code=f"rx_{flag}",
                points=15,
                description=f"Maintenance prescription detected: {flag}",
            ))


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def score_applicant(profile: ApplicantRiskProfile) -> ScoringLedger:
    """
    Execute the full debit/credit scoring pipeline for one applicant.

    Runs all scoring modules in a fixed order. Each module appends entries
    to the ScoringLedger. The ledger's net_score property computes the
    running total on demand.

    This function is async to allow future integration of async DB lookups
    (e.g., querying the actuarial rate table from PostgreSQL). Current
    scoring is purely computational (no I/O), so it completes synchronously
    within the async frame.

    Args:
        profile: Fully assembled ApplicantRiskProfile (from external_apis.py).

    Returns:
        Finalized ScoringLedger with complete debit/credit audit trail.
    """
    ledger = ScoringLedger(application_id=profile.application_id)

    _score_age(profile, ledger)
    _score_bmi(profile, ledger)
    _score_tobacco(profile, ledger)
    _score_diagnoses(profile, ledger)
    _score_blood_pressure(profile, ledger)
    _score_metabolic(profile, ledger)
    _score_lifestyle(profile, ledger)
    _score_mib_flags(profile, ledger)
    _score_prescriptions(profile, ledger)

    logger.info(
        "scoring_complete",
        application_id=profile.application_id,
        net_score=ledger.net_score,
        table_rating=ledger.table_rating,
        exclusions=ledger.permanent_exclusions,
    )
    return ledger
