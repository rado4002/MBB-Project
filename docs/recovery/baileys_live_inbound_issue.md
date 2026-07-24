# Baileys Live Inbound Issue — Step 12 Recovery Note

> **Closure addendum (current baseline `f45a45d49f79d4c05d1d1be1253c8ba7ab11bedc`):** This document preserves the historical Step 12 investigation and its then-current unresolved findings. Those findings are no longer the current Baileys status. Subsequent controlled validation passed live inbound, session restoration, international phone handling, persistence, and exactly-one outbound fallback delivery. Baileys is now the validated local WhatsApp transport, and its recovery is closed for the controlled inbound-to-fallback-send scope. Earlier “unresolved” statements below remain only as chronological evidence. Because Baileys is an unofficial transport, this closure is not permanent production approval or proof of public-deployment suitability.

## 1. Purpose

This document records the full Baileys live inbound investigation from **Step 12A through Step 12W** before the project moved to **Step 13A — Core MVP Message Pipeline Closure**.

The purpose is to preserve the technical history of the Baileys issue so future recovery work can resume from known evidence instead of repeating the same investigation.

At the close of Step 12, this note did **not** claim that Baileys live inbound was fixed. The closure addendum above records the subsequent controlled success.

It documents:

```text
What was discovered
What was fixed
What was unresolved at Step 12 close
What was proven
What was not proven
Why Step 12 was stopped
How future Baileys work should resume
```

The project remains in **recovery and stabilization mode**. Recovery and local stabilization are now nearly complete, but the project is **not publicly deployed**, **not production-ready**, and **not pilot-ready**.

---

## 2. Executive Summary

Step 12 began as a controlled attempt to prove the real WhatsApp/Baileys message path.

During the investigation, the problem was narrowed through several layers:

```text
runtime wiring
→ webhook secret mismatch
→ Celery async loop failures
→ outbound send risk
→ celery_beat side-effect risk
→ Baileys session/logout inconsistency
→ @lid sender handling
→ safe senderPn mapping
→ payload extraction
→ final no-payload stub event
```

The Step 12 classification at that time was:

```text
Core backend message pipeline:
Works when a valid normalized +243 webhook payload reaches FastAPI.

Baileys live inbound adapter:
Partially working, but not reliable enough to close controlled confirmed content-bearing validation.

Known sender +243***221:
Baileys receives the event as @lid and maps it safely through senderPn to a valid +243 identity, but the event contains no msg.message payload.

Final live blocker:
has_message=false
message_keys=[]
messageStubType=2
no extractable text
event skipped before FastAPI
```

Therefore, Step 12 was stopped as an open-ended Baileys live-adapter investigation. The project moved to **Step 13A**, focused on validating the **core backend MVP pipeline at webhook level**, while Baileys live inbound was temporarily isolated as an adapter reliability risk.

---

## 3. Final Step 12 Classification

| Area                      | Classification                                                         |
| ------------------------- | ---------------------------------------------------------------------- |
| API health                | Working                                                                |
| Celery worker             | Running                                                                |
| `celery_beat`             | Must remain stopped during recovery tests                              |
| `WHATSAPP_SEND_ENABLED`   | `false`; real outbound disabled                                        |
| Core webhook pipeline     | Working with valid fake/internal +243 payload                          |
| M1 task                   | Working after async fixes                                              |
| MAPS task                 | Working after async fixes                                              |
| DB persistence            | Working for valid payloads                                             |
| Dashboard/API visibility  | Previously validated for seeded and backend-visible messages           |
| Baileys live inbound      | Blocked/inconclusive at Step 12 for controlled content-bearing evidence |
| Known sender `+243***221` | Maps through `senderPn`, but arrives as stub/no-payload                |
| Fully stabilized project  | Not yet                                                                |

---

## 4. Full Step 12 Timeline

### Step 12A — Runtime Configuration and Webhook Secret Alignment

The first Baileys blocker was runtime configuration, not message content.

At this stage, Baileys was running and exposing health/QR behavior, but the backend and Baileys were not fully aligned.

Initial problem:

```text
Baileys had webhook secret configuration.
API runtime showed baileys_webhook_secret_set=false.
Safe authenticated fake probe returned 401 invalid_webhook_secret.
```

