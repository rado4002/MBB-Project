MBB Assistant — Multi-Language Lead Nurturer Bot

“MBB ya Kin”
Structure as a Service Initiative

Date: April 2026
Version: 2.1 (Final — Payment Systems Integrated)
Prepared for: Leadership, Toronto Supervision, Hub, Lab, Development Teams

---

1. Executive Overview

MBB Assistant is a self-hosted, multi-language conversational AI system designed to serve as the frontline interface of the MBB ecosystem via WhatsApp.

It transforms unstructured conversations into:

* Qualified leads
* Conversions
* Continuous MAPS intelligence

👉 This is not just a chatbot.
It is a scalable growth engine + intelligence layer.

---

2. Core Objectives

Business Goals

* Increase lead-to-order conversion (+30% target)
* Automate 80–85% of interactions
* Reduce lead drop-off due to silence
* Strengthen Club loyalty and retention
* Feed structured insights into the MAPS loop

---

3. The Problem (Why This Exists)

Current Challenges

* Manual WhatsApp handling → slow, inconsistent, unscalable
* Leads go cold due to:
  * Power cuts
  * Network instability
  * Payment delays
  * Daily life interruptions
* No structured capture of customer behavior

👉 Result: Lost revenue + lost intelligence

---

4. RDC Reality (Design Foundation)

Behavioral Reality

* WhatsApp-first communication
* Hybrid language: Lingala + French + Swahili
* Trust = speed + warmth + respect

Environmental Constraints

* Unstable internet (3G/4G)
* Frequent blackouts
* Heavy traffic & mobility

Customer Expectations

* Response time <60 seconds
* Human, friendly tone
* Helpful, non-pushy communication

---

5. System Capabilities

1. Lead Capture & Qualification

* Natural 2–3 question flow
* Context-aware (city, intent, product)

---

2. Nurturing

* Personalized recommendations
* Lab-driven product insights
* Delivery + pricing guidance

---

3. Conversion (Optimized for RDC)

* Seamless ordering flow داخل WhatsApp

Payment Options (Multi-Channel)

* Mobile Money
  * Orange Money
  * Airtel Money
  * M-Pesa
* Bank Transfer
  * Automatic sharing of account details
  * Payment confirmation tracking
* Cash on Delivery (COD)
  * Payment at delivery
  * Payment at Spot pickup

---

Logistics Integration

* Hub routing system:
  * Spot pickup
  * Moto-taxi delivery

---

Automated Actions

* Order confirmation
* Club points credit
* MAPS data capture

---

4. Retention & Reactivation

* Post-purchase follow-ups
* Long-term engagement sequences

---

5. Intelligent Escalation

Triggers

* Voice notes
* Complex complaints
* High-value leads
* 3 unresolved messages

👉 Human takeover in <3 minutes

---

6. Strategic Relance Engine (CORE INNOVATION)

Principle

👉 Help first. Sell second.

---

Rules

* Maximum 3 relances per lead (lifetime)
* Fully automated

---

Timing Logic

1. +24h → 1st relance
2. +48–72h → 2nd relance
3. +7–10 days → final reactivation

👉 Avoid blackout hours (especially evenings in Kinshasa)

---

Message Strategy

1st Relance

* Value + reminder
* No pressure

2nd Relance

* Social proof
* Local relevance
* Small incentive

3rd Relance

* Loyalty reward (Club points)
* Clear opt-out

---

Safeguards

* Short messages (2–3 sentences)
* Easy opt-out: “stop / arrête / non”
* Personalized content
* Respectful tone

---

MAPS Integration

Each relance generates data:

* Response rates
* Timing efficiency
* Content effectiveness

👉 Feeds:
Analyze → Ideate → Prototype → Scale

---

7. System Architecture

Core Stack

* Channel: WhatsApp (Dual-Mode)
  * Development & Testing: Baileys Bridge (Node.js, free — unofficial WhatsApp Web library)
  * Production: WhatsApp Business API (official, paid — $50/month)
* Orchestration: Celery + Celery Beat (Python-native task queue)
* Backend: FastAPI + Claude
* Database: PostgreSQL
* Queue System: Redis
* Dashboard: Streamlit
* Deployment: Docker

> **Dual-Mode Strategy:** During development and testing (Phase 0–1), the system connects to WhatsApp via a self-hosted Baileys Node.js bridge (free, QR-code-based authentication using a test phone number). For production launch, the system switches to the official WhatsApp Business API with the real MBB business number. The switch is configuration-driven (environment variable `WHATSAPP_MODE`) and requires zero code changes to modules M2–M9.

---

8. Future Integration Architecture (MBB HUB, MBB BOX & Digital Presence)

