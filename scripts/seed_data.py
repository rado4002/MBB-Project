"""
MBB ya Kin — Database Seed Script (Sprint 0.1)

Seeds the dev database with representative test data:
  - 3 customers (one per language: Lingala / French / Swahili)
  - 1 active conversation per customer
  - 2 messages per conversation (inbound + outbound)
  - 1 qualified lead per conversation (hot / warm / cold)

All inserts use ON CONFLICT DO NOTHING — fully idempotent.

Usage (from project root):
    make seed
    # or directly:
    docker compose -f docker-compose.yml -f docker-compose.dev.yml exec api \
        python scripts/seed_data.py
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, ".")

# Allow running locally with minimal env
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "mbb")
os.environ.setdefault("POSTGRES_USER", "mbb")
os.environ.setdefault("POSTGRES_PASSWORD", "mbb")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("JWT_SECRET", "testsecret32charslongpadding12345")
os.environ.setdefault("CLAUDE_API_KEY", "test")
os.environ.setdefault("AIRTABLE_API_KEY", "test")
os.environ.setdefault("PAYMENT_WEBHOOK_SECRET", "test")

from sqlalchemy import text  # noqa: E402
from app.database import async_session_factory  # noqa: E402


NOW = datetime.now(timezone.utc)

# ── Seed data definitions ─────────────────────────────────────────────────────

CUSTOMERS = [
    {
        "phone_number": "+243810000001",
        "name": "Mama Bolingo",
        "city": "Kinshasa",
        "preferred_language": "lingala",
        "opt_out_flag": False,
        "consent_given": True,
        "consent_timestamp": NOW,
        "club_member": True,
        "club_points": 150,
    },
    {
        "phone_number": "+243820000002",
        "name": "Jean-Pierre Mukendi",
        "city": "Lubumbashi",
        "preferred_language": "french",
        "opt_out_flag": False,
        "consent_given": True,
        "consent_timestamp": NOW,
        "club_member": False,
        "club_points": 0,
    },
    {
        "phone_number": "+243830000003",
        "name": "Amina Zawadi",
        "city": "Goma",
        "preferred_language": "swahili",
        "opt_out_flag": False,
        "consent_given": True,
        "consent_timestamp": NOW,
        "club_member": False,
        "club_points": 0,
    },
]

# Fixed UUIDs for idempotent seeding
CONV_IDS = [
    uuid.UUID("aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"),
    uuid.UUID("aaaaaaaa-0002-0002-0002-aaaaaaaaaaaa"),
    uuid.UUID("aaaaaaaa-0003-0003-0003-aaaaaaaaaaaa"),
]

MSG_IDS = [
    (uuid.UUID("bbbbbbbb-0001-0001-0001-bbbbbbbbbbbb"), uuid.UUID("bbbbbbbb-0001-0002-0001-bbbbbbbbbbbb")),
    (uuid.UUID("bbbbbbbb-0002-0001-0002-bbbbbbbbbbbb"), uuid.UUID("bbbbbbbb-0002-0002-0002-bbbbbbbbbbbb")),
    (uuid.UUID("bbbbbbbb-0003-0001-0003-bbbbbbbbbbbb"), uuid.UUID("bbbbbbbb-0003-0002-0003-bbbbbbbbbbbb")),
]

LEAD_IDS = [
    uuid.UUID("cccccccc-0001-0001-0001-cccccccccccc"),
    uuid.UUID("cccccccc-0002-0002-0002-cccccccccccc"),
    uuid.UUID("cccccccc-0003-0003-0003-cccccccccccc"),
]

CONVERSATIONS = [
    {
        "conversation_id": CONV_IDS[0],
        "customer_id": "+243810000001",
        "status": "qualifying",
        "language_detected": "lingala",
        "context": '{"current_topic": "cable_2m", "stage": "consideration", "qualification_step": 1}',
        "message_count": 2,
    },
    {
        "conversation_id": CONV_IDS[1],
        "customer_id": "+243820000002",
        "status": "nurturing",
        "language_detected": "french",
        "context": '{"current_topic": "powerbank_20k", "stage": "decision", "price_discussed": true}',
        "message_count": 2,
    },
    {
        "conversation_id": CONV_IDS[2],
        "customer_id": "+243830000003",
        "status": "active",
        "language_detected": "swahili",
        "context": '{"current_topic": null, "stage": "awareness"}',
        "message_count": 2,
    },
]

MESSAGES = [
    # Customer 1 — Lingala
    {
        "message_id": MSG_IDS[0][0],
        "conversation_id": CONV_IDS[0],
        "direction": "inbound",
        "content": "Mbote! Nazali koluka cable ya telefoni oyo ekoka kobatela courant mingi.",
        "content_type": "text",
        "language": "lingala",
    },
    {
        "message_id": MSG_IDS[0][1],
        "conversation_id": CONV_IDS[0],
        "direction": "outbound",
        "content": "Mbote! Eza cable ya 2m, ekoki kobatela 65W. Prix: 8 500 FC. Ozali na Kinshasa?",
        "content_type": "text",
        "language": "lingala",
        "processing_time_ms": 1850,
    },
    # Customer 2 — French
    {
        "message_id": MSG_IDS[1][0],
        "conversation_id": CONV_IDS[1],
        "direction": "inbound",
        "content": "Bonjour, est-ce que vous avez des powerbanks de bonne qualite?",
        "content_type": "text",
        "language": "french",
    },
    {
        "message_id": MSG_IDS[1][1],
        "conversation_id": CONV_IDS[1],
        "direction": "outbound",
        "content": "Oui! Notre powerbank 20 000 mAh charge 3 telephones. Prix: 45 000 FC. Vous etes a Lubumbashi?",
        "content_type": "text",
        "language": "french",
        "processing_time_ms": 2100,
        "persuasion_hook": "social_proof",
    },
    # Customer 3 — Swahili
    {
        "message_id": MSG_IDS[2][0],
        "conversation_id": CONV_IDS[2],
        "direction": "inbound",
        "content": "Habari, mna nini kwa bei nzuri leo?",
        "content_type": "text",
        "language": "swahili",
    },
    {
        "message_id": MSG_IDS[2][1],
        "conversation_id": CONV_IDS[2],
        "direction": "outbound",
        "content": "Habari! Tuna bidhaa nzuri za teknolojia. Unatafuta nini hasa - cable, powerbank au kingine?",
        "content_type": "text",
        "language": "swahili",
        "processing_time_ms": 1600,
    },
]

LEADS = [
    {
        "lead_id": LEAD_IDS[0],
        "customer_id": "+243810000001",
        "conversation_id": CONV_IDS[0],
        "score": "hot",
        "score_value": 8,
        "stage": "consideration",
        "intent": "product_inquiry",
        "product_interest": ["cable_2m", "cable_1m"],
        "source": "organic",
        "relance_count": 0,
    },
    {
        "lead_id": LEAD_IDS[1],
        "customer_id": "+243820000002",
        "conversation_id": CONV_IDS[1],
        "score": "warm",
        "score_value": 5,
        "stage": "decision",
        "intent": "product_inquiry",
        "product_interest": ["powerbank_20k"],
        "source": "facebook_ad",
        "relance_count": 1,
    },
    {
        "lead_id": LEAD_IDS[2],
        "customer_id": "+243830000003",
        "conversation_id": CONV_IDS[2],
        "score": "cold",
        "score_value": 2,
        "stage": "awareness",
        "intent": "product_inquiry",
        "product_interest": [],
        "source": "organic",
        "relance_count": 0,
    },
]


# ── Insertion helpers ─────────────────────────────────────────────────────────

async def seed_customers(session) -> None:
    for c in CUSTOMERS:
        await session.execute(text("""
            INSERT INTO mbb.customers
                (phone_number, name, city, preferred_language, opt_out_flag,
                 consent_given, consent_timestamp, club_member, club_points)
            VALUES
                (:phone_number, :name, :city, :preferred_language, :opt_out_flag,
                 :consent_given, :consent_timestamp, :club_member, :club_points)
            ON CONFLICT (phone_number) DO NOTHING
        """), c)
    print(f"  + {len(CUSTOMERS)} customers seeded")


async def seed_conversations(session) -> None:
    for conv in CONVERSATIONS:
        await session.execute(text("""
            INSERT INTO mbb.conversations
                (conversation_id, customer_id, status, language_detected, context, message_count)
            VALUES
                (:conversation_id, :customer_id, :status, :language_detected,
                 CAST(:context AS jsonb), :message_count)
            ON CONFLICT (conversation_id) DO NOTHING
        """), conv)
    print(f"  + {len(CONVERSATIONS)} conversations seeded")


async def seed_messages(session) -> None:
    for msg in MESSAGES:
        msg.setdefault("processing_time_ms", None)
        msg.setdefault("persuasion_hook", None)
        msg.setdefault("whatsapp_message_id", None)
        await session.execute(text("""
            INSERT INTO mbb.messages
                (message_id, conversation_id, direction, content, content_type,
                 language, persuasion_hook, processing_time_ms, whatsapp_message_id)
            VALUES
                (:message_id, :conversation_id, :direction, :content, :content_type,
                 :language, :persuasion_hook, :processing_time_ms, :whatsapp_message_id)
            ON CONFLICT (message_id) DO NOTHING
        """), msg)
    print(f"  + {len(MESSAGES)} messages seeded")


async def seed_leads(session) -> None:
    for lead in LEADS:
        await session.execute(text("""
            INSERT INTO mbb.leads
                (lead_id, customer_id, conversation_id, score, score_value,
                 stage, intent, product_interest, source, relance_count)
            VALUES
                (:lead_id, :customer_id, :conversation_id, :score, :score_value,
                 :stage, :intent, CAST(:product_interest AS text[]), :source, :relance_count)
            ON CONFLICT (conversation_id) DO NOTHING
        """), {**lead, "product_interest": lead["product_interest"]})
    print(f"  + {len(LEADS)} leads seeded")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    print("MBB ya Kin — Seeding dev database...")
    async with async_session_factory() as session:
        await seed_customers(session)
        await seed_conversations(session)
        await seed_messages(session)
        await seed_leads(session)
        await session.commit()
    print("\nSeed complete. Run `make shell-db` to inspect the data.")


if __name__ == "__main__":
    asyncio.run(main())
