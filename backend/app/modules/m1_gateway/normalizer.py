"""
app/modules/m1_gateway/normalizer.py — Message format normalizer for dual-mode WhatsApp.

Converts incoming webhook payloads from:
  - Baileys format (Node.js bridge)
  - Official WhatsApp Business API format (Meta Cloud API)

...into a canonical InboundMessageEvent schema that M1-M9 modules consume.

Supports zero-code switching between dev (Baileys) and prod (Official API).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog

log = structlog.get_logger(__name__)


def normalize_baileys_webhook(payload: dict) -> dict:
    """
    Convert Baileys webhook format to canonical InboundMessageEvent.

    Baileys format (simplified):
    {
      "key": {"remoteJid": "2431234567890@s.whatsapp.net", "id": "msg_xyz"},
      "message": {"conversation": "Hello"},
      "messageTimestamp": 1714182000,
      "pushName": "Customer Name"
    }

    Returns:
        Dict matching InboundMessageRequest schema
    """
    key = payload.get("key", {})
    message = payload.get("message", {})
    timestamp_unix = payload.get("messageTimestamp", 0)

    # Extract phone number (remove @s.whatsapp.net suffix)
    remote_jid = key.get("remoteJid", "")
    phone_raw = remote_jid.split("@")[0] if "@" in remote_jid else remote_jid

    # Ensure +243 prefix for DRC
    if not phone_raw.startswith("+"):
        phone_raw = f"+{phone_raw}"
    if not phone_raw.startswith("+243"):
        phone_raw = f"+243{phone_raw.lstrip('+243')}"

    # Extract content and type
    content = ""
    content_type = "text"

    if "conversation" in message:
        content = message["conversation"]
        content_type = "text"
    elif "extendedTextMessage" in message:
        content = message["extendedTextMessage"].get("text", "")
        content_type = "text"
    elif "audioMessage" in message:
        content = "[Voice Note]"
        content_type = "voice_note"
    elif "imageMessage" in message:
        content = message["imageMessage"].get("caption", "[Image]")
        content_type = "image"
    elif "videoMessage" in message:
        content = message["videoMessage"].get("caption", "[Video]")
        content_type = "video"
    else:
        # Unsupported message type
        content = "[Unsupported Message Type]"
        content_type = "text"

    # Timestamp
    timestamp = datetime.fromtimestamp(timestamp_unix, tz=timezone.utc)

    # WhatsApp message ID
    whatsapp_message_id = key.get("id", str(uuid.uuid4()))

    log.debug(
        "normalizer.baileys",
        phone=phone_raw,
        content_type=content_type,
        wa_id=whatsapp_message_id,
    )

    return {
        "message_id": str(uuid.uuid4()),
        "customer_phone": phone_raw,
        "content": content,
        "content_type": content_type,
        "timestamp": timestamp.isoformat(),
        "whatsapp_message_id": whatsapp_message_id,
    }


def normalize_official_webhook(payload: dict) -> dict:
    """
    Convert Official WhatsApp Business API format to canonical InboundMessageEvent.

    Official API format (simplified):
    {
      "entry": [{
        "changes": [{
          "value": {
            "messages": [{
              "from": "2431234567890",
              "id": "wamid.xyz",
              "timestamp": "1714182000",
              "type": "text",
              "text": {"body": "Hello"}
            }]
          }
        }]
      }]
    }

    Returns:
        Dict matching InboundMessageRequest schema
    """
    # Navigate nested structure
    entry = payload.get("entry", [{}])[0]
    changes = entry.get("changes", [{}])[0]
    value = changes.get("value", {})
    messages = value.get("messages", [])

    if not messages:
        log.warning("normalizer.official.no_messages", payload=payload)
        raise ValueError("No messages in webhook payload")

    msg = messages[0]  # Process first message only

    # Extract phone number
    phone_raw = msg.get("from", "")
    if not phone_raw.startswith("+"):
        phone_raw = f"+{phone_raw}"
    if not phone_raw.startswith("+243"):
        phone_raw = f"+243{phone_raw.lstrip('+243')}"

    # Extract content and type
    msg_type = msg.get("type", "text")
    content = ""
    content_type = "text"

    if msg_type == "text":
        content = msg.get("text", {}).get("body", "")
        content_type = "text"
    elif msg_type == "audio":
        content = "[Voice Note]"
        content_type = "voice_note"
    elif msg_type == "image":
        content = msg.get("image", {}).get("caption", "[Image]")
        content_type = "image"
    elif msg_type == "video":
        content = msg.get("video", {}).get("caption", "[Video]")
        content_type = "video"
    elif msg_type == "document":
        content = msg.get("document", {}).get("filename", "[Document]")
        content_type = "document"
    else:
        content = f"[Unsupported: {msg_type}]"
        content_type = "text"

    # Timestamp
    timestamp_unix = int(msg.get("timestamp", 0))
    timestamp = datetime.fromtimestamp(timestamp_unix, tz=timezone.utc)

    # WhatsApp message ID
    whatsapp_message_id = msg.get("id", str(uuid.uuid4()))

    log.debug(
        "normalizer.official",
        phone=phone_raw,
        content_type=content_type,
        wa_id=whatsapp_message_id,
    )

    return {
        "message_id": str(uuid.uuid4()),
        "customer_phone": phone_raw,
        "content": content,
        "content_type": content_type,
        "timestamp": timestamp.isoformat(),
        "whatsapp_message_id": whatsapp_message_id,
    }


def normalize_webhook(payload: dict, source: str = "baileys") -> dict:
    """
    Unified normalizer that routes to the correct format handler.

    Args:
        payload: Raw webhook payload dict
        source: "baileys" or "official"

    Returns:
        Dict matching InboundMessageRequest schema

    Raises:
        ValueError: If payload is invalid or unsupported
    """
    if source == "baileys":
        return normalize_baileys_webhook(payload)
    elif source == "official":
        return normalize_official_webhook(payload)
    else:
        raise ValueError(f"Unknown source: {source}")
