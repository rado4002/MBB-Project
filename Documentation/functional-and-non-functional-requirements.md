MBB Assistant — Functional & Non-Functional Requirements

Multi-Language Lead Nurturer Bot (“MBB ya Kin”)

Version: 1.1 (Final — Blueprint-Integrated)

Date: April 2026

Prepared for: MBB Leadership, Toronto Supervision, Hub, Lab & Development Teams

Preamble

This document is the final, production-ready requirements specification for the MBB Assistant. It fully incorporates:

* All original project documentation (Problem Statement, Vision, System Specification v2.1)
* The Persuasion & Growth Blueprint (Cialdini’s 7 Principles, StoryBrand SB7, Blue Ocean Strategy ERRC, Hormozi’s $100M Leads systems)
* Validation feedback from the Toronto Supervision review cycle

The requirements now explicitly embed psychological triggers, storytelling, value innovation, and lead-systemization logic into the bot’s behavior, ensuring every interaction is warm, culturally attuned, resilient to RDC realities, and optimized for +30 % conversion while feeding the MAPS loop in real time.

---

1. Functional Requirements (FR)

FR1 — Message Reception & Processing

* The system shall receive incoming messages from WhatsApp via a dual-mode channel adapter:
  * **Development & Testing:** Baileys Node.js bridge (free, QR-code-authenticated WhatsApp Web session using a test phone number).
  * **Production:** WhatsApp Business API (official, paid).
* The system shall process messages in real-time (<60 seconds) regardless of which WhatsApp mode is active.
* The system shall support:
  * Text messages
  * Emojis
  * Voice notes (trigger immediate escalation)
* The switch between Baileys and official API shall be configuration-driven (`WHATSAPP_MODE` environment variable) with zero code changes to business logic modules.

FR2 — Multi-Language Detection & Response

* The system shall automatically detect user language: Lingala, French, Swahili.
* The system shall respond in the same detected language (or seamless hybrid).
* The system shall support intra-message code-switching (e.g., Lingala-French-Swahili within a single message) and reply in the dominant or hybrid style.
* If detection fails, fallback to French.

FR3 — Conversation Management

* The system shall maintain conversation context per user session.
* The system shall store: conversation history, user preferences, interaction state.
* The system shall generate: natural, human-like responses and context-aware replies (guided by StoryBrand SB7 and Cialdini principles).

FR4 — Lead Capture & Qualification

* The system shall initiate a qualification flow with 2–3 contextual questions (user location/city, product interest, intent).
* The system shall classify leads as: Hot, Warm, Cold.
* The system shall store lead data in the database with initial MAPS tags.

FR5 — Nurturing Engine

* The system shall provide: product recommendations, Lab-based product insights, pricing information, delivery options.
* The system shall personalize responses using: city, device type, previous interactions, and Persuasion Blueprint triggers (reciprocity, liking/unity, social proof).

FR6 — Conversion & Order Processing

* The system shall enable users to place orders directly inside WhatsApp.

Payment Methods (fully enumerated per v2.1):

* Mobile Money (Orange Money, Airtel Money, M-Pesa)
* Bank transfer (auto-send account details)
* Cash on Delivery (COD)
* Payment at Spot pickup

Order Flow:

* The system shall: confirm order details, trigger Hub routing (moto-taxi delivery or Spot pickup), generate confirmation message with tracking reference, and automatically credit Club points.

FR7 — Relance (Re-Engagement) Engine

* The system shall automatically re-engage silent leads.

Rules:

* Maximum 3 relances per lead (lifetime).

Timing:

* 1st relance → after 24 hours
* 2nd relance → after 48–72 hours
* 3rd relance → after 7–10 days

Behavior:

* Messages must be value-first, context-aware, culturally adapted, and follow the Persuasion Blueprint (reciprocity first, then social proof, scarcity only when genuine).

Opt-Out:

