"""
Security helpers: JWT creation/validation and HMAC-SHA256 webhook verification.

JWT:  used by Celery workers → FastAPI (HS256, 24 h lifetime).
HMAC: used by Mobile Money payment callbacks (per-provider shared secret).
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt

from app.config import get_settings

settings = get_settings()

# ── JWT ───────────────────────────────────────────────────────────────────────
_ALGORITHM = settings.jwt_algorithm
_ISSUER_INTERNAL = "celery-mbb"
_ISSUER_DASHBOARD = "dashboard-mbb"


def create_access_token(subject: str, role: str, issuer: str = _ISSUER_INTERNAL) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": issuer,
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=24),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[_ALGORITHM],
            options={"verify_aud": False},
        )
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ── HMAC Webhook Verification ─────────────────────────────────────────────────
def verify_hmac(secret: str, payload_bytes: bytes, signature_header: str) -> bool:
    """
    Verify HMAC-SHA256 signature sent by Mobile Money provider.
    Timing-safe comparison prevents timing attacks.
    """
    expected = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


async def require_hmac(request: Request, secret: str) -> None:
    """FastAPI dependency: raises 401 if HMAC signature is invalid."""
    sig = request.headers.get("X-Signature-SHA256") or request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()
    if not verify_hmac(secret, body, sig.replace("sha256=", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_hmac_signature",
        )
