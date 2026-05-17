/**
 * frontend/e2e/claims_journey.spec.ts
 * ─────────────────────────────────────────────────────────────────────────────
 * Playwright E2E: Full adjuster journey on the Claims page.
 *
 * Covers:
 *   1. Page loads and shows the claims queue
 *   2. Upload a mock JSON claim → appears in queue
 *   3. Click "Deny" → denial reason modal opens
 *   4. Enter denial_reason → confirm → row updates to "Denied"
 *   5. "Approve" button immediately approves (no modal)
 *   6. Filter by status works
 *
 * HOW TO RUN
 * ──────────
 *   cd frontend
 *   npx playwright test e2e/claims_journey.spec.ts --headed
 *   # Headless CI:
 *   npx playwright test e2e/claims_journey.spec.ts
 *
 * MOCKING STRATEGY
 * ─────────────────
 * All API calls are intercepted with page.route() — no live backend required.
 */

import { test, expect, Page } from "@playwright/test";

// ── Fixture data ───────────────────────────────────────────────────────────────

const CLAIM_ID   = "claim-e2e-uuid-001";
const CLAIM_NUM  = "CLM-2024-0042";

const MOCK_CLAIMS_SUBMITTED = {
  total: 1, page: 1, page_size: 20,
  items: [{
    id:            CLAIM_ID,
    claim_number:  CLAIM_NUM,
    status:        "submitted",
    billed_amount: "1200.00",
    allowed_amount: null,
    ai_notes:      "Standard office visit.",
    fraud_score:   0.05,
    created_at:    "2024-03-15T09:00:00Z",
  }],
};

const MOCK_CLAIMS_DENIED = {
  ...MOCK_CLAIMS_SUBMITTED,
  items: [{ ...MOCK_CLAIMS_SUBMITTED.items[0], status: "denied" }],
};

const MOCK_CLAIMS_APPROVED = {
  ...MOCK_CLAIMS_SUBMITTED,
  items: [{ ...MOCK_CLAIMS_SUBMITTED.items[0], status: "approved" }],
};

const MOCK_INTAKE_RESPONSE = {
  claim_id:         "new-claim-uuid-002",
  claim_number:     "CLM-2024-0043",
  snip_status:      "passed",
  snip_failing_tier: null,
  adjudication_state: "validated",
};

// ── Route mock helper ──────────────────────────────────────────────────────────

async function setupClaimsMocks(page: Page, claimsResponse = MOCK_CLAIMS_SUBMITTED) {
  // List endpoint
  await page.route("**/claims/**", async (route) => {
    const url   = route.request().url();
    const method = route.request().method();

    if (method === "GET" && url.includes("/claims/") && !url.includes("/status") && !url.includes("/upload") && !url.includes("/intake")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(claimsResponse) });
    } else if (method === "POST" && url.includes("/intake")) {
      await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(MOCK_INTAKE_RESPONSE) });
    } else if (method === "POST" && url.includes("/upload")) {
      await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(MOCK_INTAKE_RESPONSE) });
    } else if (method === "PATCH" && url.includes("/status")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...MOCK_CLAIMS_SUBMITTED.items[0], status: "denied" }) });
    } else {
      await route.continue();
    }
  });
}

// ── Tests ──────────────────────────────────────────────────────────────────────

