Multi-Language Lead Nurturer Bot (“MBB ya Kin”)

Version: 1.0

Date: April 2026

Prepared for: MBB Leadership, Toronto Supervision, Hub, Lab & Development Teams

Executive Summary

This document translates the final Functional & Non-Functional Requirements (v1.1) and the Persuasion & Growth Blueprint into concrete, testable scenarios.

It defines:

* Use Cases (ideal “good” situations) — how the bot must behave to deliver <60 s responses, 80–85 % automation, +30 % conversion, value-first proximity, and MAPS intelligence.
* Misuse Cases (edge, failure, or abusive situations) — how the bot must gracefully handle RDC realities (blackouts, hybrid language, voice notes, spam attempts) without breaking trust or violating NFR12 ethics.
* User Stories — prioritized agile stories that map 1:1 to requirements and blueprint principles (Cialdini, StoryBrand SB7, Blue Ocean ERRC, Hormozi systems).

Purpose: This is the executable layer for Celery tasks, Claude prompts, Redis state machines, MAPS tagging, and testing scripts. Every scenario includes:

* Trigger
* Bot response (with blueprint tie-in)
* MAPS capture
* Success / Failure metrics

All scenarios respect the non-negotiables: short 2–3 sentence messages, emoji-friendly, “help first, sell second,” max-3 relances, easy opt-out, blackout resilience, and weekly tone audits.

---

1. Use Cases (Ideal “Good” Situations)

UC-01: Instant Lead Capture & Qualification (FR1, FR2, FR4)

Trigger: New WhatsApp message from Facebook ad / QR code / flyer (e.g., “Na besoin ya câble 2m renforcé”).

Preconditions: First contact, no prior session.

Steps & Bot Response:

1. <60 s reply (FR1) in detected language (Lingala hybrid): “Salut frère ! Merci pour ton message 😊. Tu es à Kinshasa ou Goma ? Tu cherches le câble 2m renforcé pour pro ou usage perso ?” (Reciprocity + StoryBrand Problem).
2. User answers → 2nd/3rd question → lead scored Hot/Warm/Cold + stored. Blueprint Integration: Reciprocity (free tip if relevant) + SB7 (user = hero, bot = guide). MAPS Capture: Product demand, city, language hybrid, intent. Success Criteria: Lead scored & CRM updated; 100 % first-response <60 s.

UC-02: Personalized Nurturing & Recommendation (FR5)

Trigger: Qualified lead asks about pricing/delivery.

Bot Response: “Parfait ! À Kinshasa avec la poussière et les coupures, le câble renforcé 2m est le plus demandé par les pros à Gombe. Prix : 12 500 FC. Livraison moto-taxi en 48h ou Spot pickup. Tu veux que je t’envoie les détails Mobile Money ?” (Blue Ocean: eliminate delays, raise reliability).

Blueprint Integration: Liking/unity (local reference) + Hormozi value-first.

MAPS Capture: Personalization factors used, conversion trigger.

UC-03: Seamless In-WhatsApp Conversion (FR6)

Trigger: User says “oui” to order.

Bot Response: Confirms details → lists payments (Orange Money first) → auto-sends account details if bank → triggers Hub routing → confirmation + Club points credited.

Blueprint Integration: SB7 Call-to-Action + Grand Slam Offer (“stupid to say no”).

Success Criteria: Order routed, points credited, confirmation sent.

UC-04: Strategic Relance (FR7)

Trigger: 24 h silence on Hot lead.

1st Relance: “Hey, j’espère que tout va bien malgré la coupure 😊. Comme promis voici le prix actualisé + témoignage d’un client à Limete qui a reçu en 48h.”

2nd/3rd: Social proof → scarcity → loyalty reward + opt-out link.

Blueprint Integration: Cialdini (reciprocity → social proof → scarcity) + Hormozi rhythm.

Success Criteria: 35–45 % response on 1st relance; <8 % opt-out lifetime.

UC-05: Blackout Resilience & Recovery (FR10)

Trigger: Message queued during outage → connectivity returns.

Bot Response: “Je suis toujours là même si le courant est coupé 😊 — voici ce que je t’avais promis sur le câble renforcé.”

Blueprint Integration: Reinforces reciprocity + unity (“we’re in this together”).

MAPS Capture: Blackout-related silence reason.

UC-06: Intelligent Escalation (FR9)

Trigger: Voice note received or 3 unresolved messages.

