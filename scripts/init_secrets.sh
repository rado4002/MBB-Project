#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# MBB ya Kin — Secret Files Generator
# Creates ./secrets/*.txt with placeholder values.
# Replace placeholders with real values before running: make up
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SECRETS_DIR="$(dirname "$0")/../secrets"
mkdir -p "$SECRETS_DIR"

create_secret() {
    local name="$1"
    local default_value="$2"
    local file="$SECRETS_DIR/${name}.txt"

    if [ -f "$file" ]; then
        echo "  [skip] $name already exists"
    else
        printf '%s' "$default_value" > "$file"
        chmod 600 "$file"
        echo "  [created] $name"
    fi
}

echo ""
echo "Generating secret files in ./secrets/ ..."
echo ""

# ── Database ──────────────────────────────────────────────────────────────────
create_secret "postgres_db"       "mbb"
create_secret "postgres_user"     "mbb"
create_secret "postgres_password" "CHANGE_ME_strong_db_password"

# ── JWT ───────────────────────────────────────────────────────────────────────
# Generate a cryptographically random secret if openssl is available
if command -v openssl &>/dev/null; then
    JWT_VAL=$(openssl rand -hex 32)
else
    JWT_VAL="CHANGE_ME_jwt_secret_min_32_chars_long"
fi
create_secret "jwt_secret" "$JWT_VAL"

# ── AI ────────────────────────────────────────────────────────────────────────
create_secret "claude_api_key"    "CHANGE_ME_sk-ant-..."

# ── CRM ───────────────────────────────────────────────────────────────────────
create_secret "airtable_api_key"  "CHANGE_ME_pat..."

# ── WhatsApp Official API (prod only) ─────────────────────────────────────────
create_secret "whatsapp_api_token" "CHANGE_ME_whatsapp_token"

# ── Payment Gateways ──────────────────────────────────────────────────────────
create_secret "orange_money_key"  "CHANGE_ME_orange_money_key"
create_secret "airtel_money_key"  "CHANGE_ME_airtel_money_key"
create_secret "mpesa_key"         "CHANGE_ME_mpesa_key"
create_secret "payment_webhook_secret" "CHANGE_ME_payment_webhook_hmac_secret"

# ── Grafana ───────────────────────────────────────────────────────────────────
create_secret "grafana_admin_password" "CHANGE_ME_grafana_admin_password"

echo ""
echo "Done. Edit ./secrets/*.txt with real values before running: make up"
echo ""
echo "SECURITY: secrets/ is gitignored. Never commit these files."
echo ""
