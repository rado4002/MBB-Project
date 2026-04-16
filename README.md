# MBB ya Kin: Multi-Language Lead Nurturer Bot for DRC
**"A Helpful Congolese Friend on WhatsApp"**

![Status](https://img.shields.io/badge/Phase-1.0%20(Airtable%2FStatic)-blue)
![Python](https://img.shields.io/badge/Python-3.11%2B-green)
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

## 🚀 Quick Start (5 minutes)

### Prerequisites
- Docker & Docker Compose
- `.env` file with API keys (see [Configuration](#configuration))

### Run Locally
```bash
# Clone the repository
git clone https://github.com/mbb-corp/mbb-ya-kin.git
cd mbb-ya-kin

# Copy and customize the environment file
cp .env.example .env

# Start all services
docker-compose up -d

# Verify services are running
docker-compose ps

# Check logs
docker-compose logs -f app
```

The bot is now live on your WhatsApp Business account. Send a test message to verify.

---

## 📁 Project Structure

```
mbb-ya-kin/
├── Documentation/                  # All architecture & design docs
│   ├── High Level Design/         # System design, tech stack, data model
│   ├── Low Level Design/          # Modules, APIs, database, security
│   └── Adapter Architecture Guide.md  # How to switch CRM/Payments/AI
├── app/                           # FastAPI backend code (Phase 2)
│   ├── adapters/                  # Universal sockets & Phase 1 tools
│   ├── core/                      # Conversation engine & workflows
│   ├── tasks/                     # Celery async tasks
│   ├── models/                    # SQLAlchemy database schemas
│   ├── api/                       # REST endpoints
│   └── main.py                    # FastAPI entry point
├── celery_config/                 # Celery worker & beat configuration
├── db/                            # SQL migrations
├── docker-compose.yml             # Multi-container setup
├── .env.example                   # Environment template
└── README.md                       # This file
```

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and customize:

```bash
# ========== WHATSAPP ==========
WHATSAPP_API_VERSION=v21.0
WHATSAPP_PHONE_ID=your_phone_id
WEBHOOK_VERIFY_TOKEN=your_webhook_token

# ========== AI MODEL ==========
# Options: ANTHROPIC_CLAUDE, GOOGLE_GEMINI, LOCAL_LLAMA
AI_ADAPTER=ANTHROPIC_CLAUDE
ANTHROPIC_API_KEY=sk-ant-...

# ========== CRM BACKEND ==========
# Options: AIRTABLE, MBB_HUB, LOCAL_MOCK
CRM_ADAPTER=AIRTABLE
AIRTABLE_BASE_ID=appXXX
AIRTABLE_API_KEY=key_XXX

# ========== INVENTORY ==========
# Options: STATIC_JSON, MBB_BOX
INVENTORY_ADAPTER=STATIC_JSON
PRODUCTS_JSON_PATH=/app/config/products.json

# ========== PAYMENTS ==========
# Options: ORANGE_MONEY, AIRTEL_MONEY, MBB_PAYMENTS
PAYMENT_ADAPTER=ORANGE_MONEY
ORANGE_MERCHANT_ID=your_merchant_id
ORANGE_API_KEY=your_api_key

# ========== DATABASE ==========
DATABASE_URL=postgresql://user:password@db:5432/mbb
REDIS_URL=redis://redis:6379/0

# ========== SYSTEM ==========
ENVIRONMENT=production
LOG_LEVEL=INFO
```

**For DRC Context:**
- Use `AIRTABLE` initially for fast setup
- Switch to `MBB_HUB` when ready (no code changes)
- Use `ORANGE_MONEY` for Kinshasa; `AIRTEL_MONEY` for Eastern DRC

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

## 👨‍💻 For Developers

### Running Tests
```bash
# Unit tests for adapters
pytest tests/adapters/

# Integration tests (requires .env)
pytest tests/integration/ --require-docker

# Test 3G latency
pytest tests/resilience/ -k "3g_simulation"
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
| Bot not responding | Check logs: `docker-compose logs app` |
| WhatsApp webhook not triggering | Verify WEBHOOK_VERIFY_TOKEN in .env |
| Airtable sync failing | Test API key: `curl -H "Authorization: Bearer $AIRTABLE_API_KEY" ...` |
| High latency from DRC | Switch AI_ADAPTER to GOOGLE_GEMINI |
| Database connection error | Ensure PostgreSQL is running: `docker-compose ps db` |

---

## 📞 Support & Contributions

- **Issues**: Create a GitHub issue with logs & `.env` redacted
- **Email**: Tech team at MBB
- **Slack**: #mbb-ya-kin-dev channel

---

## 📄 License

Internal MBB Project. Not for external distribution.

---

## 🎉 Next Steps

1. **Phase 1 (Now)**: Airtable + Claude + Orange Money launch
2. **Phase 2 (Q3 2026)**: Migrate to MBB HUB + MBB Payments (adapter swap)
3. **Phase 2+ (Q4 2026)**: Add MBB BOX inventory API
4. **Phase 3 (2027)**: Local Llama edge computing + SMS fallback

---

**Built with ❤️ for Kinshasa. Optimized for Africa.**