Bot Response: “Je viens d’entendre ta note vocale — je transmets immédiatement à mon collègue humain pour te répondre en <3 min. Merci pour ta patience !” + full transcript + tags sent to Hub.

Success Criteria: Human takeover <3 min.
UC-07: MBB HUB CRM Integration (FR13 — Phase 2)

Trigger: Lead qualifies or order is placed → synchronization event with MBB HUB.

Preconditions: MBBHubAdapter is registered and configured (replaces AirtableAdapter).

Steps & Bot Response:
1. Lead qualified → MBBHubAdapter pushes lead record to HUB CRM (customer profile, score, product interest, MAPS tags).
2. Order created → MBBHubAdapter syncs order details to HUB (items, payment type, delivery zone, Club points).
3. Club points credited via HUB API → bot confirms: "Tes points Club sont mis à jour 🎉"
4. Escalation → MBBHubAdapter sends full transcript + tags directly to HUB Team queue.

Blueprint Integration: Seamless ecosystem handoff — customer never feels the technology underneath.

MAPS Capture: crm_sync_success, hub_latency, points_credited.

Success Criteria: Zero data loss between bot and HUB; sync latency < 5s; fallback to Airtable if HUB unreachable.

UC-08: MBB BOX Inventory Sync (FR14 — Phase 2+)

Trigger: Customer asks about product availability, pricing, or delivery timing.

Preconditions: MBBBoxAdapter is registered and configured.

Steps & Bot Response:
1. Customer asks: "Eza na câble 2m ?" → MBBBoxAdapter queries real-time stock for customer's city.
2. BOX responds: in_stock = true, quantity = 45, price = 15,000 FC → bot says: "Oui ! Le câble 2m est disponible à Gombe (stock: 45 pièces). Prix: 15,000 FC. Tu veux commander ? 😊"
3. If out of stock → bot says: "Le câble 2m n'est pas disponible à Gombe en ce moment. Je te préviens dès que le stock revient ? 📱"
4. BOX pricing update → nurturing messages use live prices instead of static configuration.

Blueprint Integration: Real-time availability creates urgency (Cialdini scarcity) when stock is genuinely low.

MAPS Capture: product_stock_query, out_of_stock_event, dynamic_pricing_used.

Success Criteria: Live stock data in nurturing; graceful fallback to static catalog if BOX unreachable; no stale pricing in messages.
---

2. Misuse Cases (Bad / Edge / Abusive Situations & Required Bot Behavior)

MC-01: Blackout Mid-Conversation (FR10, NFR2)

Misuse: User messages during outage → conversation drops → user sends follow-up after restore.

Required Response: Queue + resume exact context (no re-qualification). Send recovery message. Do not reset lead score.

Blueprint: SB7 Plan continuity + reciprocity (re-state promised value).

MAPS: Tag “blackout_mid_flow”.

MC-02: Hybrid / Ambiguous Language (FR2)

Misuse: Message mixes Lingala + Swahili + French emojis.

Required Response: Auto-detect dominant language + hybrid reply; never default to robotic French-only. If uncertain → French + “Dis-moi si tu préfères Lingala ?”

Blueprint: Unity (mirror user style).

Implication: Prevents 5–10 % drop-off in Kinshasa.

MC-03: Voice Note or Media Spam (FR1, FR9)

Misuse: User sends repeated voice notes or images.

Required Response: Immediate escalation + polite reply: “Je ne peux pas traiter les notes vocales directement — je transmets à mon collègue humain tout de suite 😊”.

MAPS: Tag “voice_note_escalation”.

MC-04: Opt-Out or Stop Attempts (FR7, FR12, NFR12)

Misuse: User sends “stop”, “arrête”, “non”, or Lingala equivalent (“tika”).

Required Response: Instant stop of all relance + communication. Log consent withdrawal. Offer data deletion if requested.

Blueprint: Ethics guardrail (never push after opt-out).

Success: 100 % compliance; no further messages.

MC-05: Repeated / Spam-Like Messages from Same User (NFR1, NFR10)

Misuse: High-frequency messages (e.g., 10+ in 5 min).

Required Response: Redis rate limit → short polite reply: “Je suis là pour t’aider, mais prenons un message à la fois 😊. Qu’est-ce que je peux faire pour toi maintenant ?” + flag for Hub review.

MAPS: Tag “potential_spam_attempt”.

