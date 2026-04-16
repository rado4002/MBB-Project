# High Level Design (HLD) - MBB ya Kin Architecture

This folder contains the **system-wide architectural vision** for **MBB ya Kin**. These documents describe **WHAT** the system does and **HOW** the major components interact.

---

## 📋 Document Index

### 1. [Architecture Design Doc](1.%20Architecture%20Design%20Doc.md)
**Purpose**: High-level system design and component relationships

**Contains:**
- System boundary & scope
- Major components (Bot Engine, CRM Interface, Payment Gateway, etc.)
- Data flow between components
- Integration points with external systems (WhatsApp, Airtable, Claude)
- System constraints & assumptions for DRC environment

**Read This When:** You need to understand how the 7 major modules interact and where data flows.

---

### 2. [Modeling & Technology Stack](2.%20Modeling%20&%20Technology%20Stack.md)
**Purpose**: Technology selection rationale and system model

**Contains:**
- Why we chose FastAPI, Celery, PostgreSQL, Redis, Claude
- Alternative options considered & rejected
- Technology matrix (Framework vs. Performance vs. Cost)
- DRC-specific justifications (e.g., why Docker instead of serverless)
- Deployment model (self-hosted VPS in Kinshasa)
- Scaling strategy (vertical first, horizontal in Phase 2)

**Read This When:** You want to know **WHY** we picked each technology and what trade-offs we made.

---

### 3. [Data Architecture & Interface Design](3.%20Data%20Architecture%20&%20Interface%20Design.md)
**Purpose**: Data model and API contract overview

**Contains:**
- Core entities (Leads, Orders, Sessions, Messages)
- Data relationships & flow through the system
- REST API endpoints (high-level list)
- Adapter Pattern interfaces (CRMAdapter, AIAdapter, etc.)
- Webhook contract for WhatsApp
- Future-proofing for Phase 2 (MBB HUB) & Phase 2+ (MBB BOX)

**Read This When:** You need to understand what data flows where and how the bot talks to external systems.

---

## 🎯 How to Use This Folder

| I want to... | Read This |
| :--- | :--- |
| Understand the overall system architecture | Document 1 |
| Know why we chose FastAPI instead of Django | Document 2 |
| See the database schema & API endpoints | Document 3 |
| Learn how adapters work | [Adapter Guide](../Adapter%20Architecture%20Guide.md) |
| Deep-dive into specific modules | [Low Level Design](../Low%20Level%20Design/) |
| Understand all requirements | [Functional Requirements](../../functional-and-non-functional-requirements.md) |

---

## 🏛️ Architecture Overview (Quick Visual)

```
┌─────────────────────────────────────────────────────────────┐
│                      WhatsApp Users                          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    WhatsApp Business API                     │
│                    (Message Ingress/Egress)                  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────────┐
        │         FastAPI Backend (M1)             │
        │      [Webhook Handler + Router]          │
        └─────────────────────────────────────────┘
                    │
         ┌──────────┼──────────┐
         ▼          ▼          ▼
    ┌────────┐ ┌─────────┐ ┌──────────┐
    │ Celery │ │ Conv.   │ │ Lead     │
    │ (M3)   │ │ Engine  │ │ Quals.   │
    │        │ │ (M4)    │ │ (M5)     │
    └────────┘ └─────────┘ └──────────┘
         │          │          │
         └──────────┼──────────┘
                    ▼
        ┌─────────────────────────────────────────┐
        │      Adapter Interfaces (M2)             │
        │  ┌──────────────────────────────────┐   │
        │  │ CRM | AI | Payments | Inventory │   │
        │  └──────────────────────────────────┘   │
        └─────────────────────────────────────────┘
         │          │          │          │
    ┌────▼──┐  ┌───▼────┐ ┌──▼───┐  ┌─▼──────┐
    │Airtable│ │ Claude │ │Orange│  │Products│
    │(Phase1)│ │ Gemini │ │Money │  │ JSON   │
    └────────┘ └────────┘ └──────┘  └────────┘
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
                ┌────────┐ ┌────────┐ ┌────────┐
                │ Redis  │ │Postgres│ │ Logs   │
                │(Cache) │ │(Data)  │ │(Errors)│
                └────────┘ └────────┘ └────────┘
```

---

## 🔄 Data Flow: A Lead's Journey