MBB ya Kin is designed to integrate with other MBB ecosystem systems using an **Adapter Pattern** to ensure loose coupling and minimal rework.

**Current Adapters (Phase 1):**
- ✅ WhatsAppAdapter (M1) — messaging channel (dual-mode: Baileys / Official API)
- ✅ AirtableAdapter (M7) — order sync via Celery tasks
- ✅ ClaudeAdapter (M4) — LLM provider
- ✅ MobileMoneyAdapter (M7) — payment processor (Orange/Airtel/M-Pesa)

**Future Adapters (Planned):**
- 🔮 MBBHubAdapter (Phase 2) — Hub CRM integration (customer data, Club points, order routing)
- 🔮 MBBBoxAdapter (Phase 2+) — Inventory sync (real-time stock levels, product catalog, pricing)
- Other adapters will be specified in the future if needed

**Separate but Related Platform:**
- 🔮 Digital Presence Platform — social accounts, paid ads, content publishing, landing pages, and future website/CMS
- This platform is not part of the bot core business logic; it feeds leads into the bot through click-to-WhatsApp, forms, and future web chat/widget entry points

**Design Principle: Zero Code Changes**

When a new integration is needed:
1. Create a new adapter implementing the standard interface
2. Register the adapter in the configuration
3. Existing modules (M2–M9) remain untouched

> This means MBB HUB can replace Airtable for CRM and MBB BOX can provide live inventory — all without modifying the bot's core conversation, qualification, or relance logic.
> Social media management and the future official website belong to the Digital Presence Platform, while the bot remains responsible only for chat and lead-handling interfaces.

---

9. Lead Flow (Simplified)

Stage	Action	Output
Capture	Ads / WhatsApp / QR	Lead created
Qualification	Chatbot questions	Scored lead
Nurturing	Personalized messages + relance	Engagement
Conversion	Order + payment	Sale
Retention	Follow-up + reactivation	Loyalty


---

10. Success KPIs

* First response time: <60s
* Automation rate: 80–85%
* Conversion increase: +30%
* 1st relance response rate: 35–45%
* Opt-out rate: <8%
* New MAPS insights per month

---

11. Design Principles (Non-Negotiable)

1. Human & Cultural Authenticity

* Warm, casual, respectful
* Emoji-friendly
* Locally relevant

---

2. Multi-Language Intelligence

* Auto-detect:
  * Lingala
  * French
  * Swahili

---

3. Low-Bandwidth Resilience

* Text-first approach
* Offline queuing system
* Recovery messaging

---

4. Privacy & Consent

* Clear opt-in
* Minimal data collection
* Easy opt-out

---

5. MAPS-Native Intelligence

* Every interaction = data

---

12. Risks & Mitigation

Risk	Mitigation
Robotic tone	Weekly tone audits
Language errors	French fallback + escalation
Blackouts	Queue + recovery messages
Spam perception	Strict 3-touch rule
High volume	Redis queue + rate limiting


---

13. Stakeholder Impact

Leadership

* Turns operations into structured growth

Hub Team

* Reduced workload
* Focus on high-value interactions

Lab Team

* Continuous real-world insights
* Faster product iteration

Development Team

* Focus on:
  * Tone accuracy
  * System resilience
  * Clean data tagging

---

13. Success Definition

The system succeeds when:

* Customers feel:
👉 “I’m talking to a real person”
* Hub observes:
  * Higher conversions
  * Lower operational load
* Lab receives:
  * Continuous, actionable insights

---

14. Implementation Roadmap

Phase 1 (0–6 months)

* Core chatbot flows
* 3 relance templates
* Basic MAPS tagging
* Pilot with 100–150 leads
* WhatsApp connectivity: Baileys bridge for dev/testing (free), switch to official WhatsApp Business API for production pilot

---

Phase 2 (6–18 months)

* Advanced MAPS pattern recognition
* Voice-note handling
* Dynamic relance timing

---

Phase 3

* Predictive personalization
* Advanced analytics

---

15. Immediate Next Steps (0–30 Days)

1. Deploy WhatsApp chatbot core flows (using Baileys bridge for internal testing)
2. Implement 3 relance templates
3. Integrate payment options (Mobile Money, Bank, COD)
4. Switch WhatsApp mode from Baileys to official Business API for production pilot
5. Launch pilot with real leads on official MBB business number
6. Track relance + conversion KPIs
7. Review first MAPS insights

---

🔥 Final Insight

This system is powerful because it aligns with reality, not theory:

* Works with blackouts
* Speaks like the customer
* Respects attention
* Converts without pressure

👉 That’s why it will win.