* The system shall stop all relance and communication when the user sends: “stop”, “arrête”, “non”, or any Lingala/Swahili equivalent.

FR8 — MAPS Intelligence Capture

* The system shall capture structured data from every interaction: product demand, silence reasons, language usage, conversion triggers, Cialdini principles used, SB7 stage completed, ERRC value created, Hormozi offer score.
* The system shall tag and store MAPS patterns automatically and feed them to the Lab in real time.

FR9 — Escalation to Human Agents

* The system shall escalate conversations when: voice note detected, complex complaint, high-value lead, or more than 3 unresolved messages.
* The system shall notify the Hub team for takeover within <3 minutes via WhatsApp/Telegram with full conversation transcript, lead score, and all Persuasion Blueprint tags applied.

FR10 — Queue & Resilience Handling

* The system shall queue messages during network outages or power failures.
* The system shall retry sending messages when connectivity is restored.
* The system shall send the recovery message: “Je suis toujours là même si le courant est coupé 😊” (reinforcing reciprocity and proximity).

FR11 — Analytics, Dashboard & Admin Operations

* The system shall provide a **dual-purpose Streamlit dashboard** serving both analytics visualization and admin operations within a single application (M9).

**FR11.1 — Analytics Pages (all roles):**
* The system shall provide a dashboard displaying:
  * Conversion funnel
  * Response times
  * Relance performance (35–45 % 1st-relance target)
  * Language distribution
  * MAPS insights (minimum 20 new patterns per month)
  * Persuasion lift metrics (e.g., reciprocity → conversion delta)
* The system shall support: CSV export and Google Sheets integration.

**FR11.2 — Admin Operations Pages (admin role only):**
* The system shall provide a **Bot Configuration** page allowing the admin to:
  * Edit the master prompt template (with version history)
  * Manage relance message templates per language (Lingala/French/Swahili)
  * Toggle feature flags (e.g., enable/disable Gemini fallback, MBB BOX integration)
  * Switch active adapters via configuration (e.g., `CRM_PROVIDER`, `WHATSAPP_MODE`)
* The system shall provide an **Escalation Manager** page allowing the admin to:
  * View all open escalation tickets with priority and SLA status
  * Assign tickets to Hub Team members
  * Resolve or re-escalate tickets
  * **Manually toggle a conversation between bot control and human control** via a "Take Over" / "Return to Bot" button — creating or resolving an escalation ticket accordingly
* The system shall provide a **Content Manager** page allowing the admin to:
  * Manage the static product catalog fallback (used when MBB BOX is unreachable)
  * Edit i18n message templates (error messages, recovery messages, opt-out confirmations)
  * Manage relance value hooks per language and lead stage
* The system shall provide a **System Control** page allowing the admin to:
  * View circuit breaker states (Claude, MBB HUB, MBB BOX)
  * Monitor queue depth and dead-letter queue; trigger manual retries
  * View adapter health status and last sync timestamps
* All admin operations shall be logged in an **audit trail** (who, what, when) stored in PostgreSQL.

**FR11.3 — Hub Operations Pages (hub role):**
* The Hub Team shall be able to view and manage escalation tickets assigned to them.
* The Hub Team shall be able to view lead details and manually override lead status when justified.
* The Hub Team shall be able to **return a conversation to bot control** via a "Return to Bot" button after resolving an escalation.

**FR11.4 — Lab Operations Pages (lab role):**
* The Lab Team shall be able to perform tone audits (approve/flag conversations) from the dashboard.
* The Lab Team shall be able to validate or retire MAPS patterns from the insights explorer.

FR12 — Privacy & Consent Management

* The system shall request user consent in the first interaction.
* The system shall allow: data deletion requests and opt-out from communication.
* The system shall store minimal required data only.

FR13 — MBB HUB Integration (Phase 2)

