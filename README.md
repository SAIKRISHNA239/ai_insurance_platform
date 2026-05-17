# MedIntelligence — AI Healthcare Insurance Intelligence Platform

> **An enterprise-grade, full-stack AI insurance platform** — combining a FastAPI backend, Next.js 14 frontend, PostgreSQL, ChromaDB vector store, Kafka-driven claims pipeline, and live Google Gemini RAG streaming for real-time medical underwriting analysis.

---

## ✨ Feature Overview

| Module | Capability |
|---|---|
| **Overview Dashboard** | Real-time KPI cards (claims, applications, approval rate), recent activity feed |
| **Claims Center** | Drag-and-drop JSON/PDF ingestion, EDI 837P SNIP validation, Kafka pipeline, adjudication with denial-reason modal |
| **Application Pipeline** | Visual stepper tracking submission → review → UW decision → closed |
| **Underwriting Desk** | HITL split-screen workspace — RAG-powered Gemini streaming + inline citation badges + PDF viewer |
| **Knowledge Base** | Admin PDF upload → automatic chunk/embed → RAG vector store ingestion |
| **MLOps Dashboard** | Trigger RAGAS evaluation runs, live-poll faithfulness/precision/recall/relevancy scores |
| **Policies** | Policy CRUD, status filtering, tenant-scoped RBAC |
| **Marketplace** | Policy comparison grid with AI recommendation assistant |
| **Settings & Support** | Account info, platform config, FAQ documentation |
| **Auth** | JWT OAuth2 password flow, role-based access (Admin / Underwriter / Claims Adjuster / Insured) |

---

## 🏗️ Architecture

```
ai_insurance_platform/
├── backend/                          # FastAPI Python backend
│   ├── api/
│   │   ├── deps.py                   # Shared FastAPI dependencies (get_db, get_current_user, require_role)
│   │   └── routers/
│   │       ├── auth.py               # POST /auth/token, /auth/register
│   │       ├── applications.py       # CRUD + PATCH underwriting decisions
│   │       ├── claims.py             # EDI 837P intake, SNIP validation, Kafka publish, PATCH status
│   │       ├── policies.py           # Policy management (create, list, get)
│   │       ├── underwriting.py       # ✨ RAG-integrated Gemini SSE streaming endpoint
│   │       ├── knowledge.py          # ✨ POST /knowledge/upload — PDF → chunk → embed → ChromaDB
│   │       ├── mlops.py              # ✨ POST /mlops/evaluate — RAGAS background evaluation
│   │       └── system.py             # System health aggregates
│   ├── claims/
│   │   └── snip_validator.py         # 5-tier EDI 837P SNIP validation engine
│   ├── kafka/
│   │   └── producer.py               # AIOKafkaProducer (+ MockKafkaProducer for dev)
│   ├── llm/
│   │   └── client.py                 # GeminiClient (google-genai v2), OpenAIClient
│   ├── rag/
│   │   ├── ingestion.py              # PDF parse (unstructured hi_res) + LLM table enrichment
│   │   ├── chunking.py               # Semantic chunking with overlap
│   │   ├── retriever.py              # Hybrid search (Dense + BM25), RRF fusion, cross-encoder re-ranking
│   │   ├── orchestrator.py           # Full ingestion pipeline orchestration
│   │   └── schemas.py                # Typed DTOs for parsed elements and chunks
│   ├── vectorstore/
│   │   └── client.py                 # ChromaDB hybrid_search, upsert_chunk with RBAC filters
│   ├── mlops/
│   │   ├── evaluation.py             # RAGAS evaluation + 5-app gold-standard batch pytest
│   │   └── red_team.py               # Adversarial red teaming + deterministic injection pytest
│   ├── middleware/
│   │   └── auth.py                   # JWT + RBAC middleware
│   ├── database/
│   │   ├── models.py                 # SQLAlchemy async ORM (User, Policy, Claim, Application)
│   │   ├── base.py                   # Async engine setup
│   │   └── vector_client.py          # ChromaDB client factory
│   ├── config.py                     # Pydantic Settings (reads .env)
│   └── main.py                       # FastAPI app factory + lifespan + router registration
│
├── frontend/                         # Next.js 14 App Router frontend
│   ├── app/
│   │   ├── layout.tsx                # Root layout with AuthProvider + AuthShell
│   │   ├── login/page.tsx            # Login page with dev quick-fill
│   │   ├── overview/page.tsx         # Dashboard KPIs
│   │   ├── claims/page.tsx           # ✨ Claims queue + adjudication + denial reason modal
│   │   ├── applications/
│   │   │   ├── page.tsx              # Application pipeline
│   │   │   └── new/page.tsx          # New application submission form
│   │   ├── underwriting/page.tsx     # HITL underwriting desk (alias → /dashboard/underwriting)
│   │   ├── dashboard/underwriting/   # Primary underwriting desk route
│   │   ├── knowledge/page.tsx        # ✨ Admin knowledge base PDF uploader + MLOps eval panel
│   │   ├── policies/page.tsx         # Policy management
│   │   ├── marketplace/page.tsx      # Policy marketplace
│   │   ├── settings/page.tsx         # Platform settings
│   │   └── support/page.tsx          # FAQ & documentation
│   ├── components/
│   │   ├── layout/
│   │   │   ├── Sidebar.tsx           # Global navigation
│   │   │   ├── TopHeader.tsx         # Top bar with user info & logout
│   │   │   └── AuthShell.tsx         # Route guard + shell renderer
│   │   ├── GenAIAssistant.tsx        # Gemini streaming chat with citation marker rendering
│   │   └── ui/CitationViewer.tsx     # PDF citation viewer with page navigation
│   ├── e2e/                          # ✨ Playwright E2E test suites
│   │   ├── citation_ui.spec.ts       # Citation-first HITL underwriting (8 tests)
│   │   ├── claims_journey.spec.ts    # Full adjuster claims journey (6 tests)
│   │   └── underwriting_journey.spec.ts # Full underwriter AI journey (9 tests)
│   └── lib/
│       ├── api.ts                    # ✨ Typed API client — all endpoints + knowledgeAPI + mlopsAPI
│       └── AuthContext.tsx           # Global auth state (SSR-safe)
│
├── backend/tests/                    # ✨ Pytest integration tests
│   ├── test_claims_api.py            # Claims upload (multipart) + PATCH status (12 tests)
│   ├── test_underwriting.py          # Gemini SSE streaming + RAG citation mocking (10 tests)
│   └── test_policies.py              # Policies CRUD + RBAC (12 tests)
│
├── load_testing/
│   └── locustfile.py                 # ✨ Locust: ClaimSubmitter + UnderwritingStreamUser + KnowledgeUploadUser
│
├── seed.py                           # Database seeder with test users, applications, claims
├── docker-compose.yml                # PostgreSQL + ChromaDB
├── docker-compose.observability.yml  # Prometheus + Grafana observability stack
├── .env                              # Environment variables (see below)
└── pyrightconfig.json                # Pyright/Pylance config
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- Docker (for PostgreSQL & ChromaDB)
- Google Gemini API Key

### 1. Clone & set up Python environment

```bash
git clone <repo-url>
cd ai_insurance_platform

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env   # then edit .env
```

Key variables:

```env
# Database
DATABASE_URL=postgresql+asyncpg://insurance_user:insurance_pass@localhost:5433/insurance_db