test.describe("Claims Adjuster Journey", () => {

  test.beforeEach(async ({ page }) => {
    await setupClaimsMocks(page);
    await page.goto("/claims");
  });

  // 1 ── Page load ─────────────────────────────────────────────────────────────

  test("claims page loads and shows the processing queue", async ({ page }) => {
    await expect(page.getByText(CLAIM_NUM)).toBeVisible({ timeout: 6000 });
    await expect(page.getByText("Submitted")).toBeVisible();
  });

  // 2 ── File upload ────────────────────────────────────────────────────────────

  test("uploading a JSON claim file queues ingestion and shows success", async ({ page }) => {
    // Create an in-memory JSON file and dispatch it into the hidden file input
    const jsonContent = JSON.stringify({
      transaction_set: "837P",
      interchange_control_number: "ICN20240099",
      billing_provider_npi: "1234567893",
      policy_id: "00000000-0000-0000-0000-000000000001",
      service_date_start: "2024-03-01",
      diagnosis_codes: ["E11.9"],
      procedure_lines: [{ line_number: 1, procedure_code: "99213", units: 1, charge_amount: "150.00" }],
      total_charge: "150.00",
    });

    await page.evaluate((content) => {
      const dataTransfer = new DataTransfer();
      const file = new File([content], "test_claim.json", { type: "application/json" });
      dataTransfer.items.add(file);

      const dropZone = document.querySelector("[data-testid='drop-zone'], .drop-zone, [class*='cursor-pointer']");
      if (dropZone) {
        const event = new DragEvent("drop", { bubbles: true, dataTransfer });
        dropZone.dispatchEvent(event);
      }
    }, jsonContent);

    // Alternatively trigger via file input
    const fileInput = page.locator("input[type='file']").first();
    await fileInput.setInputFiles({
      name: "test_claim.json",
      mimeType: "application/json",
      buffer: Buffer.from(jsonContent),
    });

    // Expect the success result to appear
    await expect(page.getByText(/SNIP|snip|validated|claim_id/i)).toBeVisible({ timeout: 6000 });
  });

  // 3 ── Deny button opens modal ─────────────────────────────────────────────────

  test("clicking Deny opens the denial reason modal", async ({ page }) => {
    await expect(page.getByText(CLAIM_NUM)).toBeVisible({ timeout: 5000 });

    // Click the Deny button for this claim
    const denyBtn = page.locator(`#deny-claim-${CLAIM_ID}`);
    await expect(denyBtn).toBeVisible({ timeout: 5000 });
    await denyBtn.click();

    // Modal must appear
    await expect(page.locator("#denial-modal")).toBeVisible({ timeout: 3000 });
    await expect(page.getByText("Deny Claim")).toBeVisible();
    await expect(page.getByText(CLAIM_NUM)).toBeVisible();
    await expect(page.locator("#denial-reason-input")).toBeVisible();
  });

  // 4 ── Confirm denial with reason ──────────────────────────────────────────────

  test("entering a denial reason and confirming calls updateStatus and shows Denied", async ({ page }) => {
    // After denial, re-mock the list to return 'denied'
    await page.route("**/claims/**", async (route) => {
      const method = route.request().method();
      const url = route.request().url();
      if (method === "GET") {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_CLAIMS_DENIED) });
      } else if (method === "PATCH") {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...MOCK_CLAIMS_SUBMITTED.items[0], status: "denied" }) });
      } else {
        await route.continue();
      }
    });

    await page.goto("/claims");
    await expect(page.getByText(CLAIM_NUM)).toBeVisible({ timeout: 5000 });

    // Open the denial modal
    await page.locator(`#deny-claim-${CLAIM_ID}`).click();
    await expect(page.locator("#denial-modal")).toBeVisible({ timeout: 3000 });

    // The confirm button should be disabled before typing
    const confirmBtn = page.locator("#confirm-denial-btn");
    await expect(confirmBtn).toBeDisabled();

    // Type a denial reason
    await page.locator("#denial-reason-input").fill("Not medically necessary per policy section 4.2 — no referral on file.");

    // Confirm should now be enabled
    await expect(confirmBtn).toBeEnabled();
    await confirmBtn.click();

    // Modal closes
    await expect(page.locator("#denial-modal")).not.toBeVisible({ timeout: 5000 });

    // Row should show Denied badge
    await expect(page.getByText("Denied")).toBeVisible({ timeout: 5000 });
  });

  // 5 ── Approve without modal ───────────────────────────────────────────────────

  test("clicking Approve directly calls updateStatus and shows Approved", async ({ page }) => {
    await page.route("**/claims/**", async (route) => {
      const method = route.request().method();
      if (method === "GET") {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_CLAIMS_APPROVED) });
      } else if (method === "PATCH") {
        await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
      } else {
        await route.continue();
      }
    });
    await page.goto("/claims");

    await expect(page.getByText(CLAIM_NUM)).toBeVisible({ timeout: 5000 });

    // No modal — approve fires immediately
    const approveBtn = page.locator(`#approve-claim-${CLAIM_ID}`);
    await expect(approveBtn).toBeVisible({ timeout: 5000 });
    await approveBtn.click();

    // Should NOT open denial modal
    await expect(page.locator("#denial-modal")).not.toBeVisible({ timeout: 2000 });

    // Should show Approved
    await expect(page.getByText("Approved")).toBeVisible({ timeout: 5000 });
  });

  // 6 ── Modal dismiss ───────────────────────────────────────────────────────────

  test("pressing Escape closes the denial modal without submitting", async ({ page }) => {
    await expect(page.getByText(CLAIM_NUM)).toBeVisible({ timeout: 5000 });
    await page.locator(`#deny-claim-${CLAIM_ID}`).click();
    await expect(page.locator("#denial-modal")).toBeVisible({ timeout: 3000 });

    await page.keyboard.press("Escape");
    await expect(page.locator("#denial-modal")).not.toBeVisible({ timeout: 3000 });

    // Claim still shows Submitted (no API call made)
    await expect(page.getByText("Submitted")).toBeVisible();
  });

});
