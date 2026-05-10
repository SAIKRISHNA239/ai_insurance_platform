/**
 * frontend/e2e/citation_ui.spec.ts
 * ──────────────────────────────────────────────────────────────────────────────
 * Playwright E2E tests for the Citation-First HITL Underwriting Dashboard.
 *
 * HOW TO RUN LOCALLY
 * ───────────────────
 *   cd ai_insurance_platform/frontend
 *   npm install
 *   npx playwright install --with-deps chromium
 *   npx playwright test e2e/citation_ui.spec.ts --headed
 *
 * To run headlessly (CI mode):
 *   npx playwright test e2e/citation_ui.spec.ts
 *
 * To see the trace viewer on failure:
 *   npx playwright test e2e/citation_ui.spec.ts --trace=on
 *   npx playwright show-trace test-results/.../trace.zip
 *
 * MOCKING STRATEGY
 * ──────────────────
 * These tests do NOT require the FastAPI backend to be running. Playwright's
 * route interception (page.route) mocks all API calls:
 *
 *   • GET /applications/         → returns a fixture application list
 *   • GET /applications/{id}/decision → returns a fixture decision
 *   • GET /underwriting/{id}/ai-summary/stream → returns a mock SSE stream
 *     with citation markers [^1] and a final citations payload
 *
 * The PDF viewer (CitationViewer) is also tested with a mocked PDF URL served
 * by Playwright's local route interceptor.
 *
 * WHY PLAYWRIGHT NOT CYPRESS?
 * ─────────────────────────────
 * Playwright supports testing SSE (Server-Sent Events) via route interception
 * and ReadableStream mocking. Cypress has known limitations with streaming
 * responses that would require significant workarounds.
 */

import { test, expect, Page, Route } from "@playwright/test";

// ─── Fixture Data ──────────────────────────────────────────────────────────────

const MOCK_APPLICATION_ID = "app-test-uuid-001";
const MOCK_POLICY_ID = "pol-test-uuid-001";

const MOCK_APPLICATIONS = {
  total: 1,
  page: 1,
  page_size: 20,
  items: [
    {
      id: MOCK_APPLICATION_ID,
      application_number: "APP-2024-0001",
      policy_type: "individual_health",
      status: "pending_review",
      requested_coverage_limit: "500000.00",
      underwriting_score: 72,
      risk_tier: "substandard",
      ai_underwriting_notes: null,
      created_at: "2024-03-01T10:00:00Z",
    },
  ],
};

const MOCK_DECISION = {
  application_id: MOCK_APPLICATION_ID,
  route: "manual_review",
  risk_tier: "substandard",
  net_score: 72,
  table_rating: 2,
  suggested_premium: "45.75",
  permanent_exclusions: ["Heart failure (I50)"],
  routing_reason: "Score 72 exceeds STP threshold. Routing to human underwriter.",
  ai_assistant_summary: {
    clinical_summary: "Applicant presents with Type 2 Diabetes and obesity.",
    key_impairments: ["Type 2 Diabetes (E11)", "BMI 36 (Obese Class II)"],
    data_discrepancies: [],
    proposed_decision: "TABLE_RATING",
    reasoning: "Substandard but insurable with Table B loading.",
    suggested_requirements: ["Order HbA1c lab within 90 days"],
  },
};

const MOCK_CITATION = {
  id: 1,
  chunk_id: "chunk-abc-123",
  document_name: "Member Benefits Guide 2024.pdf",
  document_url: "/test-fixtures/sample.pdf",
  bounding_box: { page: 3, x: 0.1, y: 0.2, width: 0.8, height: 0.05 },
  excerpt: "Coverage for diabetes management includes annual HbA1c testing.",
};

/** SSE stream that emits tokens then a citations payload. */
const MOCK_SSE_RESPONSE = [
  `data: ${JSON.stringify({ type: "token", content: "The applicant " })}\n\n`,
  `data: ${JSON.stringify({ type: "token", content: "has Type 2 Diabetes " })}\n\n`,
  `data: ${JSON.stringify({ type: "token", content: "[^1] " })}\n\n`,
  `data: ${JSON.stringify({ type: "token", content: "confirmed by MIB records." })}\n\n`,
  `data: ${JSON.stringify({ type: "citations", citations: [MOCK_CITATION] })}\n\n`,
  `data: ${JSON.stringify({ type: "done" })}\n\n`,
].join("");


// ─── Shared Setup ──────────────────────────────────────────────────────────────

async function mockAPIRoutes(page: Page): Promise<void> {
  // Applications list
  await page.route("**/applications/**", async (route: Route) => {
    const url = route.request().url();
    if (url.includes("/decision")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(MOCK_DECISION),
      });
    } else {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(MOCK_APPLICATIONS),
      });
    }
  });

  // SSE streaming endpoint
  await page.route("**/underwriting/**/ai-summary/stream", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
      },
      body: MOCK_SSE_RESPONSE,
    });
  });
}


// ─── Tests ─────────────────────────────────────────────────────────────────────