After recreating the stack with the correct development Compose files, the Baileys webhook secret and API secret aligned.

Result:

```text
Fake Baileys webhook reached FastAPI.
FastAPI returned 202 Accepted.
Celery received M1 task.
```

That exposed a new blocker:

```text
M1 failed with async event-loop error:
Future attached to a different loop.
```

Classification:

```text
Webhook routing/secret alignment: fixed.
Backend async task execution: broken.
```

---

### Step 12B — M1 Async Event Loop Fix

The next issue was inside Celery.

M1 used async code inside a long-running Celery worker. The earlier execution pattern created async event-loop conflicts with SQLAlchemy/asyncpg resources.

Root cause boundary:

```text
FastAPI accepted the webhook.
Celery received the M1 task.
M1 failed during async DB/service execution.
```

A per-worker async execution helper was introduced in `backend/app/tasks/m1.py`, replacing unsafe repeated `asyncio.run(...)` usage.

Validated result:

```text
Fake voice-note webhook accepted.
Customer/conversation/message created.
Voice-note escalation ticket created.
M1 completed successfully.
```

Baileys was disconnected at this time, so no real WhatsApp send occurred.

Classification:

```text
M1 async execution: fixed for tested path.
Real Baileys live path: not tested yet.
```

---

### Step 12C — Fake Text Path and Outbound Safety Discovery

After M1 worked for the fake voice-note path, a fake text webhook was tested.

Validated result:

```text
Fake text webhook returned 202.
M1 succeeded.
Inbound message persisted.
Local outbound fallback message persisted.
```

This test revealed two important facts.

First, the configured AI-generation path returned:

```text
Claude API 401 invalid x-api-key.
```

This was expected for that runtime because a valid model API key was not configured. The system used the local fallback technical-error response.

Second, Baileys was disconnected, so real outbound send attempts failed safely with `503`.

This showed that once Baileys was connected, outbound sending could become dangerous unless explicitly gated.

The fake text test also exposed another downstream async issue:

```text
MAPS task failed with async event-loop error.
```

Classification:

```text
Fake text M1 path: working.
Fallback response path: working.
AI generation: unavailable due invalid/missing API key.
MAPS async execution: broken.
Outbound safety: needed before real pairing.
```

---

### Step 12D — MAPS Async Event Loop Fix

The MAPS task had the same async event-loop class of problem as M1.

A similar per-worker async loop fix was applied to `backend/app/tasks/maps.py`.

Validated result:

```text
Fake text webhook passed.
M1 succeeded.
MAPS tag task succeeded.
```

Classification:

```text
Backend fake webhook → M1 → MAPS path: working.
```

---

### Step 12E — Wider Celery Async Audit

After fixing M1 and MAPS, Codex audited other Celery task modules and found more unsafe `asyncio.run(...)` usage.

Affected modules included:

```text
conversion.py
escalation.py
m5.py
qualification.py
relance.py
```

A shared async helper was added in:

```text
backend/app/tasks/celery_app.py
```

Task modules were updated to use it.

Validated results included:

```text
M1/MAPS regression passed.
Qualification was validated after shared loop fix.
Conversion queue drain completed safely.
```

Some modules, such as relance, escalation, and M5, were not deeply exercised because of side-effect risk.

Remaining issue:

```text
Some Celery setup tests still expected old relance task naming/import behavior.
```

Classification:

```text
Core async task execution: improved.
Some side-effect task areas: still risky / not fully exercised.
```

---

### Step 12F — Relance Import Compatibility and Side-Effect Risk Inventory

A narrow compatibility export was added:

```text
schedule_next_relance = scan_eligible_leads
```

This helped task imports while avoiding enabling unsafe relance behavior.

A side-effect risk inventory was created.

Risk areas included:

```text
relance automation
escalation reminders
conversion/payment/CRM-related tasks
M5 CRM activity
```

Key decision:

```text
Do not start celery_beat during controlled Baileys live tests.
Keep outbound sending disabled.
Treat side-effect governance as part of Baileys live-testing safety.
```

Classification:

```text
Task import compatibility: improved.
Side-effect automation: must remain controlled.
celery_beat: should stay stopped during recovery testing.
```

