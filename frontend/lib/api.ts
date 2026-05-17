/**
 * lib/api.ts
 * ──────────────────────────────────────────────────────────────────────────────
 * Typed API client for the FastAPI backend.
 *
 * ARCHITECTURE DECISIONS
 * ───────────────────────
 * • API is mounted at /api/v1 — all calls include this prefix.
 * • JWT token is obtained via POST /api/v1/auth/token (OAuth2 password grant).
 * • Token is stored in localStorage for dev; replaced with httpOnly cookie in prod.
 * • Streaming uses fetch ReadableStream (not EventSource) to support auth headers.
 */

// ─── Base Configuration ────────────────────────────────────────────────────────

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const API_V1 = `${API_BASE_URL}/api/v1`;

// ─── Shared Types ──────────────────────────────────────────────────────────────

export interface APIError {
  detail: string | Record<string, unknown>;
  status: number;
}

/**
 * BoundingBox coordinates in PDF page space.
 * All values are fractions of the page dimensions [0.0–1.0].
 */
export interface BoundingBox {
  page: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * A single citation linking an AI-generated claim to a source document.
 */
export interface Citation {
  id: number;
  chunk_id: string;
  document_name: string;
  document_url: string;
  bounding_box: BoundingBox;
  excerpt: string;
}

export interface StreamChunk {
  type: "token" | "citations" | "error" | "done";
  content?: string;
  citations?: Citation[];
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

export type ApplicationStatus =
  | "draft"
  | "submitted"
  | "under_review"
  | "approved"
  | "declined"
  | "withdrawn";

export interface Application {
  id: string;
  application_number: string;
  applicant_id: string;
  policy_type: string;
  status: ApplicationStatus;
  requested_coverage_limit: string;
  underwriting_score: number | null;
  risk_tier: RiskTier | null;
  ai_underwriting_notes: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
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

/**
 * Response from POST /claims/intake (HTTP 202 Accepted).
 * Mirrors backend ClaimIntakeResponse schema.
 */
export interface ClaimIntakeResponse {
  claim_id: string;
  claim_number: string;
  adjudication_state: string;
  snip_status: 'passed' | 'rejected';
  snip_failing_tier: number | null;
  snip_violations: Array<{
    tier: number;
    error_code: string;
    field: string | null;
    message: string;
  }>;
  um_route: string | null;
  um_triggers: string[];
  message: string;
  submitted_at: string;
}

// ─── Auth Types ────────────────────────────────────────────────────────────────

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  role: string;
}

// ─── Auth Helpers ──────────────────────────────────────────────────────────────

export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("access_token");
}

export function setStoredToken(token: string): void {
  if (typeof window !== "undefined") localStorage.setItem("access_token", token);
}

export function clearStoredToken(): void {
  if (typeof window !== "undefined") localStorage.removeItem("access_token");
}

function getAuthHeaders(): HeadersInit {
  const token = getStoredToken();
  return token
    ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" };
}

// ─── Core Fetch Wrapper ────────────────────────────────────────────────────────

async function apiFetch<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const response = await fetch(`${API_V1}${path}`, {
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

// ─── Auth API ─────────────────────────────────────────────────────────────────

export const authAPI = {
  /**
   * Login with email + password. Stores the JWT in localStorage automatically.
   */
  login: async (email: string, password: string): Promise<TokenResponse> => {
    const body = new URLSearchParams({ username: email, password });
    const response = await fetch(`${API_V1}/auth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: "Login failed" }));
      throw { detail: err.detail, status: response.status } as APIError;
    }
    const data: TokenResponse = await response.json();
    setStoredToken(data.access_token);
    return data;
  },

  logout: () => clearStoredToken(),

  register: (payload: {
    email: string;
    password: string;
    full_name: string;
    role?: string;
  }): Promise<unknown> =>
    apiFetch("/auth/register", { method: "POST", body: JSON.stringify(payload) }),
};

// ─── Marketplace API (Simulated) ────────────────────────────────────────────────

export interface MarketplacePlan {
  id: string;
  name: string;
  badge: string;
  badgeClass: string;
  badgeDot: string | null;
  badgeIcon: string | null;
  price: string;
  priceColor: string;
  features: string[];
  checkColor: string;
  featured: boolean;
}

export const marketplaceAPI = {
  listPlans: async (): Promise<MarketplacePlan[]> => {
    // Simulate a network request to fetch available plans
    return new Promise((resolve) => {
      setTimeout(() => {
        resolve([
          {
            id: "plan-1",
            name: 'Care Elite',
            badge: 'Comprehensive',
            badgeClass: 'bg-secondary-container/20 text-secondary border-secondary/30',
            badgeDot: 'bg-secondary',
            badgeIcon: null,
            price: '$850',
            priceColor: 'text-primary',
            features: ['Full catastrophic risk coverage', 'No deductible for chronic care', 'Premium network access'],
            checkColor: 'text-secondary',
            featured: false,
          },
          {
            id: "plan-2",
            name: 'Health Max',
            badge: 'AI Recommended',
            badgeClass: 'bg-primary-container/20 text-primary border-primary/30',
            badgeDot: null,
            badgeIcon: 'auto_awesome',
            price: '$620',
            priceColor: 'text-primary',
            features: ['Optimized for chronic conditions (Asthma)', 'High-tier pharmacy benefits', 'Telehealth specialist network'],
            checkColor: 'text-primary',
            featured: true,
          },
          {
            id: "plan-3",
            name: 'Base Shield',
            badge: 'Essential',
            badgeClass: 'bg-surface-variant text-on-surface-variant border-outline-variant',
            badgeDot: null,
            badgeIcon: null,
            price: '$340',
            priceColor: 'text-on-surface-variant',
            features: ['Standard preventive care', 'High deductible model', 'Local network only'],
            checkColor: 'text-outline',
            featured: false,
          },
        ]);
      }, 800); // 800ms artificial delay for smooth loading animation
    });
  }
};

// ─── Applications API ──────────────────────────────────────────────────────────

export const applicationsAPI = {
  list: (page = 1, pageSize = 20): Promise<PaginatedResponse<Application>> =>
    apiFetch(`/applications/?page=${page}&page_size=${pageSize}`),

  get: (applicationId: string): Promise<Application> =>
    apiFetch(`/applications/${applicationId}`),

  /**
   * NOTE: The backend currently has PATCH /applications/{id}/underwrite for
   * posting a decision. The "getDecision" endpoint is a frontend convenience
   * that reads the underwriting fields from the Application object directly,
   * since there is no separate /decision endpoint in the current backend.
   * This method reconstructs a UnderwritingDecision from the Application data.
   */
  getDecision: async (applicationId: string): Promise<UnderwritingDecision | null> => {
    try {
      const app = await apiFetch<Application>(`/applications/${applicationId}`);
      if (!app.underwriting_score || !app.risk_tier) return null;

      // Map backend fields to the UnderwritingDecision shape
      const score = app.underwriting_score ?? 0;
      const route: UnderwritingRoute =
        score < 50
          ? "stp_approved"
          : score < 80
          ? "conditional_approved"
          : "manual_review";

      return {
        application_id: app.id,
        route,
        risk_tier: app.risk_tier,
        net_score: Math.round(score),
        table_rating: score >= 80 ? 4 : score >= 60 ? 2 : 0,
        suggested_premium: null,
        permanent_exclusions: [],
        routing_reason: app.ai_underwriting_notes ?? "Score-based automatic routing.",
        ai_assistant_summary: app.ai_underwriting_notes
          ? {
              clinical_summary: app.ai_underwriting_notes,
              key_impairments: [],
              data_discrepancies: [],
              proposed_decision: route === "stp_approved" ? "APPROVE" : route === "manual_review" ? "TABLE_RATING" : "POSTPONE",
              reasoning: app.ai_underwriting_notes,
              suggested_requirements: [],
            }
          : null,
      };
    } catch {
      return null;
    }
  },

  submitDecision: (
    applicationId: string,
    payload: {
      status: ApplicationStatus;
      underwriting_score?: number;
      risk_tier?: RiskTier;
      suggested_premium?: string;
      ai_underwriting_notes?: string;
      decision_notes?: string;
    }
  ): Promise<Application> =>
    apiFetch(`/applications/${applicationId}/underwrite`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  /**
   * Submit a new insurance application — POST /applications/.
   * Maps to the backend ApplicationSubmitRequest schema.
   */
  submit: (payload: {
    application_number: string;
    policy_type: string;
    requested_coverage_limit: string;
    health_questionnaire?: Record<string, boolean | string | number>;
  }): Promise<Application> =>
    apiFetch('/applications/', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
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

  /**
   * EDA claim intake — POST /claims/intake.
   * Accepts a pre-parsed EDI 837 JSON payload.
   * Returns HTTP 202 ClaimIntakeResponse on success.
   * Throws APIError with structured SNIP violation details on HTTP 422.
   */
  intakeClaim: (payload: Record<string, unknown>): Promise<ClaimIntakeResponse> =>
    apiFetch('/claims/intake', { method: 'POST', body: JSON.stringify(payload) }),

  /**
   * File upload intake — POST /claims/upload.
   * Accepts a PDF, PNG, or JPG file as multipart/form-data.
   * The backend performs mock EDI 837 extraction then runs the full SNIP pipeline.
   * Returns HTTP 202 ClaimIntakeResponse on success.
   * NOTE: Do NOT set Content-Type header — the browser sets the multipart boundary.
   */
  uploadFile: (file: File): Promise<ClaimIntakeResponse> => {
    const formData = new FormData();
    formData.append('file', file);
    const token = getStoredToken();
    return fetch(`${API_V1}/claims/upload`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: formData,
    }).then(async (res) => {
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: 'Upload failed' }));
        throw { detail: body.detail ?? body, status: res.status } as APIError;
      }
      return res.json() as Promise<ClaimIntakeResponse>;
    });
  },

  updateStatus: (
    claimId: string,
    status: ClaimStatus,
    denial_reason?: string
  ): Promise<Claim> =>
    apiFetch(`/claims/${claimId}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status, denial_reason }),
    }),
};

// ─── Streaming Underwriting Assistant ─────────────────────────────────────────

/**
 * Streams the GenAI Underwriting Assistant response.
 * FastAPI sends newline-delimited JSON chunks via StreamingResponse.
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
        `${API_V1}/underwriting/${applicationId}/ai-summary/stream`,
        {
          method: "GET",
          headers: getAuthHeaders() as Record<string, string>,
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

// ─── Dashboard Stats ───────────────────────────────────────────────────────────

export interface DashboardStats {
  totalClaims: number;
  totalApplications: number;
  approvedClaims: number;
  pendingReview: number;
}

/**
 * Backend aggregate shape from GET /system/stats.
 * Uses SQL COUNT() — no full rows are fetched.
 */
interface SystemStatsResponse {
  total_claims: number;
  total_applications: number;
  approved_claims: number;
  pending_applications: number;
}

/**
 * Fetches dashboard KPI stats from the high-performance
 * GET /system/stats endpoint (SQL COUNT aggregates, no full rows).
 */
export async function getDashboardStats(): Promise<DashboardStats> {
  const raw = await apiFetch<SystemStatsResponse>('/system/stats');
  return {
    totalClaims:       raw.total_claims,
    totalApplications: raw.total_applications,
    approvedClaims:    raw.approved_claims,
    pendingReview:     raw.pending_applications,
  };
}


// --- Policies API ---

export type PolicyStatus = 'pending' | 'active' | 'lapsed' | 'cancelled' | 'expired';
export type PolicyType   = 'individual' | 'group' | 'medicare_supplement' | 'dental' | 'vision';

export interface Policy {
  id: string;
  policy_number: string;
  holder_id: string;
  policy_type: PolicyType;
  premium_amount: string;
  coverage_limit: string;
  deductible: string;
  effective_date: string;
  expiry_date: string;
  status: PolicyStatus;
  created_at: string;
}

export interface PolicyCreateRequest {
  policy_number: string;
  holder_id: string;
  policy_type: PolicyType;
  premium_amount: string;
  coverage_limit: string;
  deductible?: string;
  out_of_pocket_max?: string;
  effective_date: string;
  expiry_date: string;
  benefits_schedule?: Record<string, unknown>;
}

export const policiesAPI = {
  list: (page = 1, policyStatus?: PolicyStatus): Promise<PaginatedResponse<Policy>> => {
    const params = new URLSearchParams({ page: String(page) });
    if (policyStatus) params.set('status', policyStatus);
    return apiFetch(`/policies/?${params}`);
  },

  get: (policyId: string): Promise<Policy> =>
    apiFetch(`/policies/${policyId}`),

  create: (payload: PolicyCreateRequest): Promise<Policy> =>
    apiFetch('/policies/', { method: 'POST', body: JSON.stringify(payload) }),
};

// ─── Knowledge Base API ────────────────────────────────────────────────────────

export interface KnowledgeUploadResponse {
  document_id: string;
  filename: string;
  message: string;
  status: string;
}

export interface EvaluationAccepted {
  run_id: string;
  message: string;
  status_url: string;
}

export interface EvaluationStatus {
  run_id: string;
  state: 'queued' | 'running' | 'complete' | 'failed';
  started_at: string;
  completed_at: string | null;
  scores: {
    faithfulness: number;
    context_precision: number;
    context_recall: number;
    answer_relevancy: number;
  } | null;
  error: string | null;
}

export const knowledgeAPI = {
  /**
   * Upload a PDF into the RAG knowledge base.
   * Returns 202 Accepted — ingestion runs as a background task.
   */
  uploadDocument: (file: File): Promise<KnowledgeUploadResponse> => {
    const form = new FormData();
    form.append('file', file);
    return apiFetch('/knowledge/upload', {
      method: 'POST',
      body: form,
      // Do NOT set Content-Type — browser sets multipart boundary automatically
    });
  },
};

// ─── MLOps API ────────────────────────────────────────────────────────────────

export const mlopsAPI = {
  /**
   * Trigger a background RAGAS evaluation run.
   * Returns 202 with a run_id to poll status.
   */
  triggerEvaluation: (
    collectionName = 'policy_vectors',
    modelName = 'text-embedding-004',
  ): Promise<EvaluationAccepted> =>
    apiFetch('/mlops/evaluate', {
      method: 'POST',
      body: JSON.stringify({ collection_name: collectionName, model_name: modelName }),
    }),

  /**
   * Poll evaluation status by run_id.
   */
  getEvaluationStatus: (runId: string): Promise<EvaluationStatus> =>
    apiFetch(`/mlops/evaluate/status/${runId}`),
};

