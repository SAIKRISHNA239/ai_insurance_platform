/**
 * frontend/e2e/underwriting_journey.spec.ts
 * ─────────────────────────────────────────────────────────────────────────────
 * Playwright E2E: Full underwriter journey on the Underwriting dashboard.
 *
 * Covers:
 *   1. Application queue loads with applications
 *   2. Selecting an application reveals score card and decision data
 *   3. Clicking "Analyze" starts AI streaming; tokens render progressively
 *   4. After streaming, citation badges appear inline in the text
 *   5. Clicking a citation badge opens CitationViewer on the correct page
 *   6. "Approve & Issue" button calls the decision endpoint
 *   7. Streaming shows live RAG citations in the SOURCES panel
 *
 * HOW TO RUN
 * ──────────
 *   cd frontend
 *   npx playwright test e2e/underwriting_journey.spec.ts --headed
 *   # CI:
 *   npx playwright test e2e/underwriting_journey.spec.ts
 *
 * MOCKING
 * ────────
 * page.route() intercepts all backend calls. The SSE stream is fulfilled
 * as a single body string containing all frames — Playwright parses it
 * identically to a real streaming response.
 */

import { test, expect, Page } from "@playwright/test";

// ── Fixture data ───────────────────────────────────────────────────────────────

const APP_ID  = "app-uw-e2e-uuid-001";
const APP_NUM = "APP-2024-0001";

const MOCK_APPLICATIONS = {
  total: 1, page: 1, page_size: 20,
  items: [{
    id:                       APP_ID,
    application_number:       APP_NUM,
    policy_type:              "individual_health",
    status:                   "pending_review",
    requested_coverage_limit: "500000.00",
    underwriting_score:       72,
    risk_tier:                "substandard",
    ai_underwriting_notes:    null,
    created_at:               "2024-03-01T10:00:00Z",
  }],
};

const MOCK_DECISION = {
  application_id:    APP_ID,
  route:             "manual_review",
  risk_tier:         "substandard",
  net_score:         72,
  table_rating:      2,
  suggested_premium: "45.75",
  permanent_exclusions: ["Heart failure (I50)"],
  routing_reason:    "Score 72 exceeds STP threshold.",
  ai_assistant_summary: {
    clinical_summary:       "Applicant has Type 2 Diabetes and obesity.",
    key_impairments:        ["Type 2 Diabetes (E11)", "BMI 36 (Obese Class II)"],
    data_discrepancies:     [],
    proposed_decision:      "TABLE_RATING",
    reasoning:              "Substandard but insurable with Table B loading.",
    suggested_requirements: ["Order HbA1c within 90 days"],
  },
};

const MOCK_CITATION = {
  id:            1,
  chunk_id:      "chunk-uw-001",
  document_name: "Member Benefits Guide 2024.pdf",
  document_url:  "/test-fixtures/sample.pdf",
  bounding_box:  { page: 3, x: 0.1, y: 0.2, width: 0.8, height: 0.05 },
  excerpt:       "Coverage for diabetes management includes annual HbA1c testing.",
};

const SSE_STREAM = [
  `data: ${JSON.stringify({ type: "token", content: "The applicant " })}\n\n`,
  `data: ${JSON.stringify({ type: "token", content: "has Type 2 Diabetes " })}\n\n`,
  `data: ${JSON.stringify({ type: "token", content: "[^1] " })}\n\n`,
  `data: ${JSON.stringify({ type: "token", content: "confirmed by MIB records." })}\n\n`,
  `data: ${JSON.stringify({ type: "citations", citations: [MOCK_CITATION] })}\n\n`,
  `data: ${JSON.stringify({ type: "done" })}\n\n`,
].join("");

// ── Shared route mocking ───────────────────────────────────────────────────────

async function mockUWRoutes(page: Page) {
  await page.route("**/applications/**", async (route) => {
    const url = route.request().url();
    if (url.includes("/decision")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_DECISION) });
    } else if (route.request().method() === "PATCH") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...MOCK_APPLICATIONS.items[0], status: "approved" }) });
    } else {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_APPLICATIONS) });
    }
  });

  await page.route("**/underwriting/**/ai-summary/stream", async (route) => {
    await route.fulfill({
      status:      200,
      contentType: "text/event-stream",
      headers:     { "Cache-Control": "no-cache", "X-Accel-Buffering": "no" },
      body:        SSE_STREAM,
    });
  });
}

// ── Tests ──────────────────────────────────────────────────────────────────────