---

### Step 12G — Controlled Pairing Safety Gate

Before real phone pairing, Codex stopped `celery_beat` and verified queue safety.

Runtime safety state:

```text
celery_beat stopped/exited.
celery_worker running.
Redis queues empty.
API healthy.
Baileys ready for controlled pairing.
```

This step showed why `celery_beat` had to remain stopped: historical logs showed side-effect-capable scheduled tasks, including escalation reminders, could fire if beat was active.

Classification:

```text
Safe pairing state established.
celery_beat must remain stopped.
```

---

### Step 12H — First Real Phone Pairing and Live Inbound Discovery

The phone was paired with Baileys. The user confirmed WhatsApp Linked Devices contained the Baileys session, and Baileys reported connected.

Codex confirmed Baileys was not doing full history import:

```text
syncFullHistory: false
No history mirroring/import path implemented.
```

Baileys listened primarily to:

```text
messages.upsert
```

and skipped or ignored:

```text
fromMe messages
non-notify events
status/broadcast events
unsupported events
```

During this phase, at least one real inbound text reached the backend and triggered M1. Because the outbound safety gate was not yet fully enforced, the system sent one real fallback reply.

Classification:

```text
Real Baileys inbound can work for some events.
Real outbound can happen if not gated.
Hard outbound safety gate required before more live testing.
```

---

### Step 12I — Outbound Safety Gate and Baileys Filtering

A hard outbound safety flag was added:

```text
WHATSAPP_SEND_ENABLED=false
```

When disabled, the adapter logs a skip and does not call the real Baileys `/send` endpoint.

Baileys filtering/masking was also improved:

```text
skip status@broadcast
skip group messages
skip unsupported @lid at the time
skip unsupported JID domains
skip unsupported payload types
skip fromMe
mask phone/JID logging
```

Validated behavior:

```text
Fake webhook still passed.
Inbound/local outbound persistence still worked.
Real WhatsApp outbound send skipped by safety gate.
```

This created the safe live-testing state used later:

```text
Baileys connected.
WHATSAPP_SEND_ENABLED=false.
celery_beat stopped.
celery_worker running.
No real outbound sends.
```

Classification:

```text
Outbound safety gate: working.
Live testing became safer.
```

---

### Step 12J — Bad MAC / Decryption Noise and Extended Text Classification

During early live testing, Baileys showed Bad MAC/session/decryption noise.

This suggested one or more of the following:

```text
stale/corrupted session state
sender/device-specific decrypt issue
multi-device/Baileys instability
non-content event behavior
```

A narrow bug was also found:

```text
extendedTextMessage.text was extracted but classified as "other".
```

This was corrected so extended text could be treated as text.

Sensitive Baileys/libsignal session/decryption logs were also filtered more carefully.

Classification:

```text
Some Baileys messages/events could process.
Some produced decrypt/session noise.
Adapter reliability still uncertain.
```

---

### Step 12K–12L — +243 Test Confusion and First Clear @lid Blocker

There was confusion about whether a controlled `+243` test message had actually been sent during Codex’s live watch window.

Later, when the user confirmed sending from a `+243` number, Codex observed a new inbound event as:

```text
@lid
```

At that time, the bridge did not support LID mapping, so it skipped the event before FastAPI.

Result:

```text
Baileys received the event.
Event appeared as @lid.
Bridge skipped it with lid_mapping_unsupported.
No FastAPI webhook.
No Celery task.
No DB row.
No dashboard visibility.
```

This was the first clear version of the later LID issue.

Classification:

```text
Failure layer narrowed to Baileys-side @lid handling before FastAPI.
```

---

### Step 12M — Graceful Logout Failure

The user disconnected the linked device from the phone, but Baileys still showed connected on the computer.

Codex attempted graceful logout:

```text
POST http://127.0.0.1:3000/logout
```

Result:

```text
HTTP 500
{"error":"Connection Closed"}
Baileys still connected=true.
QR unavailable.
```

This showed that the existing logout/reset path was not reliable when the socket/session was already closed or inconsistent.

Classification:

```text
Baileys session lifecycle broken.
Graceful logout/reset not reliable.
Manual session reset needed.
```

---

