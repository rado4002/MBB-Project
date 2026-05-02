# MBB ya Kin: Multi-Language Lead Nurturer Bot for DRC
**"A Helpful Congolese Friend on WhatsApp"**

![Status](https://img.shields.io/badge/Phase-1.A%20(Core%20System)-blue)
![Python](https://img.shields.io/badge/Python-3.12-green)
![License](https://img.shields.io/badge/License-Internal-red)

---

## 🎯 What is MBB ya Kin?

**MBB ya Kin** is a production-ready, self-hosted WhatsApp AI chatbot designed specifically for the **Democratic Republic of Congo (DRC)** market. It speaks Lingala, French, and Swahili—automates lead qualification, nurtures prospects through multi-language conversations, and converts them to paying customers.

### Built for DRC Constraints
- ✅ Works on **3G/4G networks** with frequent power outages
- ✅ **Offline-first** architecture with Redis queuing
- ✅ **Zero code changes** when switching CRM/Payment providers (Adapter Pattern)
- ✅ **WhatsApp-first** interface (no website needed)

---

## 📊 Key Metrics
| Target | Current Phase |
| :--- | :--- |
| Response Time | < 60 seconds |
| Automation Rate | 80–85% |
| Conversion Increase | +30% |
| Relance Response Rate | 35–45% |

---

## 🏗️ Tech Stack

| Component | Technology | Purpose |
| :--- | :--- | :--- |
| **Messaging** | WhatsApp Business API | User-facing channel |
| **Backend** | FastAPI | REST API & business logic |
| **Orchestration** | Celery + Celery Beat | Async tasks & scheduling |
| **Intelligence** | Claude 3.5 Sonnet | NLU & lead qualification |
| **Database** | PostgreSQL | Leads, orders, sessions |
| **Cache/Queues** | Redis | Sessions, message queues |
| **Adapters** | Python ABC | Pluggable integrations |
| **Analytics** | Streamlit | Funnel & performance dashboard |
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
WHATSAPP_MODE=baileys          # Uses Baileys bridge (no WhatsApp Business account needed)
AI_ADAPTER=claude              # claude | gemini
CRM_ADAPTER=airtable           # airtable | mbb_hub
TZ=Africa/Kinshasa
```

> **Note**: Secrets (passwords, API keys) are stored in `./secrets/*.txt`, NOT in `.env`. The `.env` file is committed to git; the `secrets/` directory is gitignored.

### Step 3 — Start the Dev Environment

```bash
# Build and start all 11 services
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
| **celery_beat** | — | RedBeat periodic scheduler |
| **baileys** | `localhost:3000` | WhatsApp bridge (Baileys) |
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
| **Streamlit Dashboard** | http://localhost/dashboard/ | None |
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
- **Adapter Pattern**: Switch AI (Claude/Gemini), CRM (Airtable/MBB HUB), Inventory (Static/MBB BOX) via env vars
- **Dual WhatsApp Mode**: `WHATSAPP_MODE=baileys` (dev) or `official` (prod) — zero code changes
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

# Adapter Selection — switch providers without code changes
AI_ADAPTER=claude                # claude | gemini
CRM_ADAPTER=airtable             # airtable | mbb_hub
INVENTORY_ADAPTER=static         # static | mbb_box
PAYMENT_ADAPTER=mobile_money     # mobile_money

# Service addresses (Docker internal — don't change for dev)
POSTGRES_HOST=postgres
REDIS_HOST=redis

# AI tuning
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
- `docker-compose.prod.yml` — Prod: 3 API replicas, SSL, resource limits

```bash
# Dev (Baileys + hot-reload + debug ports)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# Prod (WhatsApp Official + 3 replicas + SSL)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

## 🤖 Core Features

### 1. **Multi-Language Conversation Engine**
Detects user language from first message:
- **Lingala** (Kinshasa primary)
- **French** (default fallback)
- **Swahili** (Goma/East)

The AI responds naturally, like a helpful young Congolese friend (not a corporate chatbot).

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

### 5. **Payment Integration**
Accepts payments via:
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
Streamlit dashboard showing:
- Funnel (Leads → Qualified → Converted)
- Relance performance
- Response times & coverage
- Language breakdown
- CSV/Google Sheets export

---

## 🎮 How Adapters Work (The "Plug & Play" Strategy)

The bot's "Brain" never talks directly to Airtable, Claude, or Orange Money. Instead, it uses **Universal Sockets** (Adapter Interfaces). Swap the **Plug** in `.env`—the bot logic stays unchanged.

### Example: Switching AI Models
```bash
# Current (Claude)
AI_ADAPTER=ANTHROPIC_CLAUDE

# Switch to Gemini? (No code changes)
# AI_ADAPTER=GOOGLE_GEMINI
```

**Why This Matters:**
1. **Speed to Market**: Use Airtable today, migrate to MBB HUB tomorrow (0 dev days).
2. **Resilience**: If Claude is slow from Kinshasa, auto-switch to Gemini (configured in 1 line).
3. **Cost Control**: Use cheap Gemini Flash for FAQs, Claude Sonnet for hard decisions.

For full details, see [Adapter Architecture Guide](Documentation/Architecture/Adapter%20Architecture%20Guide.md).

---

## 🔌 WhatsApp Integration Details (Baileys)

### Message Inbound Flow

The complete pipeline from phone message to database:

```
WhatsApp Phone
    ↓ (WhatsApp Web protocol)
Baileys Bridge (/messages.upsert event)
    ↓ (HTTP POST with payload transformation)
FastAPI POST /api/v1/messages/baileys
    ↓ (M1 service layer: upsert customer, conversation, message)
PostgreSQL (tables: customers, conversations, messages)
    ↓ (Celery task processes async)
Streamlit Dashboard (Conversation Mirror page)
    ↓ (Auto-refresh shows new conversations)
User sees real WhatsApp conversation in UI
```

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

### Testing the Complete WhatsApp → Dashboard Pipeline

To verify end-to-end message flow works:

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

- **HMAC Verification** on all WhatsApp webhooks
- **API Key Authentication** with Bearer tokens
- **Idempotency Keys** on all financial transactions
- **PII Encryption** at rest (PostgreSQL encryption)
- **Rate Limiting** per user (max 10 msgs/min)
- **GDPR Compliance** with opt-out respect
- **DRC Data Residency** (self-hosted VPS in/near DRC)

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

## 🎉 Current Status & Next Steps

### Phase 0 — Foundation ✅ (Complete)
- [x] Docker Compose stack (11 services running)
- [x] PostgreSQL schema with 15+ tables, indexes, materialized views
- [x] Redis AOF persistence + Celery 5-queue configuration
- [x] FastAPI app with health checks, middleware, security
- [x] Pydantic schemas for all 10 API domains
- [x] Adapter pattern (AI, CRM, Inventory, Payment, Messaging)
- [x] Baileys WhatsApp bridge with QR code + auto-version-fetch
- [x] Streamlit dashboard skeleton
- [x] Nginx reverse proxy
- [x] Monitoring stack (Prometheus, Grafana, Loki)
- [x] CI/CD pipeline (GitHub Actions: lint, test, docker-build)
- [x] 40 passing test checks (structure, schemas, blackout, resilience)

### Phase 1 — Core System 🚀 (In Progress)

#### **Stage 1.A: M1 Gateway + WhatsApp Integration** ✅ (Complete)
- [x] **Live QR Dashboard** — http://localhost:3000/qr with 10-second auto-refresh
- [x] **Baileys WhatsApp Bridge** — Webhook payload transformation + E.164 formatting
- [x] **Message Inbound Pipeline** — Baileys → FastAPI webhook → Celery task → PostgreSQL
- [x] **Conversation Mirroring** — Real WhatsApp conversations visible in Streamlit dashboard
- [x] **Celery Async Tasks** — Worker process initialization for async engine pool management
- [x] **M1 Message Processing** — Customer upsert, conversation management, language detection
- [x] **Error Handling** — HMAC verification, circuit breakers, DRC resilience

#### **Stage 1.B: M2 Conversation Engine + M5 Lead Qualification** 🔜 (Next)
- [ ] M2: AI response generation (Claude integration)
- [ ] M5: Lead scoring (hot/warm/cold classification)
- [ ] M6: Relance scheduling (max 3 follow-ups)

#### **Stage 1.C: M7 Conversion + Payment Integration** 🔜 (Planned)
- [ ] Mobile Money payment handling (Orange/Airtel)
- [ ] Order creation + fulfillment tracking
- [ ] COD payment flow

#### **Stage 1.D: M8 MAPS Intelligence + M9 Dashboard** 🔜 (Planned)
- [ ] MAPS metrics aggregation
- [ ] Escalation system for voice notes
- [ ] Admin dashboard endpoints (M9)

#### **Stage 1.E: Integration Testing + Security Audit + Pilot** 🔜 (Planned)
- [ ] End-to-end testing across all modules
- [ ] Security penetration testing
- [ ] Pilot deployment to 500 leads

### Phase 2 — Advanced Intelligence (Q3–Q4 2026)
- Voice note handling, Gemini fallback, MBB HUB/BOX adapters

### Phase 3 — Scale (2027)
- Kubernetes, multi-city deployment, predictive AI

---

**Built with ❤️ for Kinshasa. Optimized for Africa.**
