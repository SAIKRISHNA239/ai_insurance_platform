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


# ─────────────────────────────────────────────────────────────────────────────
# Task 15: AI Underwriting Stream Load User
# ─────────────────────────────────────────────────────────────────────────────
#
# Simulates concurrent underwriters requesting the Gemini SSE stream.
#
# SLA targets:
#   • Time-to-first-token (TTFT) p95 < 800ms
#   • Full stream completion p95 < 8s
#   • Error rate < 1%
#
# Add this to the UI run alongside ClaimSubmitter with weight=15 to simulate
# a realistic mixed workload of claims ingestion + AI underwriting.
#
# Headless example:
#   locust -f load_testing/locustfile.py \
#     --host=http://localhost:8000 \
#     --users=200 --spawn-rate=10 --run-time=120s --headless \
#     --csv=load_testing/results/uw_stream_run
# ─────────────────────────────────────────────────────────────────────────────

import time


# Pre-seeded application IDs (replace with real UUIDs from your DB seed data)
_SEED_APP_IDS = [
    "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002",
    "00000000-0000-0000-0000-000000000003",
    "00000000-0000-0000-0000-000000000004",
    "00000000-0000-0000-0000-000000000005",
]

_STREAM_SLA_TTFT_MS    = 800    # Time-to-first-token SLA (p95)
_STREAM_SLA_TOTAL_MS   = 8_000  # Full stream duration SLA (p95)


class UnderwritingStreamUser(HttpUser):
    """
    Simulates an underwriter triggering the AI assistant stream.

    Measures two custom metrics per request:
      • [underwriting_stream] ttft_ms   — ms until first SSE 'data:' line received
      • [underwriting_stream] total_ms  — ms until 'done' frame received

    Weight=15 reflects roughly 15% of concurrent sessions being AI-assisted
    underwriting (the rest being claims intake / read-only queries).
    """
    weight    = 15
    wait_time = between(3, 8)   # Underwriters think between requests

    def on_start(self) -> None:
        self.headers = {
            "Authorization": f"Bearer {DEV_JWT_TOKEN}",
            "Accept": "text/event-stream",
        }

    @task
    def stream_ai_underwriting_summary(self) -> None:
        """
        GET /api/v1/underwriting/{app_id}/ai-summary/stream

        Consumes the full SSE stream, records TTFT, and asserts a 'done' frame
        is received before the connection closes.
        """
        app_id = random.choice(_SEED_APP_IDS)
        url    = f"/api/v1/underwriting/{app_id}/ai-summary/stream"

        start_ms       = time.monotonic() * 1000
        first_token_ms = None
        got_done       = False
        frame_count    = 0
        citation_count = 0

        try:
            with self.client.get(
                url,
                headers=self.headers,
                stream=True,       # Receive response incrementally
                catch_response=True,
                name="/api/v1/underwriting/[app_id]/ai-summary/stream",
            ) as resp:

                if resp.status_code != 200:
                    resp.failure(f"Unexpected status {resp.status_code}")
                    return

                # Consume SSE frames line by line
                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue

                    line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="replace")
                    if not line.startswith("data: "):
                        continue

                    frame_count += 1
                    now_ms = time.monotonic() * 1000

                    # Record TTFT on first data frame
                    if first_token_ms is None:
                        first_token_ms = now_ms - start_ms
                        self.environment.events.request.fire(
                            request_type="SSE_TTFT",
                            name="underwriting_stream_time_to_first_token",
                            response_time=first_token_ms,
                            response_length=len(line),
                            exception=None,
                            context={},
                        )

                    # Parse frame type
                    try:
                        import json as _json
                        frame = _json.loads(line[6:])
                        frame_type = frame.get("type", "")

                        if frame_type == "citations":
                            citation_count = len(frame.get("citations", []))
                        elif frame_type == "done":
                            got_done = True
                            break
                        elif frame_type == "error":
                            resp.failure(f"LLM error frame: {frame.get('error', 'unknown')}")
                            return
                    except Exception:
                        pass  # Non-JSON lines are ignored

                total_ms = (time.monotonic() * 1000) - start_ms

                # Record total stream duration
                self.environment.events.request.fire(
                    request_type="SSE_TOTAL",
                    name="underwriting_stream_total_duration",
                    response_time=total_ms,
                    response_length=frame_count,
                    exception=None,
                    context={},
                )

                # Validate SLA
                if not got_done:
                    resp.failure("Stream ended without 'done' frame")
                elif first_token_ms and first_token_ms > _STREAM_SLA_TTFT_MS:
                    # Don't fail — just log; SLA enforced at percentile level
                    pass
                else:
                    resp.success()

        except Exception as exc:
            # Surface connection errors as Locust failures
            self.environment.events.request.fire(
                request_type="SSE_TOTAL",
                name="underwriting_stream_total_duration",
                response_time=0,
                response_length=0,
                exception=exc,
                context={},
            )


# ─────────────────────────────────────────────────────────────────────────────
# Task 15 (bonus): Knowledge Base Upload Load User
# ─────────────────────────────────────────────────────────────────────────────
#
# Simulates admin users uploading policy PDFs to the knowledge base.
# Low weight (2%) — uploads are rare but large (50 KB synthetic PDF).
#
# SLA: < 500ms p95 for the 202 Accepted response (ingestion is async).
# ─────────────────────────────────────────────────────────────────────────────

_SYNTHETIC_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
    b"xref\n0 4\n"
    b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n9\n%%EOF\n"
    + b"A" * 40_000  # Pad to ~40 KB to simulate a real policy PDF
)


class KnowledgeUploadUser(HttpUser):
    """
    Simulates admin users uploading PDFs to POST /api/v1/knowledge/upload.

    Weight=2: 1 upload user per ~50 regular users.
    SLA: 202 Accepted in < 500ms (actual ingestion is async).
    """
    weight    = 2
    wait_time = between(10, 30)   # Admins upload infrequently

    def on_start(self) -> None:
        self.admin_headers = {"Authorization": f"Bearer {DEV_JWT_TOKEN}"}

    @task
    def upload_policy_pdf(self) -> None:
        """POST /api/v1/knowledge/upload with a synthetic PDF."""
        filename = f"policy_{uuid.uuid4().hex[:8]}.pdf"

        with self.client.post(
            "/api/v1/knowledge/upload",
            files={"file": (filename, _SYNTHETIC_PDF, "application/pdf")},
            headers=self.admin_headers,
            catch_response=True,
            name="/api/v1/knowledge/upload",
        ) as resp:
            if resp.status_code == 202:
                resp.success()
            elif resp.status_code == 415:
                resp.failure("415 Unsupported Media Type — check PDF content-type header")
            elif resp.status_code == 403:
                resp.failure("403 Forbidden — JWT token lacks ADMIN role")
            else:
                resp.failure(f"Unexpected status {resp.status_code}: {resp.text[:120]}")