### Step 12N — Session-Only Reset

Codex removed only the confirmed Baileys session Docker volume:

```text
bot_baileys_session
```

It did not remove:

```text
Postgres volumes
Redis volumes
backend volumes
dashboard volumes
other unrelated volumes
```

Result:

```text
Old Baileys session cleared.
Fresh QR generated.
Baileys connected=false.
QR available.
No files edited.
No backend data lost.
```

Classification:

```text
Baileys stale session state isolated to Baileys session storage.
Session-only reset works.
```

---

### Step 12O — Fresh Re-Pair Verification

The phone was paired again through the fresh QR.

Validated state:

```text
Baileys connected=true.
API healthy.
Celery worker running.
celery_beat stopped.
WHATSAPP_SEND_ENABLED=false.
Queues empty.
No Bad MAC/decryption noise immediately after fresh pairing.
```

Classification:

```text
Cleanest Baileys live state so far.
Ready for guarded inbound testing.
```

---

### Step 12P — Practical Real Inbound Success, but Not Clean Controlled Evidence

Codex did not find the exact planned phrase, but it found real `+243` inbound messages that had been processed.

Observed practical flow for at least one real inbound message:

```text
Baileys inbound_message
→ API POST /api/v1/messages/baileys returned 202
→ Celery m1.process_inbound_message succeeded
→ DB inbound stored
→ DB local outbound fallback stored
→ real WhatsApp send skipped by WHATSAPP_SEND_ENABLED=false
```

This showed that live Baileys could work for some real messages.

However, the exact planned phrase was not found. Therefore, it was not accepted as clean controlled evidence.

This led to the decision to use the **Confirmed Observed Inbound Message Test** instead of requiring a fixed phrase.

Classification:

```text
Practical live inbound evidence exists.
Formal controlled evidence not yet achieved.
Confirmed Observed Inbound method introduced.
```

---

### Step 12Q — Baileys Session Lifecycle and Logout Reliability Fix

The Baileys UI and phone-side linked device state remained confusing. The page could show connected while also showing bridge retry behavior.

Codex implemented a narrow lifecycle fix in:

```text
baileys/src/index.js
```

Changes included:

```text
explicit connectionState
currentJid tracking
reconnect timer
reset promise
connection generation guard
idempotent /logout
state reset on loggedOut/closed-session conditions
session directory contents cleanup
fresh QR generation after reset
QR/session dump suppression
UI no longer showing stale connected state during polling failure
```

Validated result:

```text
POST /logout returned 200.
Baileys connected=false.
state=qr.
QR available.
No JID present.
```

Classification:

```text
Logout/reset lifecycle improved.
Stale connected state reduced.
```

---

### Step 12R — Confirmed Observed Inbound Message Test and @lid Failure

The team switched to the Confirmed Observed Inbound Message Test:

```text
Codex watches logs.
User sends one message.
Codex reports what it observed.
User confirms whether it was the intended message.
Only then is it traced.
```

Observed events:

```text
@lid events
lid_mapping_unsupported
not forwarded to FastAPI
no text extracted
no DB record
```

The user clarified that the sender was actually a real `+243` number, not a `+256` number. The masked LID prefix was not a phone number.

Classification:

```text
Failure layer: @lid mapping.
Important correction: @lid value is internal WhatsApp identity, not phone number.
```

---

### Step 12S — Known +243 Sender LID Mapping Diagnosis

Codex inspected Baileys capabilities and found that Baileys exposes possible phone-number JID fields:

```text
senderPn
participantPn
senderLid
participantLid
```

Important finding:

```text
If future @lid message has senderPn or participantPn,
that may be treated as Baileys-provided phone-number JID.
```

Already skipped events could not be safely mapped retroactively because the bridge skipped them before logging key metadata.

Safe policy was established:

```text
Do not guess phone numbers from @lid.
Do not store unresolved @lid.
Do not forward unresolved @lid to FastAPI.
Do not weaken +243 backend validation.
```

Classification:

```text
Retroactive mapping impossible.
Future safe mapping possible if senderPn/participantPn present.
```

---

### Step 12T — Narrow LID Mapping Patch

Codex implemented a narrow mapping patch in:

```text
baileys/src/index.js
```

