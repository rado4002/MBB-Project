from functools import lru_cache
from pathlib import Path

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

    # ── Adapters ──────────────────────────────────────────────────────────────
    ai_adapter: str = "claude"
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

    # ── CRM — Airtable ────────────────────────────────────────────────────────
    airtable_api_key: str = _read_secret("airtable_api_key", "")
    airtable_base_id: str = ""
    airtable_leads_table: str = "Leads"
    airtable_orders_table: str = "Orders"

    # ── WhatsApp Official ─────────────────────────────────────────────────────
    whatsapp_api_token: str = _read_secret("whatsapp_api_token", "")
    whatsapp_business_account_id: str = ""
    whatsapp_phone_number_id: str = ""

    # ── Baileys ───────────────────────────────────────────────────────────────
    baileys_host: str = "baileys"
    baileys_port: int = 3000

    # ── Payment ───────────────────────────────────────────────────────────────
    orange_money_key: str = _read_secret("orange_money_key", "")
    airtel_money_key: str = _read_secret("airtel_money_key", "")
    mpesa_key: str = _read_secret("mpesa_key", "")
    orange_money_base_url: str = "https://api.orange.com/orange-money-webpay/dev/v1"
    airtel_money_base_url: str = "https://openapiuat.airtel.africa"
    mpesa_base_url: str = "https://sandbox.safaricom.co.ke"

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
    def baileys_url(self) -> str:
        return f"http://{self.baileys_host}:{self.baileys_port}"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