# Gemini AI (required for live streaming + RAG embedding)
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.0-flash
LLM_PROVIDER=gemini

# ChromaDB
CHROMA_HOST=localhost
CHROMA_PORT=8001
CHROMA_COLLECTION_POLICIES=policy_vectors

# JWT
SECRET_KEY=your_random_32byte_hex_secret
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60
```

### 3. Start infrastructure (Docker)

```bash
docker-compose up -d
```

Or manually:

```bash
# PostgreSQL
docker run -d --name insurance_postgres \
  -e POSTGRES_USER=insurance_user \
  -e POSTGRES_PASSWORD=insurance_pass \
  -e POSTGRES_DB=insurance_db \
  -p 5433:5432 postgres:15-alpine

# ChromaDB
docker run -d --name insurance_chromadb \
  -p 8001:8000 chromadb/chroma:latest
```

### 4. Start the backend

```bash
source .venv/bin/activate
python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

### 5. Seed the database

```bash
python seed.py
```

Creates:
- 👤 **Admin**: `admin@medintel.ai` / `Admin1234!`
- 👤 **Underwriter**: `uw@medintel.ai` / `Underwriter1!`
- 4 insurance applications with AI underwriting notes
- 4 claims with fraud scores and AI analysis
- 1 insurance policy

### 6. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:3001** (or 3000 if port is free).

---

## 🤖 AI Features

### RAG-Powered Underwriting Assistant

The **Underwriting Desk** uses a full RAG pipeline backed by Google Gemini:

1. Select any application from the Review Queue
2. Click **Analyze** in the AI Assistant panel
3. The system runs a **concurrent hybrid retrieval** (Dense + BM25, RRF fusion, cross-encoder re-ranking) against the `policy_vectors` ChromaDB collection
4. Gemini streams a structured underwriting report token-by-token with **inline citation markers** (`[^1]`, `[^2]`, …)
5. Clicking a citation badge opens the **PDF CitationViewer** at the exact bounding box page

**Graceful degradation**: If ChromaDB is unavailable, citations return `[]` and streaming continues uninterrupted.

### Knowledge Base Ingestion

Admins upload policy PDFs at `/knowledge`:

1. Drop a PDF → backend saves to temp file
2. Background task: `unstructured` hi-res parse → LLM table enrichment → semantic chunking → Gemini embedding → ChromaDB upsert
3. Document chunks are immediately available for RAG retrieval

### RAGAS ML Evaluation

Trigger an evaluation run from the Knowledge page or via API:

```bash
curl -X POST http://localhost:8000/api/v1/mlops/evaluate \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"collection_name": "policy_vectors"}'
```

Returns a `run_id` to poll:

```bash
curl http://localhost:8000/api/v1/mlops/evaluate/status/<run_id> \
  -H "Authorization: Bearer <token>"
```

Scores: `faithfulness`, `context_precision`, `context_recall`, `answer_relevancy` — CI gate requires ≥ 90% faithfulness.

---

## 📋 Claims Pipeline

```
Upload (PDF/JSON)
       │
       ▼
  EDI 837P Parsing
       │
       ▼
  SNIP Validation (5 tiers)
  ┌────────────────────────┐
  │ T1: Interchange        │
  │ T2: Functional Group   │
  │ T3: Balance Check      │
  │ T4: NPI Luhn           │
  │ T5: ICD-10 Validity    │
  └────────────────────────┘
       │ pass
       ▼
  Kafka Publish → claim_validated topic
       │
       ▼
  Adjudication (Claims Adjuster)
  ┌─────────────────────────────────┐
  │ Approve → status: approved      │
  │ Deny → denial modal → reason    │
  │          → status: denied       │
  └─────────────────────────────────┘
```

---

## 🔐 Authentication & RBAC

| Role | Email | Password | Permissions |
|---|---|---|---|
| `admin` | `admin@medintel.ai` | `Admin1234!` | Full access — all endpoints including `/knowledge/upload`, `/mlops/evaluate` |
| `underwriter` | `uw@medintel.ai` | `Underwriter1!` | Applications, underwriting stream, knowledge upload |
| `claims_adjuster` | *(seed manually)* | — | Claims intake, PATCH claim status |
| `insured` | *(seed manually)* | — | Own policies and claims only |

- JWT Bearer tokens (60 min expiry), stored in `localStorage`
- All `/api/v1/*` routes require authentication except `/auth/token`
- RBAC enforced via `require_role()` FastAPI dependency at route level

---

## 📡 API Reference

Interactive Swagger docs: **http://localhost:8000/docs**

### Auth
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/auth/token` | Login — returns JWT |
| `POST` | `/api/v1/auth/register` | Register new user |

### Applications
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/applications/` | List applications (paginated) |
| `POST` | `/api/v1/applications/` | Submit new application |
| `GET` | `/api/v1/applications/{id}/decision` | Get UW decision + AI summary |
| `PATCH` | `/api/v1/applications/{id}/underwrite` | Record UW decision |

### Underwriting
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/underwriting/{id}/ai-summary/stream` | **RAG + Gemini SSE stream** |

### Claims
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/claims/intake` | Ingest EDI 837P JSON — SNIP + Kafka |
| `POST` | `/api/v1/claims/upload` | Upload PDF/PNG — mock extraction + intake |
| `GET` | `/api/v1/claims/` | List claims (paginated, role-scoped) |
| `GET` | `/api/v1/claims/{id}` | Get claim by ID |
| `PATCH` | `/api/v1/claims/{id}/status` | Approve / deny claim (adjuster/admin) |

### Policies
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/policies/` | Create policy (admin/underwriter) |
| `GET` | `/api/v1/policies/` | List policies (role-scoped) |
| `GET` | `/api/v1/policies/{id}` | Get policy by ID |

### Knowledge Base ✨
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/knowledge/upload` | Upload PDF for RAG ingestion (admin/underwriter) |

### MLOps ✨
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/mlops/evaluate` | Trigger RAGAS evaluation run (admin) |
| `GET` | `/api/v1/mlops/evaluate/status/{run_id}` | Poll evaluation run status |

---

## 🧪 Testing

### Backend unit & integration tests

```bash
source .venv/bin/activate