Changes included:

```text
normalizePnJid
resolveInboundSenderJid
phoneFromPnJid
isDrcPhone
```

New behavior:

```text
@lid message arrives
→ check senderPn / participantPn
→ if valid +243 PN-JID exists, proceed
→ if missing, skip as lid_mapping_unavailable
→ if non-DRC, skip as lid_pn_not_drc
```

No USync guessing was added.

Fake webhook regression passed:

```text
API returned 202.
M1 succeeded.
MAPS succeeded.
DB stored inbound/outbound.
Real WhatsApp send skipped.
```

Live attempt result:

```text
Message reached Baileys as @lid.
Mapping gate did not reject it.
But payload type was other.
No conversation.
No extendedTextMessage.text.
Not forwarded to FastAPI.
```

Classification:

```text
@lid identity handling improved.
New failure layer: payload/text extraction.
```

---

### Step 12U — Safe Wrapped Text Extraction Patch

Codex added safe message unwrapping and text extraction in:

```text
baileys/src/index.js
```

Supported extraction:

```text
conversation
extendedTextMessage.text
```

Supported wrappers:

```text
ephemeralMessage.message
viewOnceMessage.message
viewOnceMessageV2.message
deviceSentMessage.message
editedMessage.message
```

Live confirmed result:

```text
Remote JID: @lid
pn_source: senderPn
Mapping passed as valid +243
Detected type: other
Wrappers: []
Safe message keys: []
Text extraction failed
Not forwarded to FastAPI
```

Classification:

```text
Safe wrapper extraction improved.
Known sender still produced no extractable message content.
```

---

### Step 12V — Redacted Payload Shape Diagnostic

Codex added a redacted structural diagnostic for unsupported payloads.

It logged only safe structure:

```text
message presence
message key count
messageStubType presence
protocol/media/reaction/contact/location/wrapper booleans
senderPn/participantPn presence
JID domain only
```

Live confirmed result:

```text
senderPn present
participantPn absent
mapping passed to valid +243
has_message=false
message_keys=[]
message_keys_count=0
has_messageStubType=true
messageStubType=2
has_messageStubParameters=true
no protocol/media/reaction/contact/location/wrapper fields
```

No raw text, raw JIDs, QR, auth, or session files were intentionally logged. One Baileys internal session material exposure happened during live validation before stream-level suppression was tightened; suppression was improved afterward.

Classification:

```text
Immediate blocker proven:
valid sender mapping exists, but Baileys event has no msg.message payload.
```

---

### Step 12W — Final Limited Baileys Live Attempt and Classification

Step 12W was run as runtime validation only. No files were edited.

Attempt 1:

```text
Sender/JID: masked @lid
senderPn: present
participantPn: absent
has_message=false
message_keys=[]
messageStubType=2
Text extracted: no
Forwarded to FastAPI: no
User confirmed this was the sent message
```

Attempt 2:

```text
First observed event was status_broadcast.
No one-to-one content-bearing event appeared during remaining watch.
```

Separate masked logs showed unconfirmed content-bearing `+243` activity processed by M1/MAPS, but because it was not captured through the confirmed observed method, it was not counted as Step 12W acceptance evidence.

Final classification:

```text
Controlled Baileys live inbound validation:
blocked/inconclusive in this environment.

Known +243***221:
mapped through senderPn, but delivered as stub/no-payload.

Step 12 should stop.
Baileys live inbound should be isolated as adapter risk.
Core MVP recovery should continue at webhook/backend level in Step 13A.
```

---

## 5. Confirmed Facts

### 5.1 Runtime and Backend Facts

```text
API can accept valid Baileys-style webhook payloads.
API returns 202 for valid controlled +243 payloads.
Celery worker receives M1 tasks.
M1 succeeds after async loop fixes.
MAPS succeeds after async loop fixes.
DB stores inbound and local outbound rows for valid payloads.
Real outbound send is skipped when WHATSAPP_SEND_ENABLED=false.
```

### 5.2 Safety Facts

```text
celery_beat can trigger side-effect-capable tasks and should remain stopped during recovery tests.
WHATSAPP_SEND_ENABLED=false prevents real WhatsApp outbound sends.
Unresolved @lid messages should not be forwarded to FastAPI.
Backend +243 validation should not be weakened.
```