test.describe("Citation-First HITL Dashboard", () => {

  test.beforeEach(async ({ page }) => {
    await mockAPIRoutes(page);
    await page.goto("/dashboard/underwriting");
  });

  // ── 1. Page loads and shows application queue ─────────────────────────────

  test("renders the application queue with applications", async ({ page }) => {
    // The sidebar queue should show the mock application
    await expect(
      page.getByText("APP-2024-0001")
    ).toBeVisible({ timeout: 5000 });

    // Risk tier badge should be visible
    await expect(page.getByText("SUBSTANDARD")).toBeVisible();
  });

  // ── 2. Selecting an application shows the score card ─────────────────────

  test("clicking an application reveals score card data", async ({ page }) => {
    await page.getByText("APP-2024-0001").click();

    // Score card stats
    await expect(page.getByText("72 pts")).toBeVisible({ timeout: 5000 });
    await expect(page.getByText("Table 2")).toBeVisible();
    await expect(page.getByText("$45.75/mo")).toBeVisible();

    // Exclusion rider
    await expect(page.getByText("Heart failure (I50)")).toBeVisible();

    // Manual review action buttons should appear
    await expect(page.getByRole("button", { name: "Approve & Issue" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Decline" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Postpone" })).toBeVisible();
  });

  // ── 3. GenAI streaming renders tokens progressively ───────────────────────

  test("clicking Analyze starts streaming and renders tokens", async ({ page }) => {
    await page.getByText("APP-2024-0001").click();

    // Click the Analyze button in GenAIAssistant
    const analyzeBtn = page.getByRole("button", { name: "Analyze" });
    await expect(analyzeBtn).toBeEnabled({ timeout: 5000 });
    await analyzeBtn.click();

    // Streamed content should appear
    await expect(
      page.getByText("has Type 2 Diabetes")
    ).toBeVisible({ timeout: 8000 });

    // Citation marker [^1] should be rendered as a badge button
    const citationBadge = page.locator("button[aria-label*='Citation 1']");
    await expect(citationBadge).toBeVisible({ timeout: 5000 });
  });

  // ── 4. CITATION-FIRST: clicking a badge updates the PDF viewer ────────────

  test("clicking citation badge navigates PDF viewer to correct page", async ({ page }) => {
    await page.getByText("APP-2024-0001").click();

    // Start analysis stream
    await page.getByRole("button", { name: "Analyze" }).click();

    // Wait for citation badge to appear
    const citationBadge = page.locator("button[aria-label*='Citation 1']").first();
    await expect(citationBadge).toBeVisible({ timeout: 8000 });

    // Verify badge has tooltip showing document name
    await citationBadge.hover();
    // Tooltip content from the `title` attribute
    const badgeTitle = await citationBadge.getAttribute("title");
    expect(badgeTitle).toContain("Member Benefits Guide 2024.pdf");

    // Click the citation badge
    await citationBadge.click();

    // CitationViewer should show the document name
    await expect(
      page.getByText("Member Benefits Guide 2024.pdf")
    ).toBeVisible({ timeout: 5000 });

    // Page indicator should show "p. 3" (the cited page)
    await expect(page.getByText("p. 3")).toBeVisible({ timeout: 5000 });
  });

  // ── 5. Source list links also trigger PDF navigation ─────────────────────

  test("clicking source list entry navigates PDF viewer", async ({ page }) => {
    await page.getByText("APP-2024-0001").click();
    await page.getByRole("button", { name: "Analyze" }).click();

    // Wait for streaming to complete (source list appears after done event)
    await expect(page.getByText("SOURCES")).toBeVisible({ timeout: 8000 });

    // Click the source entry
    const sourceEntry = page.getByText("Member Benefits Guide 2024.pdf").first();
    await sourceEntry.click();

    // Excerpt tooltip should appear in CitationViewer
    await expect(page.getByText("CITED PASSAGE")).toBeVisible({ timeout: 3000 });
    await expect(
      page.getByText("Coverage for diabetes management")
    ).toBeVisible();
  });

  // ── 6. RBAC: Action bar disabled for non-manual-review routes ─────────────

  test("action buttons do not appear for STP-approved applications", async ({ page }) => {
    // Override decision to return stp_approved route
    await page.route("**/applications/**/decision", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...MOCK_DECISION, route: "stp_approved" }),
      });
    });

    await page.getByText("APP-2024-0001").click();

    // Action buttons should NOT be visible for STP claims
    await expect(
      page.getByRole("button", { name: "Approve & Issue" })
    ).not.toBeVisible({ timeout: 3000 });
  });

  // ── 7. Cancelling stream stops token rendering ────────────────────────────

  test("Stop button aborts the stream", async ({ page }) => {
    await page.getByText("APP-2024-0001").click();
    await page.getByRole("button", { name: "Analyze" }).click();

    // Click Stop during stream
    const stopBtn = page.getByRole("button", { name: "Stop" });
    await expect(stopBtn).toBeVisible({ timeout: 3000 });
    await stopBtn.click();

    // State should return to idle
    await expect(
      page.getByRole("button", { name: "Regenerate" }).or(
        page.getByRole("button", { name: "Analyze" })
      )
    ).toBeVisible({ timeout: 3000 });
  });

  // ── 8. Empty state when no application selected ───────────────────────────

  test("empty state is shown before any application is selected", async ({ page }) => {
    await expect(
      page.getByText("Select an application")
    ).toBeVisible({ timeout: 3000 });

    // No score card or action buttons should be visible
    await expect(
      page.getByRole("button", { name: "Approve & Issue" })
    ).not.toBeVisible();
  });

});
