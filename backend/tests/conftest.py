"""
tests/conftest.py — Pytest configuration for host-side test runs.

When tests run on the host machine (not inside Docker), the database host
'postgres' is unreachable. We override it to localhost:5433 where the
Docker postgres container publishes its port.

Environment variables must be set BEFORE any app module is imported,
because app/database.py creates the SQLAlchemy engine at import time.
"""
import os

# Override to host-accessible address when running outside Docker
if os.environ.get("POSTGRES_HOST", "postgres") == "postgres":
    os.environ["POSTGRES_HOST"] = "localhost"
    os.environ["POSTGRES_PORT"] = "5433"

# Provide defaults so Settings() doesn't fail on missing secrets
os.environ.setdefault("POSTGRES_DB", "mbb")
os.environ.setdefault("POSTGRES_USER", "mbb")
os.environ.setdefault("POSTGRES_PASSWORD", "mbb_postgres_change_me")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("JWT_SECRET", "testsecret32charslongpadding12345")
os.environ.setdefault("CLAUDE_API_KEY", "test-key")
os.environ.setdefault("AIRTABLE_API_KEY", "test-key")
os.environ.setdefault("BAILEYS_WEBHOOK_SECRET", "dev-webhook-secret-do-not-use-in-production")