test.describe("Underwriter Journey", () => {

  test.beforeEach(async ({ page }) => {
    await mockUWRoutes(page);
    await page.goto("/dashboard/underwriting");
  });

  // 1 ── Queue loads ────────────────────────────────────────────────────────────

  test("renders the application queue with mock applications", async ({ page }) => {
    await expect(page.getByText(APP_NUM)).toBeVisible({ timeout: 6000 });
    await expect(page.getByText(/substandard/i)).toBeVisible();
  });

  // 2 ── Score card ─────────────────────────────────────────────────────────────

  test("selecting an application shows score card with risk metrics", async ({ page }) => {
    await page.getByText(APP_NUM).click();

    await expect(page.getByText("72 pts")).toBeVisible({ timeout: 6000 });
    await expect(page.getByText("Table 2")).toBeVisible();
    await expect(page.getByText("$45.75/mo")).toBeVisible();
    await expect(page.getByText("Heart failure (I50)")).toBeVisible();

    // Action buttons appear for manual_review route
    await expect(page.getByRole("button", { name: /Approve & Issue/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Decline/i })).toBeVisible();
  });

  // 3 ── AI streaming renders tokens ────────────────────────────────────────────

  test("clicking Analyze streams tokens progressively into the panel", async ({ page }) => {
    await page.getByText(APP_NUM).click();

    const analyzeBtn = page.getByRole("button", { name: /Analyze/i });
    await expect(analyzeBtn).toBeEnabled({ timeout: 5000 });
    await analyzeBtn.click();

    // Streamed text should appear
    await expect(page.getByText("has Type 2 Diabetes")).toBeVisible({ timeout: 8000 });
    await expect(page.getByText("confirmed by MIB records")).toBeVisible({ timeout: 8000 });
  });

  // 4 ── Citation badge appears inline ──────────────────────────────────────────

  test("citation marker [^1] renders as a clickable badge after streaming", async ({ page }) => {
    await page.getByText(APP_NUM).click();
    await page.getByRole("button", { name: /Analyze/i }).click();

    // Wait for the citation badge
    const badge = page.locator("button[aria-label*='Citation 1']").first();
    await expect(badge).toBeVisible({ timeout: 8000 });

    // Badge should have tooltip with document name
    const title = await badge.getAttribute("title");
    expect(title).toContain("Member Benefits Guide 2024.pdf");
  });

  // 5 ── Citation viewer opens on correct page ───────────────────────────────────

  test("clicking a citation badge opens CitationViewer on page 3", async ({ page }) => {
    await page.getByText(APP_NUM).click();
    await page.getByRole("button", { name: /Analyze/i }).click();

    const badge = page.locator("button[aria-label*='Citation 1']").first();
    await expect(badge).toBeVisible({ timeout: 8000 });
    await badge.click();

    // CitationViewer must show the document name
    await expect(page.getByText("Member Benefits Guide 2024.pdf")).toBeVisible({ timeout: 5000 });

    // Page indicator must show page 3 (from bounding_box.page)
    await expect(page.getByText("p. 3")).toBeVisible({ timeout: 5000 });
  });

  // 6 ── SOURCES panel populated after stream ────────────────────────────────────

  test("SOURCES panel shows retrieved citations after streaming completes", async ({ page }) => {
    await page.getByText(APP_NUM).click();
    await page.getByRole("button", { name: /Analyze/i }).click();

    // Wait for streaming done (SOURCES section appears)
    await expect(page.getByText("SOURCES")).toBeVisible({ timeout: 8000 });
    await expect(page.getByText("Member Benefits Guide 2024.pdf")).toBeVisible();
  });

  // 7 ── Approve & Issue ─────────────────────────────────────────────────────────

  test("clicking Approve & Issue calls the decision PATCH endpoint", async ({ page }) => {
    let patchCalled = false;

    // Override to capture PATCH call
    await page.route("**/applications/**", async (route) => {
      if (route.request().method() === "PATCH") {
        patchCalled = true;
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "approved" }) });
      } else if (route.request().url().includes("/decision")) {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_DECISION) });
      } else {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_APPLICATIONS) });
      }
    });

    await page.goto("/dashboard/underwriting");
    await page.getByText(APP_NUM).click();

    const approveBtn = page.getByRole("button", { name: /Approve & Issue/i });
    await expect(approveBtn).toBeVisible({ timeout: 6000 });
    await approveBtn.click();

    // Allow the request to process
    await page.waitForTimeout(1000);
    expect(patchCalled).toBe(true);
  });

  // 8 ── Empty state before selection ───────────────────────────────────────────

  test("empty state shown before any application is selected", async ({ page }) => {
    await expect(page.getByText(/Select an application/i)).toBeVisible({ timeout: 5000 });
    await expect(page.getByRole("button", { name: /Approve & Issue/i })).not.toBeVisible();
  });

  // 9 ── STP auto-approved hides action buttons ──────────────────────────────────

  test("STP-approved applications hide the action bar", async ({ page }) => {
    await page.route("**/applications/**/decision", async (route) => {
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ ...MOCK_DECISION, route: "stp_approved" }),
      });
    });

    await page.goto("/dashboard/underwriting");
    await page.getByText(APP_NUM).click();

    await expect(page.getByRole("button", { name: /Approve & Issue/i })).not.toBeVisible({ timeout: 4000 });
  });

});
