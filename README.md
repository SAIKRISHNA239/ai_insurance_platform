# MedIntelligence — AI Healthcare Insurance Intelligence Platform

> **An enterprise-grade, full-stack AI underwriting platform** — combining a FastAPI backend, Next.js 14 frontend, PostgreSQL database, and live Google Gemini streaming for real-time medical underwriting analysis.

---

## ✨ Features

| Module | Capability |
|---|---|
| **Overview Dashboard** | Real-time KPI cards (claims, applications, approval rate), recent activity tables |
| **Claims Center** | Drag-and-drop document ingestion, AI fraud scoring, ICD-10 extraction, status pipeline |
| **Application Pipeline** | Visual stepper tracking submission → review → UW decision → closed |
| **Underwriting Desk** | HITL split-screen workspace — AI streaming analysis (Gemini) + citation viewer |
| **Marketplace** | Policy comparison grid with AI recommendation assistant |
| **Settings & Support** | Account info, platform config, FAQ documentation |
| **Auth** | JWT OAuth2 password flow, role-based access (Admin / Underwriter / Insured) |

---

## 🏗️ Architecture

```
ai_insurance_platform/
├── backend/                      # FastAPI Python backend
│   ├── api/
│   │   └── routers/              # HTTP route handlers
│   │       ├── auth.py           # POST /auth/token, /auth/register
│   │       ├── applications.py   # CRUD + underwriting decisions
│   │       ├── claims.py         # Claims ingestion & adjudication
│   │       ├── policies.py       # Policy management
│   │       └── underwriting.py   # ✨ Gemini SSE streaming endpoint
│   ├── llm/
│   │   └── client.py             # GeminiClient (google-genai v2), OpenAIClient
│   ├── database/
│   │   ├── models.py             # SQLAlchemy ORM models
│   │   ├── base.py               # Async engine setup
│   │   └── vector_client.py      # ChromaDB client
│   ├── middleware/
│   │   └── auth.py               # JWT + RBAC middleware
│   ├── config.py                 # Pydantic settings (reads .env)
│   └── main.py                   # FastAPI app factory
│
├── frontend/                     # Next.js 14 App Router frontend
│   ├── app/
│   │   ├── layout.tsx            # Root layout with AuthProvider + AuthShell
│   │   ├── login/page.tsx        # Login page with dev quick-fill
│   │   ├── overview/page.tsx     # Dashboard KPIs
│   │   ├── claims/page.tsx       # Claims processing queue
│   │   ├── applications/page.tsx # Application pipeline
│   │   ├── underwriting/page.tsx # HITL underwriting desk
│   │   ├── marketplace/page.tsx  # Policy marketplace
│   │   ├── settings/page.tsx     # Platform settings
│   │   └── support/page.tsx      # FAQ & documentation
│   ├── components/
│   │   ├── layout/
│   │   │   ├── Sidebar.tsx       # Global navigation
│   │   │   ├── TopHeader.tsx     # Top bar with user info & logout
│   │   │   └── AuthShell.tsx     # Route guard + shell renderer
│   │   ├── GenAIAssistant.tsx    # Gemini streaming chat component
│   │   └── ui/CitationViewer.tsx # Document citation viewer
│   └── lib/
│       ├── api.ts                # Typed API client (/api/v1 prefix, JWT)
│       └── AuthContext.tsx       # Global auth state (SSR-safe)
│
├── seed.py                       # Database seeder with test data
├── .env                          # Environment variables (see below)
├── pyrightconfig.json            # Pyright/Pylance config → uses .venv
└── .vscode/settings.json         # VS Code interpreter + formatter config
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11
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

Copy and edit the `.env` file:

```bash
cp .env.example .env   # or edit .env directly
```

Key variables to set:

```env
# Database (update host if running locally, not in Docker)
DATABASE_URL=postgresql+asyncpg://insurance_user:insurance_pass@localhost:5433/insurance_db

# Gemini AI (required for live streaming analysis)
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.0-flash
LLM_PROVIDER=gemini