MC-06: Complex Complaint or High-Value Lead During Outage (FR9, FR10)

Misuse: SAV issue + blackout.

Required Response: Queue escalation; on restore send “Je suis toujours là… ton SAV est prioritaire, je transmets maintenant”.

Implication: Protects reputation during RDC infrastructure stress.

MC-07: Robotic Tone Drift Detected (NFR5)

Misuse: MAPS or tone audit flags unnatural phrasing.

Required Response: System auto-logs + pauses non-critical replies until Lab updates Master Claude Prompt. User sees warm fallback.

MC-08: Data Deletion Request (FR12)

Misuse: User says “supprime mes données”.

Required Response: Confirm, delete minimal records, confirm deletion. No further contact.

---

3. User Stories (Agile Format – Prioritized for Phase 1)

Priority 1 – Core Interaction (Must be in 0–6 months pilot)

US-01: As a new Congolese lead (Kinshasa/Goma), I want an instant <60 s reply in my language so I don’t lose interest during traffic or blackout.

(FR1, FR2, NFR1 – Cialdini Reciprocity + SB7 Problem)

US-02: As a price-sensitive pro, I want 2–3 quick questions + personalized recommendation so I feel understood and helped immediately.

(FR4, FR5 – Blue Ocean value innovation)

US-03: As a buyer ready to order, I want to complete the full order (payment + Hub routing + Club points) inside WhatsApp without leaving the chat.

(FR6 – Hormozi Grand Slam Offer)

US-04: As a silent lead, I want up to 3 value-first relances (never pushy) so I can re-engage without feeling chased.

(FR7 – Cialdini + Hormozi rhythm)

Priority 2 – Resilience & Intelligence

US-05: As a user during blackout, I want the bot to remember our conversation and greet me with the recovery message when power returns.

(FR10 – Unity + reciprocity)

US-06: As the Lab team, I want every interaction automatically tagged with MAPS data (product demand, silence reason, persuasion trigger) so we can iterate prototypes faster.

(FR8 – Core MAPS loop)

US-07: As the Hub team, I want automatic escalation with full transcript + tags when voice note or complex case appears so I can take over in <3 min.

(FR9)

Priority 3 – Compliance & Extensibility

US-08: As a privacy-conscious user, I want clear consent, easy opt-out, and data deletion so I trust MBB.

(FR12, NFR12)

US-09: As the Development team, I want modular flows so we can add Telegram or web widget later without rewriting core logic.

(NFR8)

US-10: As Toronto Supervision, I want a dashboard with persuasion lift metrics and weekly tone-audit scores so we can prove the bot stays human and effective.

(FR11, NFR5)

Priority 4 – Ecosystem Integration (Phase 2 / Phase 2+)

US-11: As the Hub team, I want the bot to sync leads, orders, and Club points directly to MBB HUB CRM so I don't have to manually transfer data from Airtable.

(FR13 — MBBHubAdapter, Adapter Pattern)

US-12: As a customer asking about product availability, I want the bot to check real-time MBB BOX stock so I get accurate info on pricing and delivery instead of outdated data.

(FR14 — MBBBoxAdapter, Adapter Pattern)

US-13: As the Development team, I want new integrations (HUB, BOX, or future systems) to be plugged in via adapters with zero code changes to conversation, qualification, or relance logic.

(NFR8 — Extensibility, Adapter Pattern)

---

Traceability Matrix Summary

Every Use/Misuse Case and User Story maps directly to:

* Specific FR / NFR
* Blueprint principle (Cialdini / SB7 / ERRC / Hormozi)
* Success KPI (conversion, automation rate, opt-out <8 %, MAPS insights/month)

Testing Recommendations

* Positive Tests: 100–150 pilot leads covering UC-01 to UC-06.
* Negative / Edge Tests: Simulate blackouts, voice notes, spam, opt-outs.
* Tone Audit: Lab reviews 10 % of all scenarios weekly against “proximity” checklist.
* Tools: Celery task tests + Claude prompt variants + Redis blackout simulator.

Risks Mitigated by These Scenarios

* Loss of trust during outages → recovery message + context resume.
* Cultural friction → hybrid language + short friendly replies.
* Spam perception → strict rate limits + max-3 relances.
* Intelligence leak → automatic MAPS tagging on every path.

This document is now the single source of truth for conversation design, testing, and MAPS feedback. It closes the loop between theory (blueprint) and execution (requirements v1.1).