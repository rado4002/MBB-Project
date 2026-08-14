from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(name: str, default: str = "") -> str:
    """Read a Docker secret from /run/secrets/<name>. Falls back to default."""
    path = Path(f"/run/secrets/{name}")
    if path.exists():
        return path.read_text().strip()
    return default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Application ───────────────────────────────────────────────────────────
    app_env: str = "development"
    debug: bool = True
    tz: str = "Africa/Kinshasa"

    # ── WhatsApp Mode ─────────────────────────────────────────────────────────
    whatsapp_mode: str = "baileys"          # baileys | official
    whatsapp_send_enabled: bool = False     # safety gate for real outbound delivery
    crm_send_enabled: bool = False          # safety gate for CRM external writes
    payment_send_enabled: bool = False      # safety gate for payment provider calls
    relance_enabled: bool = False           # safety gate for relance scheduling/sends
    scheduled_tasks_enabled: bool = False   # safety gate for Celery beat schedules
    m1_maps_fanout_enabled: bool = True     # safety gate for M1 MAPS task submission

    # ── Adapters ──────────────────────────────────────────────────────────────
    ai_adapter: str = "disabled"
    ai_turn_provider: str = "disabled"
    crm_adapter: str = "airtable"
    inventory_adapter: str = "static"
    payment_adapter: str = "mobile_money"
    messaging_adapter: str = "whatsapp"

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = _read_secret("postgres_db", "mbb")
    postgres_user: str = _read_secret("postgres_user", "mbb")
    postgres_password: str = _read_secret("postgres_password", "")

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0

    # ── Browser authentication (default-off until runtime validation) ───────
    browser_auth_enabled: bool = False
    browser_allowed_origin: str = ""
    browser_session_redis_db: Literal[4] = 4
    browser_session_idle_seconds: int = Field(default=1800, ge=1, le=1800)
    browser_session_absolute_seconds: int = Field(default=28800, ge=1, le=28800)
    browser_recent_reauth_seconds: int = Field(default=600, ge=1, le=600)
    browser_max_sessions_per_account: int = Field(default=2, ge=1, le=2)
    browser_session_activity_coalesce_seconds: int = Field(default=60, ge=0)
    browser_session_hmac_secret: str = _read_secret("browser_session_hmac_secret", "")
    browser_csrf_hmac_secret: str = _read_secret("browser_csrf_hmac_secret", "")
    browser_idempotency_hmac_secret: str = _read_secret(
        "browser_idempotency_hmac_secret", ""
    )
    browser_preauth_seconds: int = Field(default=600, ge=60, le=600)
    browser_login_account_failure_limit: Literal[5] = 5
    browser_login_source_failure_limit: Literal[20] = 20
    browser_reauth_failure_limit: Literal[5] = 5
    browser_auth_rate_window_seconds: Literal[900] = 900
    operator_audit_retention_days: int = Field(default=365, ge=365)
    operator_security_metadata_retention_days: int = Field(default=90, ge=1, le=90)
    temporary_password_lifetime_seconds: int = Field(default=86400, ge=1, le=86400)

    # ── Celery ────────────────────────────────────────────────────────────────
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"
    celery_timezone: str = "Africa/Kinshasa"

    # ── JWT ───────────────────────────────────────────────────────────────────
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60
    jwt_secret: str = _read_secret("jwt_secret", "")

    # ── AI — Claude ───────────────────────────────────────────────────────────
    claude_api_key: str = _read_secret("claude_api_key", "")
    claude_model: str = "claude-sonnet-4-5"
    claude_max_tokens: int = 1024
    claude_timeout_s: int = 25

    # ── AI — DeepSeek ─────────────────────────────────────────────────────────
    deepseek_api_key: str = Field(
        default=_read_secret("deepseek_api_key", ""),
        repr=False,
    )
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_s: int = Field(default=60, ge=1, le=120)

    # ── CRM — Airtable ────────────────────────────────────────────────────────
    airtable_api_key: str = _read_secret("airtable_api_key", "")
    airtable_base_id: str = ""
    airtable_leads_table: str = "Leads"
    airtable_orders_table: str = "Orders"

    # ── WhatsApp Official ─────────────────────────────────────────────────────
    whatsapp_api_token: str = _read_secret("whatsapp_api_token", "")
    whatsapp_business_account_id: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = _read_secret("whatsapp_verify_token", "")
    whatsapp_api_secret: str = _read_secret("whatsapp_api_secret", "")

    # ── Baileys ───────────────────────────────────────────────────────────────
    baileys_host: str = "baileys"
    baileys_port: int = 3000
    baileys_webhook_secret: str = _read_secret("baileys_webhook_secret", "")
    baileys_send_max_attempts: int = Field(default=1, ge=1)

    # ── Payment ───────────────────────────────────────────────────────────────
    orange_money_key: str = _read_secret("orange_money_key", "")
    airtel_money_key: str = _read_secret("airtel_money_key", "")
    mpesa_key: str = _read_secret("mpesa_key", "")
    orange_money_base_url: str = "https://api.orange.com/orange-money-webpay/dev/v1"
    airtel_money_base_url: str = "https://openapiuat.airtel.africa"
    mpesa_base_url: str = "https://sandbox.safaricom.co.ke"
    payment_webhook_secret: str = _read_secret("payment_webhook_secret", "")

    # ── Observability ─────────────────────────────────────────────────────────
    loki_url: str = "http://loki:3100"
    log_level: str = "INFO"

    # ── Computed Properties ───────────────────────────────────────────────────
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def browser_session_redis_url(self) -> str:
        return (
            f"redis://{self.redis_host}:{self.redis_port}/"
            f"{self.browser_session_redis_db}"
        )

    @property
    def baileys_url(self) -> str:
        return f"http://{self.baileys_host}:{self.baileys_port}"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