* The system shall integrate with MBB HUB via an **MBBHubAdapter** using the Adapter Pattern.
* The adapter shall support: customer CRM data sync, Club points management, order routing, and escalation handoff.
* The integration shall replace the current AirtableAdapter (M7) as the primary CRM backend.
* The switch from Airtable to MBB HUB shall be configuration-driven and require zero code changes to modules M1–M9 business logic.
* The system shall maintain backward compatibility with AirtableAdapter during the transition period.

FR14 — MBB BOX Integration (Phase 2+)

* The system shall integrate with MBB BOX via an **MBBBoxAdapter** using the Adapter Pattern.
* The adapter shall support: real-time product inventory checks, dynamic pricing updates, product catalog retrieval, and stock availability by city/zone.
* The bot shall use live MBB BOX data for nurturing (M5) and conversion (M7) flows instead of static product configuration.
* The integration shall be additive — the system must function without MBB BOX (graceful fallback to static catalog).
* Future adapters (e.g., logistics, analytics partners) shall follow the same Adapter Pattern and require zero changes to existing modules.

---

2. Non-Functional Requirements (NFR)

NFR1 — Performance

* First response time shall be <60 seconds for 100 % of messages.
* System shall handle high concurrent message volume.
* Message processing latency shall be minimal.

NFR2 — Reliability & Availability

* System shall function under unstable network conditions and frequent power outages.
* Message queue system shall ensure no message loss.
* System uptime target: ≥ 95 %.

NFR3 — Scalability

* System shall scale horizontally: add more users without performance degradation.
* Support expansion to multiple cities: Kinshasa, Lubumbashi, Goma (and future cities).

NFR4 — Usability (User Experience)

* Conversations shall feel: human, natural, friendly.
* Messages shall be: short (2–3 sentences), easy to understand, emoji-friendly.

NFR5 — Cultural & Linguistic Accuracy

* System shall adapt to local expressions, cultural tone, and regional language patterns.
* Weekly tone audits shall be supported (Lab reviews 10 % of conversations against “proximity” and hero-centric checklists).

NFR6 — Security & Privacy

* System shall ensure secure data storage and protection of user data.
* Only minimal required data shall be stored.

NFR7 — Maintainability

* System shall be: modular, easy to update, easy to debug.
* Clear separation between: conversation logic, business logic, data layer.

NFR8 — Extensibility

* System shall allow future integration with: Telegram, web chat, additional APIs without rework.
* System shall use the Adapter Pattern for all external system integrations (WhatsApp, CRM, LLM, payments, inventory).
* New adapters (MBB HUB, MBB BOX, or others) shall be pluggable without modifying core business logic (M2–M9).
* Adapter registration shall be configuration-driven (environment variables or config files).

NFR9 — Deployment Constraints

* System shall be: fully self-hosted, Dockerized.
* Must run on low-resource infrastructure (low-bandwidth-first design).

NFR10 — Observability & Monitoring

* System shall log: errors, failures, performance metrics.
* Alerts shall be generated for: system failures, message delivery issues.

NFR11 — Data Consistency

* User data shall remain consistent across sessions and channels.
* No duplication of lead records.

NFR12 — Compliance & Ethics

* System shall: respect user consent, avoid spam behavior, provide clear opt-out mechanisms.
* All interactions must remain value-first and culturally respectful (never robotic or pushy).

---

Approval & Traceability Note

This Version 1.1 is fully validated and directly traceable to:

* Original project documents (April 2026)
* Persuasion & Growth Blueprint (Cialdini, StoryBrand, Blue Ocean, Hormozi)
* Technical architecture (WhatsApp dual-mode [Baileys for dev/testing + Business API for production] + FastAPI + Celery + Claude + PostgreSQL + Redis + Streamlit)

Success Definition

When deployed, the MBB Assistant will deliver:

* 80–85 % automation rate
* +30 % lead-to-order conversion (Phase 1)
* Customers consistently feeling “I’m talking to a real person”
* Continuous, actionable MAPS intelligence for the Lab

This document is now the single source of truth for all development, testing, pilot, and scaling activities.