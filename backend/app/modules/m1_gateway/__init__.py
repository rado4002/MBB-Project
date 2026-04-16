# M1 — Message Gateway
# Receives inbound WhatsApp events, normalizes to InboundMessageEvent,
# and dispatches to M2 (Conversation Engine) via Celery.
