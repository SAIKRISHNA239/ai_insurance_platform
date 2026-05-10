/**
 * lib/api.ts
 * ──────────────────────────────────────────────────────────────────────────────
 * Typed API client for the FastAPI backend.
 *
 * ARCHITECTURE DECISIONS
 * ───────────────────────
 * • All non-streaming calls use the native `fetch` API (available in Next.js
 *   App Router server and client components) — no extra Axios dependency.
 * • The `streamUnderwritingAssistant` function uses the EventSource / fetch
 *   ReadableStream pattern to consume SSE from FastAPI's StreamingResponse.
 * • All API types are co-located here so components import one source of truth.
 * • The `getAuthHeaders` function reads the JWT from a cookie (httpOnly) set
 *   by the FastAPI /auth/token endpoint.
 */

// ─── Base Configuration ────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ─── Shared Types ──────────────────────────────────────────────────────────────

export interface APIError {
  detail: string | Record<string, unknown>;
  status: number;
}

/**
 * BoundingBox coordinates in PDF page space.
 * Origin (0,0) is top-left of the page.
 * All values are fractions of the page dimensions [0.0–1.0] for
 * resolution-independent rendering in the CitationViewer.
 */
export interface BoundingBox {
  page: number;      // 1-indexed PDF page number
  x: number;        // Left edge [0–1]
  y: number;        // Top edge [0–1]
  width: number;    // Box width [0–1]
  height: number;   // Box height [0–1]
}

/**
 * A single citation linking an AI-generated claim to a source document.
 * The backend embeds citation markers as `[^1]`, `[^2]` etc. in the
 * LLM response text. This object provides the PDF metadata for rendering.
 */
export interface Citation {
  id: number;              // Matches the [^id] marker in the response text
  chunk_id: string;        // Vector DB chunk ID for traceability
  document_name: string;   // Human-readable document title
  document_url: string;    // URL to the source PDF served by backend
  bounding_box: BoundingBox;
  excerpt: string;         // The exact text passage the AI cited
}

export interface StreamChunk {
  type: "token" | "citations" | "error" | "done";
  content?: string;        // Text token (type === "token")
  citations?: Citation[];  // Final citation list (type === "citations")
  error?: string;
}

// ─── Application & Underwriting Types ─────────────────────────────────────────

export type RiskTier = "preferred" | "standard" | "substandard" | "decline";
export type UnderwritingRoute =
  | "stp_approved"
  | "conditional_approved"
  | "manual_review";

export interface UnderwritingDecision {
  application_id: string;
  route: UnderwritingRoute;
  risk_tier: RiskTier;
  net_score: number;
  table_rating: number;
  suggested_premium: string | null;
  permanent_exclusions: string[];
  routing_reason: string;
  ai_assistant_summary: {
    clinical_summary: string;
    key_impairments: string[];
    data_discrepancies: string[];
    proposed_decision: string;
    reasoning: string;
    suggested_requirements: string[];
  } | null;
}

export interface Application {
  id: string;
  application_number: string;
  policy_type: string;
  status: string;
  requested_coverage_limit: string;
  underwriting_score: number | null;
  risk_tier: RiskTier | null;
  ai_underwriting_notes: string | null;
  created_at: string;
}

// ─── Claims Types ──────────────────────────────────────────────────────────────

export type ClaimStatus =
  | "submitted" | "in_review" | "pending_info"
  | "approved" | "partially_approved" | "denied"
  | "appealed" | "closed";

export interface Claim {
  id: string;
  claim_number: string;
  policy_id: string;
  status: ClaimStatus;
  billed_amount: string;
  allowed_amount: string | null;
  paid_amount: string | null;
  service_date_start: string;
  fraud_score: number | null;
  ai_notes: string | null;
  created_at: string;
}

export interface PaginatedResponse<T> {
  total: number;
  page: number;
  page_size: number;
  items: T[];
}

// ─── Auth ──────────────────────────────────────────────────────────────────────