### 5.3 Baileys Facts

```text
Baileys session state can become stale/inconsistent.
Old /logout behavior could fail with 500 Connection Closed.
Session-only reset of bot_baileys_session produced fresh QR.
Lifecycle patch improved logout/reset behavior.
Baileys can produce @lid inbound events.
@lid value must not be interpreted as a phone number.
senderPn can be present and can map to valid +243 PN-JID.
Known +243***221 mapped through senderPn.
Known +243***221 confirmed events had has_message=false and messageStubType=2.
No text extraction was possible for those confirmed events.
```

---

## 6. What Was Fixed During Step 12

### Backend and Celery Fixes

```text
M1 async event-loop handling fixed.
MAPS async event-loop handling fixed.
Wider Celery async helper introduced.
Several task modules moved away from unsafe asyncio.run pattern.
Relance import compatibility improved.
```

### Safety Fixes

```text
WHATSAPP_SEND_ENABLED=false added/enforced.
Real outbound sends skipped by safety gate.
celery_beat kept stopped during live testing.
Side-effect queues monitored.
Sensitive phone/JID logging reduced.
```

### Baileys Fixes

```text
Webhook secret/runtime alignment fixed.
Baileys logout/session lifecycle improved.
Idempotent logout/reset added.
Fresh QR handling improved.
Stale connected state reduced.
QR/session/auth-sensitive logs suppressed.
@lid mapping through senderPn/participantPn added.
Only valid +243 PN-JID may proceed.
Unresolved/non-DRC LID remains blocked.
Safe wrapped text extraction added.
Redacted payload diagnostics added.
```

---

## 7. What Was Unresolved at Step 12 Close

The main unresolved Baileys issue at that time was:

```text
Why does the known +243***221 sender produce a mapped @lid event with no msg.message payload?
```

Open questions at that time:

```text
Does a content-bearing event arrive later through messages.update?
Is messageStubType=2 a placeholder/lifecycle event?
Is this caused by Baileys version behavior?
Is it caused by WhatsApp multi-device/LID behavior?
Is it caused by session/decryption/device-specific behavior?
Would a different paired phone/device behave differently?
Would another Baileys version expose content correctly?
Would WhatsApp Cloud API avoid this class of problem?
```

Not resolved during Step 12:

```text
Controlled confirmed content-bearing live evidence from Baileys.
Reliable live inbound behavior for known +243***221.
Full Baileys production suitability.
```

---

## 8. Root Cause Boundary

### Confirmed Immediate Failure Layer

```text
Baileys delivered an inbound event with valid senderPn mapping but no msg.message payload.
```

### Not Proven

```text
Exact internal root cause inside Baileys/WhatsApp.
Whether messageStubType=2 is caused by sync, placeholder, decryption, LID, device behavior, or version behavior.
Whether messages.update later contains the missing text.
```

### Confirmed Non-Causes for the Controlled Failure

```text
Not a backend +243 validation issue.
Not a Celery/M1 issue.
Not a DB issue.
Not a dashboard issue.
Not a safety gate issue.
Not caused by assuming the sender was +256.
Not caused by unresolved LID after senderPn mapping was added.
Not caused by missing simple wrapper extraction after Step 12U.
```

---

## 9. Expected vs Actual Message Lifecycle

### Expected Path

```text
WhatsApp message
→ Baileys messages.upsert with text payload
→ sender resolved to valid +243
→ text extracted
→ FastAPI /api/v1/messages/baileys returns 202
→ Celery M1 processes
→ DB stores inbound message
→ local outbound response stored
→ MAPS tags event
→ real WhatsApp outbound skipped by WHATSAPP_SEND_ENABLED=false
→ API/dashboard visibility confirmed
```

### Actual Path for Known `+243***221`

```text
WhatsApp message
→ Baileys messages.upsert as @lid
→ senderPn present
→ valid +243 mapping
→ has_message=false
→ message_keys=[]
→ messageStubType=2
→ no conversation
→ no extendedTextMessage.text
→ no supported wrapper
→ skipped before FastAPI
```

---

## 10. Safety Decisions

These decisions should remain active until explicitly changed:

```text
Do not guess phone numbers from @lid.
Do not forward unresolved @lid to FastAPI.
Do not store @lid as customer phone.
Do not weaken backend +243 validation.
Do not enable real WhatsApp outbound sends during recovery.
Do not start celery_beat during controlled message tests.
Do not continue open-ended Baileys debugging inside core recovery.
Do not expose raw phone numbers, raw JIDs, QR, session/auth material, message IDs, tokens, or secrets.
```

---

## 11. Known Good Evidence

### Backend/Webhook Evidence

```text
Valid fake/internal +243 webhook returns 202.
M1 receives and succeeds.
MAPS receives and succeeds.
DB stores inbound and local outbound rows.
Real WhatsApp send skipped by WHATSAPP_SEND_ENABLED=false.
```

### Practical Baileys Evidence

```text
Some real +243 inbound messages appeared to process through Baileys/API/M1/MAPS.
One earlier live inbound triggered real reply before the safety gate, proving the adapter can send when connected.
Separate unconfirmed content-bearing +243 activity was seen in broader logs and processed.
```

Important limitation:

```text
Those practical live events were not accepted as final Step 12 controlled evidence because they were not captured through the confirmed observed method.
```

---

## 12. Known Bad / Blocked Evidence

```text
Known +243***221 repeatedly produced @lid stub/no-payload events.
Confirmed events mapped through senderPn but had has_message=false.
No text could be extracted.
No FastAPI payload was created.
No M1 task ran.
No DB/dashboard record was expected.
Attempt 2 in Step 12W saw status_broadcast, then no one-to-one content-bearing event.
Controlled confirmed content-bearing evidence was not achieved.
```

---

## 13. Why Step 12 Was Stopped

Step 12 provided enough evidence to classify the problem.

Continuing to patch Baileys inside the main recovery path risked wasting time and blocking the rest of the project.

The engineering decision was:

```text
Stop open-ended Baileys live debugging.
Document the adapter risk.
Move to Step 13A to validate the core backend MVP pipeline at webhook level.
```

This does not mean Baileys is abandoned. It means Baileys becomes a separate future adapter-reliability track.

---

## 14. Future Baileys Repair Options

Future Baileys-specific work may investigate:

```text
messages.update lifecycle events
messageStubType=2 meaning and handling
whether content-bearing event follows the stub later
Baileys auth/session/decryption behavior
Baileys version comparison, especially 6.7.16 vs 6.7.23
different paired WhatsApp account/device
known content-bearing sender testing
USync/LID-to-PN investigation only if safe and proven
WhatsApp Cloud API as production-grade adapter
keeping Baileys as dev/experimental adapter only
```

Future work must preserve these rules:

```text
Do not guess phone numbers.
Do not weaken backend validation.
Do not forward unresolved LID.
Do not store raw LID as customer identity.
Do not expose private session/auth data.
```

---

## 15. Recommended Future Baileys-Specific Step

Future step name:

```text
Step B1 — Baileys Event Lifecycle Investigation for Stub/No-Payload LID Events
```

Suggested scope:

```text
Inspect messages.upsert and messages.update lifecycle.
Track messageStubType=2 structurally.
Check whether content appears after the stub.
Compare Baileys versions.
Investigate session/decryption signals.
Do not forward unresolved events.
Do not touch backend validation.
Do not enable real outbound.
```

This future step is **not** a Step 13A blocker.

---

## 16. Decision Before Step 13A

Final decision:

```text
Step 12 is closed by classification, not by full Baileys success.
Baileys live inbound was classified as an adapter risk at this decision point.
Core backend MVP recovery continues with Step 13A.
```

This block records the historical decision before Step 13A; it is superseded by the closure addendum at the top of this note.

Step 13A should validate:

```text
FastAPI Baileys webhook
→ Celery M1
→ DB inbound persistence
→ local outbound response persistence
→ MAPS
→ API/dashboard visibility
→ real outbound skipped by WHATSAPP_SEND_ENABLED=false
```

without continuing live Baileys debugging.

---

## 17. Appendix — Full Masked Evidence Summary

