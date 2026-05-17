"""
load_testing/locustfile.py
───────────────────────────
High-concurrency load test for the Claims Intake API using Locust.

HOW TO RUN LOCALLY
───────────────────
  pip install locust faker
  locust -f load_testing/locustfile.py --host=http://localhost:8000

Then open the Locust UI at http://localhost:8089 and configure:
  • Number of users:    1000
  • Spawn rate:        50 users/second
  • Duration:          120 seconds

HEADLESS MODE (CI / load testing server):
  locust -f load_testing/locustfile.py \
    --host=http://localhost:8000 \
    --users=1000 \
    --spawn-rate=50 \
    --run-time=120s \
    --headless \
    --csv=load_testing/results/run_$(date +%Y%m%d_%H%M)

WHAT IS BEING MEASURED
────────────────────────
1. ASYNCHRONOUS EVENT LOOP THROUGHPUT
   The `/claims/intake` endpoint is designed for sub-100ms p95 latency even
   under 1,000 concurrent users. It must NOT block — no LLM or UM routing
   occurs synchronously. If p95 > 200ms, the async event loop has a bottleneck.

2. KAFKA PUBLISHER BOTTLENECK
   The MockKafkaProducer (dev) or AIOKafkaProducer (production) is called
   once per valid claim. Under 1,000 RPS, the Kafka producer's batch window
   (linger_ms=5) and max_batch_size should prevent individual send latency
   from accumulating. Alert threshold: kafka_publish_time > 50ms p95.

3. SNIP VALIDATION THROUGHPUT
   SNIP runs synchronously in the async frame (pure CPU, no I/O). Under
   1,000 concurrent requests, Python's GIL becomes visible. If SNIP
   processing causes latency spikes, deploy multiple Uvicorn workers
   (--workers=4) to distribute CPU load across processes.

4. DATABASE CONNECTION POOL EXHAUSTION
   SQLAlchemy's async engine uses a connection pool (pool_size=10 by default).
   Under 1,000 concurrent requests, pool exhaustion causes `QueuePool Overflow`
   errors. Monitor via Prometheus `sqlalchemy_pool_checkedout` metric.
   Remediation: Increase pool_size or deploy PgBouncer as a connection pooler.

LOCUST USER STRATEGY
──────────────────────
Three weighted user types simulate a realistic claims submission mix:

  1. ClaimSubmitter (weight=70):  Submits valid 837P claims.
                                  Expects HTTP 202 Accepted.
  2. InvalidClaimUser (weight=20): Submits intentionally unbalanced claims.
                                   Expects HTTP 422 (SNIP rejection).
                                   Tests the rejection fast path.
  3. ReadOnlyUser (weight=10):    Polls the claims list endpoint.
                                   Tests read concurrency.
"""

from __future__ import annotations

import random
import uuid

from faker import Faker
from locust import HttpUser, between, events, task
from locust.env import Environment

fake = Faker()


# ─────────────────────────────────────────────────────────────────────────────
# Auth Token (shared across users)
# ─────────────────────────────────────────────────────────────────────────────

# In a real load test, obtain a JWT via the /auth/token endpoint once
# and share it across all user instances via a module-level variable.
# For development, we use a static dev token (configured in backend .env).
DEV_JWT_TOKEN = "dev-load-test-token"


# ─────────────────────────────────────────────────────────────────────────────
# Payload Factories
# ─────────────────────────────────────────────────────────────────────────────

# Valid 10-digit NPIs that pass Luhn checksum (pre-validated)
VALID_NPIS = [
    "1234567893",
    "1245319599",
    "1083675400",
    "1669436987",
]

CPT_CODES = ["99213", "99214", "99215", "99203", "99204", "99232", "99238"]
ICD10_CODES = ["E11.9", "I10", "J45.31", "M54.5", "F32.1", "G40.909"]


def _make_valid_claim_payload() -> dict:
    """
    Generate a balanced 837P claim payload that will pass all SNIP tiers.

    Balance contract enforced: the single line-item charge equals the header total.
    """
    charge = round(random.uniform(50.0, 5000.0), 2)
    return {
        "transaction_set": "837P",
        "interchange_control_number": f"ICN{uuid.uuid4().hex[:14].upper()}",
        "billing_provider_npi": random.choice(VALID_NPIS),
        "policy_id": str(uuid.uuid4()),
        "service_date_start": "2024-03-01",
        "service_date_end": "2024-03-01",
        "diagnosis_codes": random.sample(ICD10_CODES, k=random.randint(1, 3)),
        "procedure_lines": [
            {
                "line_number": 1,
                "procedure_code": random.choice(CPT_CODES),
                "modifier": None,
                "units": random.randint(1, 3),
                "charge_amount": str(charge),
                "place_of_service": "11",
                "rendering_provider_npi": None,
            }
        ],
        "total_charge": str(charge),
        "place_of_service": "11",
    }


def _make_invalid_claim_payload() -> dict:
    """
    Generate a claim that will fail SNIP Tier 3 (balance mismatch).
    Line charge $100 vs header total $999.
    """
    payload = _make_valid_claim_payload()
    payload["total_charge"] = "999.99"   # Intentional mismatch
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# User Classes
# ─────────────────────────────────────────────────────────────────────────────

