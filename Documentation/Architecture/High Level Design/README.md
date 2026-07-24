# High Level Design (HLD) - MBB ya Kin Architecture

This folder contains the **system-wide target architectural vision** for **MBB ya Kin**. It includes planned integrations and deployment models that are not all enabled or validated.

## Current Implementation Overlay

The current application remains provider-neutral and disconnected from external AI APIs. Baileys is the validated local WhatsApp transport for the controlled inbound-to-fallback-send scope; it is unofficial and has no permanent production approval. The isolated local production-like runtime validated PostgreSQL, Redis, FastAPI, Celery worker, dashboard, and Nginx, including authentication, routing, healthchecks, restart recovery, and database persistence.

The default production scope excludes monitoring, backup, Celery Beat, and Baileys. It keeps external AI, WhatsApp sending, CRM, payments, relance, scheduled tasks, and MAPS fanout disabled. PostgreSQL is not host-published. Dashboard access requires Basic Auth plus an explicitly provisioned API token; the dashboard does not auto-mint an administrator JWT.

No public deployment exists. Domain ownership, a public host, DNS, public ports 80 and 443, permanent production secrets, CA-issued TLS, certificate renewal, Nginx certificate reload, and public deployment validation are deferred. Treat provider-specific diagrams and VPS/TLS descriptions in the architecture documents as target design, not current runtime evidence.

---

## 📋 Document Index

### 1. [Architecture Design Doc](1.%20Architecture%20Design%20Doc.md)
**Purpose**: High-level system design and component relationships

**Contains:**
- System boundary & scope
- Major components (Bot Engine, CRM Interface, Payment Gateway, etc.)
- Data flow between components
- Target integration points with external systems (messaging, CRM, and AI providers)
- System constraints & assumptions for DRC environment

**Read This When:** You need to understand how the 7 major modules interact and where data flows.

---

### 2. [Modeling & Technology Stack](2.%20Modeling%20&%20Technology%20Stack.md)
**Purpose**: Technology selection rationale and system model

**Contains:**
- Why the design considered FastAPI, Celery, PostgreSQL, Redis, and external AI providers
- Alternative options considered & rejected
- Technology matrix (Framework vs. Performance vs. Cost)
- DRC-specific justifications (e.g., why Docker instead of serverless)
- Target deployment model (public host not yet selected or deployed)
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

## 🏛️ Target Architecture Overview (Quick Visual)

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

## 🔄 Target Data Flow: A Lead's Journey

The validated local flow uses Baileys, local fallback response selection, persistence, and exactly-one outbound fallback delivery. The provider-specific steps below remain target examples for future external integrations.

1. **User sends WhatsApp message** → selected messaging adapter webhook
2. **FastAPI router (M1)** receives & validates → passes to Celery worker (M3)
3. **Celery worker** processes the task → calls Conversation Engine (M4)
4. **Conversation Engine (M4)** asks: "Is this lead hot/warm/cold?"
5. **Lead Qualification (M5)** scores the lead → calls AI via **AIAdapter**
6. **AIAdapter** (future selected provider) → scores the lead
7. **CRMAdapter** (future selected provider) → synchronizes the lead
8. **Nurturing Engine (M6)** → schedules follow-up message in +24h
9. **Message queued in Redis** → waits for delivery window
10. **Response sent back** via WhatsApp API

**What Makes This Great for DRC:**
- If a future CRM provider times out → the adapter should fail safely and preserve retryable work
- If a future AI provider is unavailable → the application should use the validated local fallback rather than assume another provider is connected
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

**Why This Matters:** Provider boundaries allow future integrations to change without rewriting core conversation logic. No CRM, payment, or external AI provider is connected in the current validated state, and any future provider still requires configuration and separate validation.

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
   - Current validated state: external CRM and payment actions disabled
   - Future target: select and validate provider adapters
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

**Q: Why consider self-hosting instead of a managed cloud?**

A: DRC regulatory concerns, cost predictability, and network reliability motivate the target design. A public deployment host and domain have not yet been selected or validated.

**Q: Can we scale to 100K+ leads?**  
A: That remains a target requiring load and public-deployment validation. Current evidence covers the isolated local production-like recovery runtime, not that scale.

**Q: What if a future third-party API changes pricing?**

A: The adapter pattern limits provider coupling, but switching still requires credentials, configuration, safety review, and validation.

**Q: How do we handle GDPR/DRC data protection?**

A: Data minimization, consent, access control, retention, encryption, and audit requirements belong in the deployment security plan. No public host or data-residency posture has yet been validated.

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
