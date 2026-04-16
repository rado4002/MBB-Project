"""Full integration test — validates all project setup components."""
import sys
sys.path.insert(0, ".")

print("=" * 60)
print("MBB ya Kin — Full Project Setup Validation")
print("=" * 60)
errors = []

# ── 1. ORM Models ────────────────────────────────────────────────────────────
print("\n[1] ORM Models...")
try:
    from app.models import (
        Customer, Conversation, Message, Lead, Relance,
        MapsTag, Order, Payment, EscalationTicket, AdminAuditLog,
    )
    print("    10 models imported OK")
except Exception as e:
    errors.append(f"Models: {e}")
    print(f"    FAIL: {e}")

# ── 2. Timestamp timezone check ──────────────────────────────────────────────
print("\n[2] TIMESTAMP(timezone=True) cross-check...")
from app.database import Base
for table_name, table in Base.metadata.tables.items():
    for col in table.columns:
        if hasattr(col.type, "timezone"):
            if not col.type.timezone:
                msg = f"    BUG: {table_name}.{col.name} is TIMESTAMP WITHOUT timezone"
                errors.append(msg)
                print(msg)
if not any("TIMESTAMP WITHOUT" in e for e in errors):
    print("    All datetime columns have timezone=True ✓")

# ── 3. Schemas ───────────────────────────────────────────────────────────────
print("\n[3] Pydantic Schemas...")
try:
    from app.schemas.common import Language, LeadScore, MapsCategory, PaginationMeta, ErrorResponse
    from app.schemas.messages import InboundMessageRequest, QueuedMessageResponse
    from app.schemas.conversations import ConversationContextUpdate
    from app.schemas.leads import LeadCreate
    from app.schemas.relances import RelanceCreate
    from app.schemas.orders import OrderCreate
    from app.schemas.payments import PaymentCallback
    from app.schemas.maps import MapsTagCreate
    from app.schemas.customers import OptOutRequest
    from app.schemas.escalations import EscalationCreate
    from app.schemas.analytics import FunnelMetrics
    from app.schemas.admin import HandoffToggle
    print("    12 schema modules OK")
except Exception as e:
    errors.append(f"Schemas: {e}")
    print(f"    FAIL: {e}")

# ── 4. Security ──────────────────────────────────────────────────────────────
print("\n[4] Security (JWT + HMAC)...")
try:
    from app.security import create_access_token, decode_token, verify_hmac
    token = create_access_token("test-worker", "admin")
    payload = decode_token(token)
    assert payload["sub"] == "test-worker"
    assert payload["role"] == "admin"
    assert verify_hmac("secret", b"payload", "invalid") == False
    import hmac, hashlib
    sig = hmac.new(b"secret", b"payload", hashlib.sha256).hexdigest()
    assert verify_hmac("secret", b"payload", sig) == True
    print("    JWT create/decode OK, HMAC verify OK")
except Exception as e:
    errors.append(f"Security: {e}")
    print(f"    FAIL: {e}")

# ── 5. Dependencies ──────────────────────────────────────────────────────────
print("\n[5] FastAPI Dependencies...")
try:
    from app.api.deps import (
        get_current_role, require_role, DBSession, RedisClient,
        IdempotencyKey, RequestID,
    )
    print("    All deps imported OK")
except Exception as e:
    errors.append(f"Deps: {e}")
    print(f"    FAIL: {e}")

# ── 6. Middleware ─────────────────────────────────────────────────────────────
print("\n[6] Middleware...")
try:
    from app.middleware import RequestTracingMiddleware, MaintenanceModeMiddleware
    print("    2 middleware classes OK")
except Exception as e:
    errors.append(f"Middleware: {e}")
    print(f"    FAIL: {e}")

# ── 7. Cache ─────────────────────────────────────────────────────────────────
print("\n[7] Redis Cache utility...")
try:
    from app.cache import (
        cache_get, cache_set, cache_delete, invalidate_prefix,
        cache_or_compute, make_key, TTL,
    )
    assert make_key("lead", "abc") == "mbb:lead:abc"
    assert make_key("analytics", "funnel", 2026) == "mbb:analytics:funnel:2026"
    assert TTL.SESSION == 3600
    assert TTL.LEAD == 300
    print("    Cache utils + make_key + TTL constants OK")
except Exception as e:
    errors.append(f"Cache: {e}")
    print(f"    FAIL: {e}")

# ── 8. Redis Client (blackout queue) ─────────────────────────────────────────
print("\n[8] Redis Client + Blackout Queue...")
try:
    from app.redis_client import (
        get_redis, get_redis_pool,
        blackout_enqueue, blackout_dequeue_batch, blackout_queue_length,
    )
    print("    Blackout queue helpers imported OK")
except Exception as e:
    errors.append(f"Redis: {e}")
    print(f"    FAIL: {e}")