# All backend tests
pytest backend/tests/ -v --asyncio-mode=auto

# Specific suites
pytest backend/tests/test_claims_api.py -v      # 12 tests: upload + PATCH status
pytest backend/tests/test_underwriting.py -v    # 10 tests: SSE stream + RAG mocking
pytest backend/tests/test_policies.py -v        # 12 tests: CRUD + RBAC
```

### MLOps evaluation tests (no API key required)

```bash
# 5-application gold-standard accuracy batch (≥90% SLA)
pytest backend/mlops/evaluation.py::test_underwriting_accuracy -v

# Deterministic prompt injection tests
pytest backend/mlops/red_team.py -k "injection" -v

# Full red team (requires OPENAI_API_KEY)
pytest backend/mlops/red_team.py::test_red_team_no_breaches -v
```

### E2E tests (Playwright)

```bash
cd frontend
npm install
npx playwright install --with-deps chromium

# All E2E specs (no backend required — all API calls mocked)
npx playwright test

# Individual suites
npx playwright test e2e/claims_journey.spec.ts --headed      # 6 tests
npx playwright test e2e/underwriting_journey.spec.ts --headed # 9 tests
npx playwright test e2e/citation_ui.spec.ts --headed          # 8 tests
```

### Load testing (Locust)

```bash
pip install locust faker

# Interactive UI
locust -f load_testing/locustfile.py --host=http://localhost:8000

# Headless CI — mixed workload (claims + UW stream + knowledge uploads)
locust -f load_testing/locustfile.py \
  --host=http://localhost:8000 \
  --users=200 --spawn-rate=10 --run-time=120s \
  --headless --csv=load_testing/results/run

# Open Locust UI at http://localhost:8089
```

**Locust user mix:**
| User Class | Weight | Task | SLA Target |
|---|---|---|---|
| `ClaimSubmitter` | 70% | `POST /claims/intake` | p95 < 100ms |
| `InvalidClaimUser` | 20% | Malformed claim (SNIP reject) | p95 < 50ms |
| `ReadOnlyUser` | 10% | `GET /claims/` list | p95 < 200ms |
| `UnderwritingStreamUser` | 15% | SSE AI stream (TTFT metric) | TTFT p95 < 800ms |
| `KnowledgeUploadUser` | 2% | `POST /knowledge/upload` | p95 < 500ms |

---

## 🛠️ Development

### TypeScript type check

```bash
cd frontend
npx tsc --noEmit
```

### VS Code setup

The `.vscode/settings.json` and `pyrightconfig.json` are pre-configured to:
- Use `.venv/bin/python3.11` for all Python files
- Use `frontend/node_modules/typescript/lib` for the TS language server
- Auto-format Python with Ruff, TypeScript with Prettier on save

> **If you see "Cannot find module" errors**: Press `Ctrl+Shift+P` → `Python: Select Interpreter` → choose `.venv/bin/python3.11`, then `Developer: Reload Window`.

---

## 📦 Tech Stack

### Backend
| Library | Purpose |
|---|---|
| **FastAPI** | Async Python API framework |
| **SQLAlchemy** (async) + **asyncpg** | PostgreSQL ORM + async driver |
| **Pydantic v2** | Request/response validation |
| **google-genai v2** | Gemini SDK — streaming LLM calls + embeddings |
| **ChromaDB** | Vector store for RAG retrieval |
| **unstructured** | hi-res PDF parsing for knowledge ingestion |
| **aiokafka** | Async Kafka producer for claims events |
| **ragas** | RAG evaluation metrics (faithfulness, precision, recall) |
| **mlflow** | Experiment tracking for evaluation runs |
| **bcrypt** | Password hashing |
| **structlog** | Structured JSON logging |
| **uvicorn** | ASGI server |

### Frontend
| Library | Purpose |
|---|---|
| **Next.js 14** (App Router) | React framework with SSR |
| **TypeScript** | Type safety |
| **CSS / Tailwind** | Styling with Material Design 3 token system |
| **Material Symbols** | Icon font |
| **Playwright** | E2E browser testing |

### Infrastructure
| Tool | Purpose |
|---|---|
| **PostgreSQL 15** | Primary relational database |
| **ChromaDB** | Vector database for RAG |
| **Kafka** (AIOKafka) | Async event streaming for claims pipeline |
| **Docker / docker-compose** | Local infrastructure |
| **Prometheus + Grafana** | Observability (docker-compose.observability.yml) |
| **Locust** | Load & performance testing |

---

## 📝 License

MIT © 2026 MedIntelligence Platform