class ClaimSubmitter(HttpUser):
    """
    Primary load user: submits valid EDI 837P claims.

    Expected behavior: HTTP 202 Accepted in < 100ms p95.
    Wait time between tasks: 1–3 seconds (simulates a clearinghouse batch feed).
    """
    weight = 70
    wait_time = between(1, 3)

    def on_start(self) -> None:
        """Authenticate once per virtual user."""
        self.headers = {
            "Authorization": f"Bearer {DEV_JWT_TOKEN}",
            "Content-Type": "application/json",
        }

    @task(10)
    def submit_single_claim(self) -> None:
        """Submit one valid claim — the primary hot path."""
        payload = _make_valid_claim_payload()
        with self.client.post(
            "/claims/intake",
            json=payload,
            headers=self.headers,
            catch_response=True,
            name="/claims/intake [valid]",
        ) as response:
            if response.status_code == 202:
                response.success()
            elif response.status_code == 422:
                # Unexpected SNIP rejection for a valid payload — mark as failure
                response.failure(f"Unexpected 422: {response.text[:200]}")
            else:
                response.failure(f"Unexpected status {response.status_code}")

    @task(2)
    def submit_high_cost_claim(self) -> None:
        """Submit a high-cost surgical claim (triggers UM routing path)."""
        payload = _make_valid_claim_payload()
        payload["procedure_lines"][0]["procedure_code"] = "27447"   # Total knee
        payload["procedure_lines"][0]["charge_amount"] = "15000.00"
        payload["total_charge"] = "15000.00"
        payload["diagnosis_codes"] = ["M17.11", "Z96.641"]

        with self.client.post(
            "/claims/intake",
            json=payload,
            headers=self.headers,
            catch_response=True,
            name="/claims/intake [high-cost]",
        ) as response:
            if response.status_code in (202, 202):
                response.success()
            else:
                response.failure(f"Status {response.status_code}")

    @task(1)
    def get_claims_list(self) -> None:
        """Read claims list — tests read concurrency under write load."""
        with self.client.get(
            "/claims/?page=1&page_size=20",
            headers=self.headers,
            catch_response=True,
            name="/claims/ [list]",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")


class InvalidClaimUser(HttpUser):
    """
    Tests the SNIP rejection fast path under concurrent load.

    Expected behavior: HTTP 422 with structured error in < 50ms.
    The rejection path is faster than the happy path (no DB write, no Kafka).
    """
    weight = 20
    wait_time = between(1, 5)

    def on_start(self) -> None:
        self.headers = {
            "Authorization": f"Bearer {DEV_JWT_TOKEN}",
            "Content-Type": "application/json",
        }

    @task
    def submit_unbalanced_claim(self) -> None:
        """Submit claim with balance mismatch — expects 422."""
        payload = _make_invalid_claim_payload()
        with self.client.post(
            "/claims/intake",
            json=payload,
            headers=self.headers,
            catch_response=True,
            name="/claims/intake [snip-reject]",
        ) as response:
            if response.status_code == 422:
                data = response.json()
                if "SNIP_VALIDATION_FAILED" in str(data.get("detail", "")):
                    response.success()
                else:
                    response.failure("422 but not a SNIP error")
            else:
                response.failure(f"Expected 422, got {response.status_code}")


class ReadOnlyUser(HttpUser):
    """
    Read-only polling user — tests read throughput under heavy write load.
    """
    weight = 10
    wait_time = between(2, 8)

    def on_start(self) -> None:
        self.headers = {"Authorization": f"Bearer {DEV_JWT_TOKEN}"}

    @task(3)
    def poll_claims_list(self) -> None:
        self.client.get("/claims/?page=1", headers=self.headers,
                        name="/claims/ [read]")

    @task(1)
    def health_check(self) -> None:
        self.client.get("/health", name="/health")


# ─────────────────────────────────────────────────────────────────────────────
# Custom Locust Event Hooks
# ─────────────────────────────────────────────────────────────────────────────

@events.test_stop.add_listener
def on_test_stop(environment: Environment, **kwargs) -> None:
    """Print a pass/fail summary based on error rate and latency thresholds."""
    stats = environment.stats.total

    error_rate = stats.fail_ratio * 100
    p95_ms = stats.get_response_time_percentile(0.95) or 0
    rps = stats.current_rps

    print("\n" + "=" * 60)
    print("  LOAD TEST SUMMARY")
    print(f"  Total Requests : {stats.num_requests}")
    print(f"  Total Failures : {stats.num_failures}")
    print(f"  Error Rate     : {error_rate:.2f}%")
    print(f"  p95 Latency    : {p95_ms:.0f} ms")
    print(f"  Peak RPS       : {rps:.1f}")
    print("=" * 60)

    # SLA thresholds
    if error_rate > 1.0:
        print(f"  ❌ FAIL: Error rate {error_rate:.2f}% exceeds 1% SLA")
        environment.process_exit_code = 1
    elif p95_ms > 500:
        print(f"  ❌ FAIL: p95 latency {p95_ms:.0f}ms exceeds 500ms SLA")
        environment.process_exit_code = 1
    else:
        print("  ✅ PASS: All SLA thresholds met")
        environment.process_exit_code = 0
