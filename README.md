# MBB ya Kin: Multi-Language Lead Nurturer Bot for DRC
**"A Helpful Congolese Friend on WhatsApp"**

![Status](https://img.shields.io/badge/Status-Recovery%20Mode-yellow)
![Python](https://img.shields.io/badge/Python-3.12-green)
![License](https://img.shields.io/badge/License-Internal-red)

---

## 🎯 What is MBB ya Kin?

**MBB ya Kin** is a self-hosted WhatsApp-first chatbot project designed specifically for the **Democratic Republic of Congo (DRC)** market. It remains in **recovery and stabilization mode**, focused on proving and protecting one clean MVP flow before feature expansion.

Recovery and local stabilization are nearly complete. The project is **not publicly deployed**, **not production-ready**, and **not pilot-ready**.

## Current Recovery Status

Current validated recovery evidence at baseline `f45a45d49f79d4c05d1d1be1253c8ba7ab11bedc`:

- **Baileys is the validated local WhatsApp transport.** Controlled live inbound, session restoration, international phone handling, persistence, and exactly-one outbound fallback delivery passed. Baileys recovery is closed for that controlled inbound-to-fallback-send scope.
- Baileys uses an unofficial WhatsApp transport. The local result is not permanent production approval and does not establish public-service suitability.
- **PostgreSQL, Redis, FastAPI, the Celery worker, the Streamlit dashboard, and Nginx** passed isolated local production-like startup. Authentication, routing, healthchecks, restart recovery, and database persistence passed.
- The worker consumes `default`, `relance`, `maps`, `escalation`, and `conversion`. PostgreSQL is not published to the host by the production configuration.
- Dashboard access requires Nginx Basic Auth and an explicitly provisioned dashboard API token. The dashboard does not hold the JWT signing secret and does not auto-mint an administrator JWT.
- The application is provider-neutral and currently disconnected from external AI APIs. `AI_ADAPTER=disabled` selects the local fallback path; Claude, OpenAI, Gemini, and other external AI providers are not connected.
- Monitoring, backup, Celery Beat, and Baileys are outside the default production scope. External AI, WhatsApp sending, CRM writes, payments, relance, scheduled tasks, and MAPS fanout remain disabled by default.

Public deployment remains deferred. It requires domain ownership, a public deployment host, DNS, public ports 80 and 443, permanent production secrets, CA-issued TLS, certificate renewal, Nginx certificate reload, and public deployment validation.

Known non-blocking constraint: `scripts/init_db.sql` currently assumes the database identifier `mbb`. Do not treat the database name as freely configurable until that script supports another identifier.

### Design Priorities for DRC Constraints
- Operate safely across **unstable 3G/4G networks** and power recovery
- Use persistent queues and idempotent boundaries where validated
- Keep CRM, payment, AI, and messaging providers behind adapter boundaries
- Preserve a lightweight **WhatsApp-first** customer interface

---

## 📊 Intended Product Targets
| Target | Planned Target |
| :--- | :--- |
| Response Time | < 60 seconds |
| Automation Rate | 80–85% |
| Conversion Increase | +30% |
| Relance Response Rate | 35–45% |

---

## 🏗️ Tech Stack

| Component | Technology | Purpose |
| :--- | :--- | :--- |
| **Messaging** | Baileys / WhatsApp adapter boundary | Baileys is validated locally for the controlled inbound-to-fallback-send scope |
| **Backend** | FastAPI | REST API & business logic |
| **Orchestration** | Celery worker; Celery Beat opt-in | Worker validated on five queues; scheduled tasks disabled by default |
| **Intelligence** | Provider-neutral adapter boundary; disabled/local fallback mode | No external AI provider is currently connected |
| **Database** | PostgreSQL | Leads, orders, sessions |
| **Cache/Queues** | Redis | Sessions, message queues |
| **Adapters** | Python ABC | Pluggable integrations |
| **Analytics** | Streamlit | Implemented funnel, relance, and language views |
| **Container** | Docker & Docker Compose | Deployment |

---

## 🚀 Quick Start (Development Mode)

### Prerequisites

| Tool | Version | Check |
|------|---------|-------|
| **Docker Desktop** | 24+ | `docker --version` |
| **Docker Compose** | v2.20+ | `docker compose version` |
| **Git** | 2.40+ | `git --version` |
| **Python** | 3.12 | `python --version` (for local tests only) |

> **Windows users**: Use PowerShell. All commands below work on Windows, macOS, and Linux.

---

### Step 1 — Clone & Initial Setup

```bash
# Clone the repository
git clone https://github.com/rado4002/MBB-Project.git
cd MBB-Project

# (Optional) Generate secrets with defaults — or manually create them
bash scripts/init_secrets.sh
```

This creates `./secrets/*.txt` files with placeholder values. For **dev/testing**, the defaults work fine. For production, replace each placeholder with real credentials.

**Secrets files created** (in `./secrets/`):

| File | Purpose | Dev Default |
|------|---------|-------------|
| `postgres_db.txt` | PostgreSQL database name | `mbb` |
| `postgres_user.txt` | PostgreSQL username | `mbb` |
| `postgres_password.txt` | PostgreSQL password | `CHANGE_ME_strong_db_password` |
| `jwt_secret.txt` | JWT signing key (auto-generated) | Random 64-char hex |
| `claude_api_key.txt` | Claude AI API key | `CHANGE_ME_sk-ant-...` |
| `airtable_api_key.txt` | Airtable Personal Access Token | `CHANGE_ME_pat...` |
| `whatsapp_api_token.txt` | WhatsApp Cloud API token (prod only) | Placeholder |
| `orange_money_key.txt` | Orange Money API key | Placeholder |
| `airtel_money_key.txt` | Airtel Money API key | Placeholder |
| `mpesa_key.txt` | M-Pesa API key | Placeholder |
| `payment_webhook_secret.txt` | HMAC key for payment callbacks | Placeholder |
| `grafana_admin_password.txt` | Grafana dashboard login | Placeholder |
| `redis_password.txt` | Redis auth (optional) | Placeholder |

### Step 2 — Environment Configuration

```bash
# Copy the environment template
cp .env.example .env
```

The `.env` file controls adapter selection and service addresses. Key settings for development:

```ini
APP_ENV=development
WHATSAPP_MODE=baileys          # Validated local transport for the controlled MVP scope
AI_ADAPTER=disabled            # recovery mode; no external AI provider connected
WHATSAPP_SEND_ENABLED=false    # default safety gate; controlled outbound validation already passed
CRM_ADAPTER=airtable           # airtable | mbb_hub
TZ=Africa/Kinshasa
```

> **Note**: Secrets (passwords, API keys) are stored in `./secrets/*.txt`, NOT in `.env`. The `.env` file is committed to git; the `secrets/` directory is gitignored.

### Step 3 — Start the Dev Environment

```bash
# Build and start the development stack.
# During recovery validation, keep celery_beat stopped/exited unless a task explicitly authorizes it.
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

First run takes 3–5 minutes (pulling images + building). Subsequent starts take < 10 seconds.

**What starts:**

| Service | Port | Purpose |
|---------|------|---------|
| **postgres** | `localhost:5433` | PostgreSQL 16 database |
| **redis** | `localhost:6379` | Cache + task broker (AOF persistence) |
| **api** | Internal `:8000` | FastAPI backend (behind nginx) |
| **celery_worker** | — | 4 async workers, 5 task queues |
| **celery_beat** | — | RedBeat periodic scheduler; keep stopped/exited during recovery validation |
| **baileys** | `localhost:3000` | Validated local WhatsApp bridge; unofficial transport, not production-approved |
| **dashboard** | Internal `:8501` | Streamlit analytics (behind nginx) |
| **nginx** | `localhost:80` | Reverse proxy (routes to api + dashboard) |
| **prometheus** | `localhost:9090` | Metrics collection |
| **grafana** | `localhost:3001` | Metrics dashboards |
| **loki** | Internal `:3100` | Log aggregation |

### Step 4 — Verify Everything Is Running

```bash
# Check all container statuses
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps

# Check API health (should return {"status":"ok",...})
curl http://localhost/health

# Or directly (bypassing nginx)
curl http://localhost:8000/health    # Won't work — api is not port-mapped
# Use: docker exec bot-api-1 python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"
```

**Expected healthy output:**
```json
{
  "status": "ok",
  "checks": {"redis": true, "database": true},
  "blackout_queue_depth": 0
}
```

### Step 5 — Connect WhatsApp (Optional — requires phone + internet)

**Option A: Live QR Dashboard (Browser)**

Open **http://localhost:3000/qr** in your browser. The dashboard polls for the QR code every 10 seconds and displays it as a high-res PNG image that's easy to scan with your phone.

**Features:**
- ✅ Auto-refresh every 10 seconds (no manual refresh needed)
- ✅ Server-side QR generation (fast, no external CDN)
- ✅ Shows "WhatsApp Connected!" once logged in
- ✅ Logout button to reset the session

**Option B: Terminal QR Code**

```bash
# Watch logs for the QR code (renders as ASCII art)
docker logs bot-baileys-1 -f
```

**Steps (both options):**
1. Open WhatsApp on your phone
2. Go to **Settings → Linked Devices → Link a Device**
3. Scan the QR code
4. Once connected, the dashboard shows: "✅ WhatsApp Connected!" with your JID

> **Note**: The QR refreshes every ~20 seconds. Scan it quickly. If your internet is unstable, restart baileys: `docker compose -f docker-compose.yml -f docker-compose.dev.yml restart baileys`

### Step 6 — Check Baileys Health (no phone needed)

```bash
# Health endpoint — works without WhatsApp connection
curl http://localhost:3000/health
# Returns: {"status":"ok","connected":false,"jid":null}

# After scanning QR:
# Returns: {"status":"ok","connected":true,"jid":"243XXXXXXXXX@s.whatsapp.net"}
```

---

### Available Commands (Makefile)

```bash
make help          # Show all available commands
make up            # Start dev environment (all 11 services)
make down          # Stop dev environment
make restart       # Restart all services
make logs          # Follow all container logs
make logs-api      # Follow API logs only
make logs-celery   # Follow Celery worker + beat logs
make build         # Build all Docker images
make rebuild       # Force rebuild (no cache)
make shell-api     # Bash shell inside API container
make shell-db      # PostgreSQL shell (psql)
make redis-cli     # Redis CLI inside container
make migrate       # Run Alembic database migrations
make seed          # Seed test data into database
make up-prod       # Start production (3 API replicas, WhatsApp Official)
make down-prod     # Stop production
```

### Access Points (Dev Mode)

| Service | URL | Auth |
|---------|-----|------|
| **WhatsApp QR Dashboard** | http://localhost:3000/qr | None |
| **Baileys Health** | http://localhost:3000/health | None |
| **API Docs** (Swagger) | http://localhost/api/docs | None |
| **API Health** | http://localhost/health | None |
| **Streamlit Dashboard** | http://localhost/dashboard/ | Nginx Basic Auth plus explicitly provisioned API token |
| **Grafana** | http://localhost:3001 | admin / (see `secrets/grafana_admin_password.txt`) |
| **Prometheus** | http://localhost:9090 | None |
| **PostgreSQL** | `localhost:5433` | user/pass from `secrets/` |
| **Redis** | `localhost:6379` | None (dev) |

---

### Running Tests (Local — No Docker Required)

Tests run directly with Python, using a local PostgreSQL + Redis:

```powershell
# Navigate to backend
cd backend

# Install Python dependencies (one-time)
pip install -r requirements.txt

# Set environment variables (PowerShell)
$env:APP_ENV="development"
$env:POSTGRES_HOST="localhost"
$env:POSTGRES_PORT="5433"         # Docker-mapped port
$env:POSTGRES_DB="mbb"
$env:POSTGRES_USER="mbb"
$env:POSTGRES_PASSWORD="mbb_postgres_change_me"  # From secrets/postgres_password.txt
$env:REDIS_HOST="localhost"
$env:JWT_SECRET="testsecret32charslongpadding12345"
$env:CLAUDE_API_KEY="test"
$env:AIRTABLE_API_KEY="test"
$env:PAYMENT_WEBHOOK_SECRET="test"

# Run test suites
python tests/test_project_setup.py          # 15 checks — project structure & imports
python tests/test_schema_api_validation.py  # 12 checks — Pydantic schemas & API routes
python tests/test_blackout_simulation.py    # 6 checks  — Redis blackout queue
python tests/test_resilience.py             # 7 checks  — circuit breakers & idempotency
```

**Linux/macOS equivalent:**
```bash
export APP_ENV=development POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
  POSTGRES_DB=mbb POSTGRES_USER=mbb POSTGRES_PASSWORD=mbb_postgres_change_me \
  REDIS_HOST=localhost JWT_SECRET=testsecret32charslongpadding12345 \
  CLAUDE_API_KEY=test AIRTABLE_API_KEY=test PAYMENT_WEBHOOK_SECRET=test

python tests/test_project_setup.py
python tests/test_schema_api_validation.py
python tests/test_blackout_simulation.py
python tests/test_resilience.py
```

> **Note**: `test_blackout_simulation.py` and `test_resilience.py` require Docker services running (PostgreSQL on `:5433`, Redis on `:6379`). `test_project_setup.py` and `test_schema_api_validation.py` work offline.
- **Dashboard**: http://localhost/dashboard/
- **Grafana**: http://localhost:3001/ (dev mode)
- **Health Check**: http://localhost/health

---

## 📁 Project Structure

```
mbb-ya-kin/
├── Documentation/                  # 📚 All architecture & design docs
│   ├── Architecture/
│   │   ├── High Level Design/     # System design, tech stack, data model
│   │   ├── Low Level Design/      # Modules, APIs, database, security
│   │   └── Adapter Architecture Guide.md
│   ├── functional-and-non-functional-requirements.md
│   ├── Use Cases, Misuse Cases & User Stories.md
│   └── Implementation Roadmap & Workflow.md
│
├── backend/                        # 🐍 FastAPI Backend
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py                # FastAPI entry point + health checks
│       ├── config.py              # Settings (reads Docker secrets)
│       ├── database.py            # PostgreSQL async engine
│       ├── redis_client.py        # Redis connection pool
│       ├── adapters/              # Adapter pattern (AI, CRM, Inventory, Payment, Messaging)
│       │   ├── __init__.py        # Factory functions
│       │   └── base.py            # Base interfaces
│       ├── models/                # SQLAlchemy ORM (Sprint 0.2)
│       ├── schemas/               # Pydantic request/response
│       ├── modules/               # M1-M9 business logic
│       │   ├── m1_gateway/        # WhatsApp webhook handler
│       │   ├── m2_conversation/   # Language detection + AI response
│       │   ├── m3_queue/          # Blackout-aware message queue
│       │   ├── m4_nurturing/      # Product recommendations + persuasion
│       │   ├── m5_qualification/  # Lead scoring (hot/warm/cold)
│       │   ├── m6_relance/        # Max 3 relances (value-first)
│       │   ├── m7_conversion/     # Mobile Money + order management
│       │   ├── m8_maps/           # MAPS intelligence + escalation
│       │   └── m9_dashboard/      # Dashboard endpoints (admin ops)
│       └── tasks/                 # Celery async tasks
│           ├── celery_app.py      # Celery config (RedBeat, 5 queues)
│           ├── relance.py         # (Sprint 1.B)
│           ├── maps.py            # (Sprint 1.D)
│           ├── escalation.py      # (Sprint 1.D)
│           └── conversion.py      # (Sprint 1.C)
│
├── dashboard/                      # 📊 Streamlit Dashboard (M9)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       └── main.py                # 3 roles: admin, hub, lab
│
├── baileys/                        # 📱 WhatsApp Bridge (Dev Mode)
│   ├── Dockerfile
│   ├── package.json
│   └── src/
│       └── index.js               # Baileys → FastAPI webhook forwarder
│
├── nginx/                          # 🌐 Reverse Proxy
│   ├── nginx.conf                 # Base config (gzip, rate limits, upstreams)
│   └── conf.d/
│       ├── mbb.conf               # HTTP config (dev)
│       └── mbb.ssl.conf           # HTTPS + HTTP Basic Auth (prod)
│
├── redis/
│   └── redis.conf                 # AOF persistence (blackout recovery)
│
├── monitoring/                     # 📈 Observability
│   ├── prometheus/
│   │   └── prometheus.yml         # Scrape configs
│   ├── grafana/
│   │   └── datasources/           # Auto-provisioned datasources
│   │       └── datasource.yml
│   └── loki/
│       └── loki-config.yml        # Log aggregation
│
├── scripts/                        # 🔧 Setup & Utilities
│   ├── init_db.sql                # PostgreSQL schema + extensions
│   ├── init_secrets.sh            # Generate ./secrets/*.txt files
│   └── seed_data.py               # Test data (Sprint 0.2)
│
├── secrets/                        # 🔐 Docker Secrets (gitignored)
│   ├── .gitkeep
│   ├── postgres_db.txt            # Generated by init_secrets.sh
│   ├── postgres_user.txt
│   ├── postgres_password.txt
│   ├── jwt_secret.txt
│   ├── claude_api_key.txt
│   └── ... (11 total secret files)
│
├── docker-compose.yml              # Base: 7 services (postgres, redis, api, celery, dashboard, nginx, monitoring)
├── docker-compose.dev.yml          # Dev overrides: Baileys, hot-reload, exposed ports
├── docker-compose.prod.yml         # Prod overrides: 3 API replicas, SSL, resource limits
├── Makefile                        # Commands: make setup, make up, make down, make migrate, etc.
├── .env.example                    # Environment template (copy to .env)
└── README.md                       # This file
```

### Key Architectural Decisions
- **Adapter Pattern**: Keep AI, CRM, payment, inventory, and messaging providers behind explicit boundaries; provider selection still requires configuration and validation
- **Dual WhatsApp Mode**: `WHATSAPP_MODE=baileys` is the validated local transport; the official/public path is deferred
- **DRC-First Design**: Redis AOF persistence, circuit breakers, < 10KB payloads, blackout recovery
- **Modular (M1-M9)**: Each business domain is isolated; modules communicate via FastAPI + Celery

---

## ⚙️ Configuration

The system is configured through three layers:

### 1. Environment Variables (`.env`)
Non-secret configuration. Committed to git as `.env.example`.

```ini
# Core
APP_ENV=development              # development | production
WHATSAPP_MODE=baileys            # baileys (dev) | official (prod)
TZ=Africa/Kinshasa

# Adapter selection — external integrations remain disabled until separately validated
AI_ADAPTER=disabled              # disabled | claude
CRM_ADAPTER=airtable             # airtable | mbb_hub
INVENTORY_ADAPTER=static         # static | mbb_box
PAYMENT_ADAPTER=mobile_money     # mobile_money

# Service addresses (Docker internal — don't change for dev)
POSTGRES_HOST=postgres
REDIS_HOST=redis

# Optional Claude adapter settings; the provider is not connected in the current state
CLAUDE_MODEL=claude-sonnet-4-5
CLAUDE_MAX_TOKENS=1024
CLAUDE_TIMEOUT_S=25
```

### 2. Docker Secrets (`./secrets/*.txt`)
Passwords and API keys. Gitignored. Each file contains a single raw value (no trailing newline).

```bash
# View current secrets (placeholders)
cat secrets/postgres_password.txt

# Replace with real values before production
echo -n "my_real_password" > secrets/postgres_password.txt
```

### 3. Docker Compose Overrides
- `docker-compose.yml` — Base: all 11 services defined
- `docker-compose.dev.yml` — Dev: hot-reload, Baileys, exposed debug ports
- `docker-compose.prod.yml` — Production-like safety overlay validated locally; public deployment and CA-issued TLS remain deferred

```bash
# Dev (Baileys + hot-reload + debug ports)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# Isolated local production-like validation only; not public deployment guidance
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The default production scope starts PostgreSQL, Redis, API, Celery worker, dashboard, and Nginx. It excludes Baileys, monitoring, backup, and Celery Beat unless their separate scope/profile is intentionally enabled. PostgreSQL has no production host-port publication.

---

## 🤖 Product and Implementation Areas

This section describes product intent and implemented surfaces. It is not evidence that every area is enabled, externally connected, or ready for pilot use. The default recovery configuration keeps external AI, sending, CRM, payments, relance, schedules, and MAPS fanout off.

### 1. **Multi-Language Conversation Engine**
Detects user language from first message:
- **Lingala** (Kinshasa primary)
- **French** (default fallback)
- **Swahili** (Goma/East)

The conversation layer is designed to respond naturally, like a helpful young Congolese friend rather than a corporate chatbot. The current validated path uses local fallback behavior because no external AI provider is connected.

### 2. **Lead Qualification** (Automated)
Bot asks 2–3 smart questions → scores lead:
- **Hot** (85+): High purchase intent → immediate follow-up
- **Warm** (50–84): Interested but needs nurture
- **Cold** (<50): Early interest → long-term relance

### 3. **Nurturing Workflows**
- Daily product recommendations
- Lab insights & health tips
- Delivery options & stock info
- Payment method guidance

### 4. **Relance (Follow-up) Engine**
Smart reminders at optimal times:
- **+24h**: First gentle reminder
- **+48–72h**: Second reminder with offer
- **+7–10d**: Final reminder before opt-out
- **Respects**: User timezone, power outage patterns, opt-out signals

### 5. **Payment Integration** (disabled by default)
The adapter surface is intended to support:
- **Mobile Money** (Orange/Airtel)
- **Bank Transfer** (COD available)
- **Future**: MBB Payments API (Phase 2)

### 6. **Escalation System**
Routes to human agents if:
- Voice note received
- 3 failed AI responses
- High-value lead detected
- Complex issue (order issue, refund request)

### 7. **MAPS Intelligence**
Captures & analyzes:
- Demand patterns by location/time
- Language preferences
- Silence reasons ("yaka te", "arrête")
- Conversion triggers

### 8. **Analytics Dashboard**
Implemented Streamlit views:
- Funnel (Leads → Qualified → Converted)
- Relance performance
- Language breakdown
- CSV downloads

The response-time page is a placeholder pending metrics integration. Google
Sheets export is not implemented.

---

## 🎮 How Adapters Work (The "Plug & Play" Strategy)

The bot's "Brain" accesses provider-specific services through adapter interfaces. The application is provider-neutral; choosing and enabling a provider still requires credentials, safety-gate changes, and separate validation.

### Example: Switching AI Models
```bash
# Current recovery mode
AI_ADAPTER=disabled

# External providers are not connected in the validated recovery state.
# Re-enable only in a separate validated step.
```

**Why This Matters:**
1. **Recovery Safety**: keep external AI disabled while the core flow is being stabilized.
2. **Controlled Reconnection**: re-enable provider adapters only in separate validated steps.
3. **Boundary Clarity**: keep AI, CRM, payment, and messaging integrations behind adapter boundaries.

For full details, see [Adapter Architecture Guide](Documentation/Architecture/Adapter%20Architecture%20Guide.md).

---

## 🔌 WhatsApp Integration Details (Baileys)

### Message Inbound Flow

The controlled local MVP path is:

```
WhatsApp Phone
    ↓ (WhatsApp Web protocol)
Baileys Bridge (/messages.upsert event)
    ↓ (HTTP POST with payload transformation)
FastAPI POST /api/v1/messages/baileys
    ↓ (M1 service layer: upsert customer, conversation, message)
PostgreSQL (tables: customers, conversations, messages)
    ↓ (Celery task processes and selects local fallback)
Outbound response persisted
    ↓ (Baileys adapter send-back with idempotency boundary)
Exactly one fallback response delivered
    ↓
Streamlit Dashboard (Conversation Mirror page)
    ↓ (Auto-refresh shows new conversations)
User sees conversation in UI
```

Controlled live inbound, session restoration, international phone handling, persistence, and exactly-one outbound fallback delivery passed. Dashboard/API read safety also passed. This closes Baileys recovery for the controlled inbound-to-fallback-send scope. It does not grant permanent production approval to the unofficial transport; the default send gate remains off outside explicitly controlled validation.

### Baileys Webhook Payload Schema

The Baileys bridge (`baileys/src/index.js`) transforms incoming WhatsApp events into this schema:

```json
{
  "customer_phone": "+243682126391",          // E.164 format (+ prefix required)
  "whatsapp_message_id": "wamid.XXXX",       // WhatsApp's message ID (for idempotency)
  "content": "Mbote! Habari...",              // Text content (empty string for media)
  "content_type": "text",                     // "text" | "audio" | "image" | "voice_note"
  "timestamp": "2026-05-01T14:23:45.123Z"    // ISO 8601 format (UTC)
}
```

**Key Transformations:**
- Phone number: `243682126391` → `+243682126391` (E.164 format)
- Field mapping: `message_id` → `whatsapp_message_id`, `type` → `content_type`
- WhatsApp JID: Phone is stored as normalized: `243682126391@s.whatsapp.net` (no +)

### Baileys Endpoints

| Endpoint | Method | Purpose | Response |
|----------|--------|---------|----------|
| `/qr` | GET | HTML dashboard with live QR code | HTML with auto-refresh JS |
| `/qr.json` | GET | JSON status (QR code as data URL) | `{connected, jid, qrDataUrl, ts}` |
| `/health` | GET | Health check | `{status: "ok", connected, jid}` |
| `/send` | POST | Send outbound message | `{success: true}` |
| `/logout` | POST | Disconnect & reset session | `{success: true, message}` |

### Celery Async Pool Management (Important!)

**Problem**: SQLAlchemy async engine's `asyncpg` connection pool is inherited by forked Celery workers. The pool's Futures are bound to the parent process's event loop, causing "Future attached to a different loop" errors.

**Solution**: In `backend/app/tasks/celery_app.py`, we use the `@worker_process_init` signal to reset the pool after fork:

```python
from celery.signals import worker_process_init

@worker_process_init.connect
def reset_db_pool(**kwargs):
    from app.database import engine
    engine.sync_engine.dispose(close=False)  # Close stale connections, allow new ones
```

This ensures each worker gets fresh connections bound to its own event loop.

---

## 👨‍💻 For Developers

### Testing the WhatsApp → Dashboard Pipeline

Baileys is now validated for the controlled local scope described above. Repeat live tests only as explicitly controlled work with the external-send safety gates and idempotency boundary understood.

Historical/manual live-path checklist:

```bash
# 1. Open the QR dashboard in browser
open http://localhost:3000/qr

# 2. Scan QR from phone (WhatsApp → Settings → Linked Devices → Link a Device)

# 3. Send a test message from the linked WhatsApp phone

# 4. Check Docker logs for successful webhook processing
docker logs bot-api-1 | grep "m1.inbound_persisted"

# 5. Check Celery task execution
docker logs bot-celery_worker-1 | grep "m1_process_inbound_task"

# 6. Verify message was saved to database
docker exec bot-postgres-1 psql -U mbb -d mbb -c "SELECT * FROM mbb.messages ORDER BY created_at DESC LIMIT 5;"

# 7. Check Streamlit dashboard shows the conversation
open http://localhost/dashboard/

# Expected output:
# - QR dashboard shows "✅ WhatsApp Connected!" with your JID
# - Message appears in Conversation Mirror page within seconds
# - Database query returns the message row with correct content_type and language
```

### Running Unit Tests
```bash
# From the backend/ directory with env vars set (see Quick Start)
python tests/test_project_setup.py          # Structure + imports (15 checks)
python tests/test_schema_api_validation.py  # Schemas + routes (12 checks)
python tests/test_blackout_simulation.py    # Redis queue (6 checks, needs Docker)
python tests/test_resilience.py             # Circuit breakers (7 checks, needs Docker)

# Total: 40 checks across 4 test files
```

### Adding a New Adapter (e.g., Telegram)
1. Define the interface in `app/adapters/messaging_interface.py`
2. Create `app/adapters/telegram_adapter.py` implementing that interface
3. Add `TELEGRAM_ADAPTER` to `.env`
4. Update `app/adapters/factory.py` to load it
5. Test with DRC network conditions (see [Testing Checklist](Documentation/Architecture/Low%20Level%20Design/README.md))

### Code Style
- **Python 3.11+**, async/await throughout
- **FastAPI** for REST APIs
- **SQLAlchemy** ORM with async support
- **Pydantic** for request/response schemas
- **Black** for formatting (`black app/`)
- **Ruff** for linting (`ruff check app/`)

### Database Migrations
```bash
# Create a new migration
alembic revision --autogenerate -m "Add hub_synced column"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade -1
```

---

## 📚 Documentation Structure

| Document | Purpose |
| :--- | :--- |
| [Problem Statement](Documentation/Problem%20Statement.md) | Why we built this |
| [Functional Requirements](Documentation/functional-and-non-functional-requirements.md) | FR1–FR14 feature specs |
| [Use Cases & User Stories](Documentation/Use%20Cases,%20Misuse%20Cases%20&%20User%20Stories.md) | UC-01 through UC-08 |
| [High-Level Design](Documentation/Architecture/High%20Level%20Design/) | System architecture, tech stack |
| [Low-Level Design](Documentation/Architecture/Low%20Level%20Design/) | Modules M1–M7, APIs, database schema |
| [Adapter Architecture Guide](Documentation/Architecture/Adapter%20Architecture%20Guide.md) | How to add new integrations |

---

## 🔒 Security & Compliance

- **Dashboard authentication**: Nginx Basic Auth plus a separately and explicitly provisioned dashboard API token.
- **Fail-closed dashboard startup**: no token means no dashboard access to the API; the dashboard does not auto-mint an administrator JWT.
- **Messaging safety**: webhook authentication, validation, persistence, and outbound idempotency are present on the controlled Baileys path.
- **Network isolation**: PostgreSQL is not host-published in the production configuration.
- **Default-off external effects**: external AI, WhatsApp sends, CRM writes, payments, relance, scheduled tasks, and MAPS fanout require deliberate enablement and separate validation.

The wider security design contains target-state controls as well as implementation detail. Public TLS and public deployment security have not been validated.

See [Security Design](Documentation/Architecture/Low%20Level%20Design/4.%20Security%20Design.md) for full details.

---

## 🛡️ Error Handling & Resilience

Critical for DRC where **power outages & network timeouts are normal**:

- **Circuit Breaker Pattern**: Auto-fallback if Airtable/HUB times out
- **Message Queuing**: Redis handles offline → online recovery
- **Idempotency**: Duplicate messages prevented via unique lead IDs
- **Graceful Degradation**: Use cached data if API unreachable
- **Auto-Escalation**: Route to human if bot fails 3 times

See [Exception & Error Handling](Documentation/Architecture/Low%20Level%20Design/5.%20Exception%20&%20Error%20Handling.md).

---

## 🧪 Testing on DRC Network Conditions

We simulate real-world Kinshasa constraints:

```bash
# Test on throttled 3G (1 Mbps, 100ms latency)
docker run --cap-add=NET_ADMIN \
  tc qdisc add dev eth0 root tbf rate 1mbit burst 32kbit latency 100ms

pytest tests/resilience/ -k "3g"

# Monitor before/after
docker exec bot tail -f /app/logs/latency_report.json
```

---

## 🚨 Troubleshooting

| Issue | Solution |
| :--- | :--- |
| `Cannot connect to Docker daemon` | Start Docker Desktop, wait for it to be fully ready |
| Containers fail to start | Run `docker compose -f docker-compose.yml -f docker-compose.dev.yml logs <service>` |
| Port 80 conflict (nginx) | Stop Apache/IIS or any other service on port 80 |
| Port 5432 conflict (postgres) | Local PostgreSQL running — dev compose maps to `5433` instead |
| Baileys QR not showing | Check logs: `docker logs bot-baileys-1`. Needs internet to reach WhatsApp servers |
| Baileys 405/408 errors | Outdated WA version — the code auto-fetches latest. Restart: `docker compose ... restart baileys` |
| Messages not appearing in dashboard | Check: (1) API logs for `m1.inbound_persisted`, (2) Celery logs for task execution, (3) PostgreSQL for message records. Refresh dashboard browser window. |
| Celery "Future attached to different loop" errors | Worker pool not reset after fork. Verify `@worker_process_init` signal handler is running in `celery_app.py`. Restart workers: `docker compose ... restart celery_worker` |
| Message inbound webhook returns 422 | Baileys payload schema mismatch. Verify `customer_phone` (with +), `whatsapp_message_id`, and `content_type` fields match Pydantic schema in `schemas/message.py`. |
| `apt-get` fails during build | Flaky network — retry the build, or use `--no-cache` flag |
| API health returns unhealthy | Check postgres & redis are healthy first: `docker compose ... ps` |
| Celery tasks fail with ImportError | Expected — module services (M2–M9) are not built yet (Phase 1 work) |
| Dashboard not loading | Access via nginx: `http://localhost/dashboard/` (trailing slash required) |
| Docker pull fails (EOF) | Network instability — retry: `docker pull <image>` then rebuild |

---

## 📞 Support & Contributions

- **Issues**: Create a GitHub issue with logs & `.env` redacted
- **Email**: Tech team at MBB
- **Slack**: #mbb-ya-kin-dev channel

---

## 📄 License

Internal MBB Project. Not for external distribution.

---

## Current Status & Next Steps

### Recovery Mode

Recovery and local stabilization are nearly complete. The controlled Baileys inbound-to-fallback-send scope and the isolated local production-like runtime are validated as summarized in [Current Recovery Status](#current-recovery-status).

The next step is a final stabilization audit. Public deployment work remains a separate deferred phase, and no current evidence establishes production or pilot readiness. Older roadmap completion markers and the historical pilot runbook are planning/history, not readiness proof.

---

**Built with ❤️ for Kinshasa. Optimized for Africa.**