| Step             | Observation                                       | Result                                     | Implication                                |
| ---------------- | ------------------------------------------------- | ------------------------------------------ | ------------------------------------------ |
| 12A              | API/Baileys webhook secret mismatch               | 401 invalid_webhook_secret                 | Runtime config had to be aligned           |
| 12A              | After config alignment, fake webhook returned 202 | Celery M1 triggered                        | Webhook route worked                       |
| 12A              | M1 failed with async loop error                   | Task failed                                | Backend async task execution needed fixing |
| 12B              | M1 async loop fixed                               | Fake voice-note path succeeded             | M1 became usable                           |
| 12C              | Fake text path succeeded                          | Inbound/local outbound persisted           | Core text path partly worked               |
| 12C              | Configured AI path returned 401 invalid x-api-key | Fallback response used                     | Model API key not configured               |
| 12C              | MAPS failed async loop                            | Downstream task issue                      | MAPS needed fix                            |
| 12D              | MAPS async loop fixed                             | MAPS succeeded                             | Backend pipeline improved                  |
| 12E              | Wider async audit found more risky task modules   | Shared async helper added                  | Celery stability improved                  |
| 12F              | Relance import compatibility fixed                | Imports improved                           | Side-effect tasks still risky              |
| 12G              | Beat stopped, queues empty                        | Safe pairing state                         | Live testing could proceed                 |
| 12H              | First phone pairing succeeded                     | Live inbound possible                      | Baileys could connect                      |
| 12H              | Real reply happened before safety gate            | Outbound risk proven                       | Hard send gate required                    |
| 12I              | `WHATSAPP_SEND_ENABLED=false` enforced            | Sends skipped                              | Live testing safer                         |
| 12J              | Bad MAC/decryption noise observed                 | Session/adapter instability suspected      | Baileys reliability uncertain              |
| 12J              | extendedText classification bug found             | Fixed                                      | Text support improved                      |
| 12K–12L          | +243 attempt appeared as @lid                     | Skipped before FastAPI                     | LID handling became blocker                |
| 12M              | `/logout` returned 500 Connection Closed          | Reset failed                               | Session lifecycle broken                   |
| 12N              | Removed only Baileys session volume               | Fresh QR generated                         | Session issue isolated to Baileys volume   |
| 12O              | Fresh re-pair succeeded                           | Connected cleanly                          | Ready for controlled test                  |
| 12P              | Some real +243 messages processed                 | Practical evidence                         | Not clean confirmed evidence               |
| 12Q              | Logout lifecycle patch                            | `/logout` became safer                     | Developer reset improved                   |
| 12R              | Confirmed observed method used                    | @lid unsupported                           | Needed LID mapping                         |
| 12S              | senderPn/participantPn identified                 | Safe future mapping possible               | Do not guess LID                           |
| 12T              | LID mapping patch added                           | Valid +243 PN-JID can proceed              | Identity problem improved                  |
| 12U              | Wrapped text extraction added                     | Known sender still no text                 | Not a simple wrapper issue                 |
| 12V              | Redacted diagnostic added                         | `has_message=false`, `messageStubType=2`   | Immediate blocker proven                   |
| 12W              | Limited final attempts                            | No confirmed content-bearing live evidence | Stop Baileys debugging                     |
| Fake regressions | Valid internal +243 payload worked                | API/M1/MAPS/DB passed                      | Step 13A can proceed at webhook level      |

---

## 18. Final Summary

The Baileys investigation produced useful recovery value. It fixed or clarified several real issues:

```text
runtime alignment
M1 async execution
MAPS async execution
Celery async stability
outbound safety
side-effect safety discipline
session logout/reset lifecycle
LID sender mapping
safe text extraction
redacted diagnostics
```

At Step 12 close, the final controlled live blocker appeared outside the core backend pipeline:

```text
Baileys receives the known sender as a valid mapped @lid,
but the event has no message payload.
```

Therefore, at that historical point:

```text
Baileys live inbound was classified as an adapter reliability risk.
The core system should be validated through the webhook path.
Step 13A was the correct next move.
```

Subsequent work resolved this current-status classification for the controlled local scope: live inbound, session restoration, international phone handling, persistence, and exactly-one outbound fallback delivery passed. The remaining boundary is product/transport approval, not an unresolved controlled Baileys recovery test.