# JWT secret
SECRET_KEY=your_random_32byte_hex_secret
```

### 3. Start infrastructure (Docker)

```bash
docker run -d --name insurance_postgres \
  -e POSTGRES_USER=insurance_user \
  -e POSTGRES_PASSWORD=insurance_pass \
  -e POSTGRES_DB=insurance_db \
  -p 5433:5432 postgres:15-alpine

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
source .venv/bin/activate
python seed.py
```

This creates:
- 👤 **Admin**: `admin@medintel.ai` / `Admin1234!`
- 👤 **Underwriter**: `uw@medintel.ai` / `Underwriter1!`
- 4 insurance applications with AI underwriting notes
- 4 claims with fraud scores and AI analysis

### 6. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:3001** (or 3000 if port is free).

---

## 🤖 Gemini AI Streaming

The **Underwriting Desk** uses Google Gemini for live clinical analysis:

1. Select any application from the Review Queue
2. Click **Analyze** in the Medical Summary panel  
3. Gemini streams a structured underwriting report token-by-token:
   - Risk Assessment (chronic conditions, biomarkers)
   - Actuarial Impact (table rating, premium loading)
   - Routing Recommendation (Preferred / Standard / Substandard / Decline)

**Fallback**: If the Gemini API is unavailable, the component displays pre-seeded AI notes from the database with a typewriter animation.

---

## 🔐 Authentication

| Role | Email | Password | Access |
|---|---|---|---|
| Admin | `admin@medintel.ai` | `Admin1234!` | Full platform access |
| Underwriter | `uw@medintel.ai` | `Underwriter1!` | Applications + Underwriting |

- JWT Bearer tokens stored in `localStorage` (dev mode)
- Tokens expire after 60 minutes
- All `/api/v1/*` routes require authentication except `/auth/token`

---

## 📡 API Reference

Interactive Swagger docs available at **http://localhost:8000/docs**

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/auth/token` | Login, returns JWT |
| `POST` | `/api/v1/auth/register` | Register new user |
| `GET` | `/api/v1/applications/` | List applications (paginated) |
| `POST` | `/api/v1/applications/` | Submit new application |
| `PATCH` | `/api/v1/applications/{id}/underwrite` | Record UW decision |
| `GET` | `/api/v1/underwriting/{id}/ai-summary/stream` | **Gemini SSE stream** |
| `GET` | `/api/v1/claims/` | List claims |
| `POST` | `/api/v1/claims/` | Ingest new claim |
| `PATCH` | `/api/v1/claims/{id}/status` | Update claim status |
| `GET` | `/api/v1/policies/` | List policies |

---

## 🛠️ Development

### Backend tests

```bash
source .venv/bin/activate
pytest backend/tests/ -v --asyncio-mode=auto
```

### TypeScript check

```bash
cd frontend
npx tsc --noEmit
```

### VS Code setup

The `.vscode/settings.json` and `pyrightconfig.json` are pre-configured to:
- Use `.venv/bin/python3.11` for all Python files (no "module not found" errors)
- Use `frontend/node_modules/typescript/lib` for TS language server
- Auto-format Python with Ruff, TypeScript with Prettier on save

> **If you still see "Cannot find module" errors in VS Code**: Press `Ctrl+Shift+P` → `Python: Select Interpreter` → choose `.venv/bin/python3.11`, then `Ctrl+Shift+P` → `Developer: Reload Window`.

---

## 📦 Tech Stack

### Backend
- **FastAPI** — async Python API framework
- **SQLAlchemy** (async) + **asyncpg** — PostgreSQL ORM
- **Pydantic v2** — request/response validation
- **google-genai v2** — Gemini SDK for streaming LLM calls
- **ChromaDB** — vector store for RAG
- **bcrypt** — password hashing
- **structlog** — structured JSON logging
- **uvicorn** — ASGI server

### Frontend
- **Next.js 14** (App Router) — React framework
- **TypeScript** — type safety
- **Tailwind CSS** — utility-first styling (Material Design 3 token system)
- **Material Symbols** — icon font

---

## 📝 License

MIT © 2026 MedIntelligence Platform