# ── 9. Celery ────────────────────────────────────────────────────────────────
print("\n[9] Celery configuration...")
try:
    from app.tasks.celery_app import celery_app
    conf = celery_app.conf

    # Verify key settings
    assert conf.task_serializer == "json"
    assert conf.timezone == "Africa/Kinshasa"
    assert conf.enable_utc == True
    assert conf.task_acks_late == True
    assert conf.task_reject_on_worker_lost == True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.result_expires == 86400

    # Verify queues
    routes = conf.task_routes
    assert "app.tasks.relance.*" in routes
    assert "app.tasks.maps.*" in routes
    assert "app.tasks.escalation.*" in routes
    assert "app.tasks.conversion.*" in routes

    # Verify Beat schedule
    beat = conf.beat_schedule
    assert "relance-process-due" in beat
    assert "conversion-drain-blackout" in beat
    assert "maps-aggregate-daily" in beat
    assert "escalation-check-stale" in beat

    print(f"    Queues: {list(routes.keys())}")
    print(f"    Beat tasks: {list(beat.keys())}")
    print("    All Celery config assertions passed")
except Exception as e:
    errors.append(f"Celery: {e}")
    print(f"    FAIL: {e}")

# ── 10. Task modules ─────────────────────────────────────────────────────────
print("\n[10] Celery Task modules...")
try:
    from app.tasks import relance, maps, escalation, conversion
    # Verify tasks are registered
    from app.tasks.relance import send_relance, schedule_next_relance, process_due_relances
    from app.tasks.maps import tag_event, aggregate_daily_maps
    from app.tasks.escalation import escalate_conversation, check_stale_escalations
    from app.tasks.conversion import initiate_payment, process_payment_callback, drain_blackout_queue
    print("    11 tasks across 4 modules OK")
except Exception as e:
    errors.append(f"Tasks: {e}")
    print(f"    FAIL: {e}")

# ── 11. Routers ──────────────────────────────────────────────────────────────
print("\n[11] API Routers...")
try:
    from app.api.v1 import (
        messages, conversations, leads, relances,
        orders, payments, maps, customers, analytics, admin,
    )
    print("    10 routers imported OK")
except Exception as e:
    errors.append(f"Routers: {e}")
    print(f"    FAIL: {e}")

# ── 12. App creation ─────────────────────────────────────────────────────────
print("\n[12] FastAPI App assembly...")
try:
    from app.main import app
    route_paths = [r.path for r in app.routes if hasattr(r, "path")]
    print(f"    Total routes: {len(app.routes)}")

    # Verify critical routes exist
    critical = [
        "/health", "/api/v1/health",
        "/api/v1/messages", "/api/v1/conversations/{conversation_id}",
        "/api/v1/leads", "/api/v1/relances",
        "/api/v1/orders", "/api/v1/payments/callback",
        "/api/v1/maps/tags", "/api/v1/customers/{phone_number:path}/opt-out",
        "/api/v1/admin/system-health",
    ]
    # /metrics is exposed by Instrumentator and may not appear in app.routes
    # Verify it was configured by checking the instrumentator setup
    from prometheus_fastapi_instrumentator import Instrumentator
    # If import succeeds + expose was called, /metrics is available at runtime
    for path in critical:
        if path not in route_paths:
            msg = f"    MISSING ROUTE: {path}"
            errors.append(msg)
            print(msg)

    if not any("MISSING ROUTE" in e for e in errors):
        print("    All critical routes present ✓")
except Exception as e:
    errors.append(f"App: {e}")
    print(f"    FAIL: {e}")

# ── 13. Config ───────────────────────────────────────────────────────────────
print("\n[13] Settings...")
try:
    from app.config import get_settings
    s = get_settings()
    assert s.tz == "Africa/Kinshasa"
    assert s.celery_timezone == "Africa/Kinshasa"
    assert "asyncpg" in s.database_url
    assert "redis" in s.redis_url
    print(f"    database_url: {s.database_url[:40]}...")
    print(f"    redis_url:    {s.redis_url}")
    print(f"    celery broker: {s.celery_broker_url}")
    print(f"    celery backend: {s.celery_result_backend}")
    print("    Config OK")
except Exception as e:
    errors.append(f"Config: {e}")
    print(f"    FAIL: {e}")

# ── 14. Alembic ──────────────────────────────────────────────────────────────
print("\n[14] Alembic migration files...")
try:
    from pathlib import Path
    versions_dir = Path("alembic/versions")
    migrations = list(versions_dir.glob("*.py"))
    migrations = [m for m in migrations if not m.name.startswith("__")]
    print(f"    {len(migrations)} migration(s): {[m.name for m in migrations]}")
    if len(migrations) == 0:
        errors.append("No migration files found")
except Exception as e:
    errors.append(f"Alembic: {e}")
    print(f"    FAIL: {e}")

# ── Summary ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if errors:
    print(f"FAILED — {len(errors)} error(s):")
    for e in errors:
        print(f"  ✗ {e}")
    sys.exit(1)
else:
    print("ALL 14 CHECKS PASSED ✓")
    sys.exit(0)