function getAuthHeaders(): HeadersInit {
  // In production, the JWT is stored in an httpOnly cookie managed by the
  // Next.js middleware (middleware.ts) and forwarded to FastAPI.
  // For dev, we read from localStorage as a fallback.
  const token =
    typeof window !== "undefined"
      ? localStorage.getItem("access_token")
      : null;
  return token
    ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" };
}

// ─── Core Fetch Wrapper ────────────────────────────────────────────────────────

async function apiFetch<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: { ...getAuthHeaders(), ...options?.headers },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: "Unknown error" }));
    const error: APIError = { detail: body.detail ?? body, status: response.status };
    throw error;
  }

  return response.json() as Promise<T>;
}

// ─── Applications API ──────────────────────────────────────────────────────────

export const applicationsAPI = {
  list: (page = 1, pageSize = 20): Promise<PaginatedResponse<Application>> =>
    apiFetch(`/applications/?page=${page}&page_size=${pageSize}`),

  get: (applicationId: string): Promise<Application> =>
    apiFetch(`/applications/${applicationId}`),

  getDecision: (applicationId: string): Promise<UnderwritingDecision> =>
    apiFetch(`/applications/${applicationId}/decision`),
};

// ─── Claims API ────────────────────────────────────────────────────────────────

export const claimsAPI = {
  list: (page = 1, status?: string): Promise<PaginatedResponse<Claim>> => {
    const params = new URLSearchParams({ page: String(page) });
    if (status) params.set("status", status);
    return apiFetch(`/claims/?${params}`);
  },

  get: (claimId: string): Promise<Claim> =>
    apiFetch(`/claims/${claimId}`),

  intake: (payload: Record<string, unknown>): Promise<{ claim_id: string; adjudication_state: string }> =>
    apiFetch("/claims/intake", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};

// ─── Streaming Underwriting Assistant ─────────────────────────────────────────

/**
 * Streams the GenAI Underwriting Assistant response using the Fetch
 * ReadableStream API (SSE-compatible).
 *
 * FastAPI sends newline-delimited JSON chunks, each matching the
 * `StreamChunk` interface. The caller provides two callbacks:
 *   onToken:     Called for each text token to update the UI progressively.
 *   onCitations: Called once at the end with the full citation list.
 *   onError:     Called on stream error.
 *   onDone:      Called when the stream completes cleanly.
 *
 * WHY NOT `EventSource`?
 * The native EventSource API does not support POST requests or custom
 * Authorization headers — both required here. We use fetch() with a
 * ReadableStream reader instead, which is fully compatible with FastAPI's
 * `StreamingResponse` using `media_type="text/event-stream"`.
 *
 * @param applicationId  The underwriting application to analyze.
 * @param callbacks      Event handlers for each stream chunk type.
 * @returns              AbortController.signal to cancel the stream.
 */
export function streamUnderwritingAssistant(
  applicationId: string,
  callbacks: {
    onToken: (token: string) => void;
    onCitations: (citations: Citation[]) => void;
    onError: (error: string) => void;
    onDone: () => void;
  }
): AbortController {
  const controller = new AbortController();

  (async () => {
    try {
      const response = await fetch(
        `${API_BASE_URL}/underwriting/${applicationId}/ai-summary/stream`,
        {
          method: "GET",
          headers: getAuthHeaders(),
          signal: controller.signal,
        }
      );

      if (!response.ok || !response.body) {
        callbacks.onError(`HTTP ${response.status}: Failed to start stream`);
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE format: "data: {...}\n\n"
        const lines = buffer.split("\n\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          const dataLine = line.replace(/^data:\s*/, "");
          if (!dataLine) continue;

          try {
            const chunk: StreamChunk = JSON.parse(dataLine);
            if (chunk.type === "token" && chunk.content) {
              callbacks.onToken(chunk.content);
            } else if (chunk.type === "citations" && chunk.citations) {
              callbacks.onCitations(chunk.citations);
            } else if (chunk.type === "error") {
              callbacks.onError(chunk.error ?? "Unknown stream error");
            } else if (chunk.type === "done") {
              callbacks.onDone();
            }
          } catch {
            // Malformed JSON chunk — skip silently
          }
        }
      }
      callbacks.onDone();
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      callbacks.onError(String(err));
    }
  })();

  return controller;
}