1. **User sends WhatsApp message** → WhatsApp API webhook
2. **FastAPI router (M1)** receives & validates → passes to Celery worker (M3)
3. **Celery worker** processes the task → calls Conversation Engine (M4)
4. **Conversation Engine (M4)** asks: "Is this lead hot/warm/cold?"
5. **Lead Qualification (M5)** scores the lead → calls AI via **AIAdapter**
6. **AIAdapter** (Plug A: Claude) → scores lead as **85 (Hot)**
7. **CRMAdapter** (Plug B: Airtable) → saves lead to Airtable base
8. **Nurturing Engine (M6)** → schedules follow-up message in +24h
9. **Message queued in Redis** → waits for delivery window
10. **Response sent back** via WhatsApp API

**What Makes This Great for DRC:**
- If Airtable times out → CRMAdapter falls back to Redis queue (no lead lost)
- If Claude is slow → switch AI_ADAPTER to Gemini in `.env` (zero code changes)
- If power cuts out → Redis queue survives; messages retry when power returns

---

## 🔌 The "Plug & Play" Strategy

Every external system is accessed via an **Adapter Interface**:

```
┌─────────────────────────────────────┐
│   Conversation Engine (The Brain)   │  Always calls these methods:
│                                     │  - get_response()
│ The bot never talks directly to     │  - save_lead()
│ Airtable, Claude, or Orange Money   │  - process_payment()
└─────────────────────────────────────┘
                 │
                 ▼
    ┌─────────────────────────────┐
    │  Adapter Interfaces         │
    │  (The Universal Sockets)    │
    │                             │
    │ - AIAdapterInterface        │
    │ - CRMAdapterInterface       │
    │ - PaymentAdapterInterface   │
    └─────────────────────────────┘
                 │
      ┌──────────┼──────────┐
      ▼          ▼          ▼
   Claude     Airtable    Orange
   Gemini     MBB HUB     Airtel
   Llama      LOCAL_MOCK  MBB Payments
```

**Why This Matters:** On Day 1, use **Airtable + Claude + Orange Money**. On Day 180, swap to **MBB HUB + Gemini + MBB Payments**—all in `.env`, zero code changes.

**Boundary Reminder:** Social media pages, ad campaigns, content publishing, and the future official website belong to a separate Digital Presence Platform. MBB ya Kin consumes only the resulting leads and conversation entry points.

---

## 📊 Key Assumptions

1. **DRC Infrastructure is Unreliable**
   - Power: 8–12 hour daily blackouts common
   - Network: 3G/4G at 100–500 kbps typical
   - Solution: All operations **idempotent** and **resumable**

2. **Users are WhatsApp-Native**
   - Android dominant
   - Low phone storage
   - Mixed language preferences
   - Solution: Lightweight messages, 2–3 sentences max

3. **CRM & Payments are Evolving**
   - Today: Airtable + Orange Money
   - Tomorrow: MBB HUB + MBB Payments
   - Solution: Adapter Pattern ensures no rewrite needed

4. **Human Escalation is Required**
   - Some issues need a live agent (complex complaints, refunds)
   - Solution: Escalation criteria in M7 (Module 7)

---

## 🔗 Connected Documents

- **Feel comfortable with HLD?** → Read [Low Level Design](../Low%20Level%20Design/) to dive into code structure
- **Need to add a new payment provider?** → See [Adapter Architecture Guide](../Adapter%20Architecture%20Guide.md)
- **Want to understand ALL requirements?** → See [Functional Requirements](../../functional-and-non-functional-requirements.md)
- **Ready to implement?** → See [Main README](../../README.md) for setup instructions

---

## ❓ FAQs for HLD

**Q: Why FastAPI instead of Django REST Framework?**  
A: FastAPI is async-first (better for high concurrency), has automatic OpenAPI docs, and is lighter-weight for DRC bandwidth constraints.

**Q: Why self-hosted instead of cloud?**  
A: DRC regulatory concerns, cost predictability, and network reliability. VPS in Kinshasa = lower latency for local users.

**Q: Can we scale to 100K+ leads?**  
A: Phase 1 handles 10K leads on a 2-core VPS. Phase 2 adds horizontal scaling via load balancer + read replicas.

**Q: What if a third-party API (Airtable/Claude) changes pricing?**  
A: Adapter Pattern means we swap providers in 1 line. Built-in insurance against vendor lock-in.

**Q: How do we handle GDPR/DRC data protection?**  
A: Self-hosted (no data leaves your VPS), encryption at rest, opt-out mechanism respected, audit logs for compliance.

---

## 🤝 Contributing

If you're modifying HLD:
1. Update the relevant document (1, 2, or 3)
2. Ensure consistency with LLD (in the Low Level Design folder)
3. Update this README if structure changes
4. Link any new diagrams or examples

For more details, see the [main project README](../../README.md).

---

**Last Updated:** April 15, 2026  
**Status:** ✅ Phase 1 Ready | 🔄 Phase 2 Planned
