"""
MBB ya Kin — Streamlit Dashboard

Roles: admin (Toronto), hub (Hub Team), lab (Lab Team)
All write ops routed through FastAPI /admin/* endpoints.
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="MBB ya Kin — Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding (JWT standard)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_dashboard_token(role: str = "admin") -> str:
    """
    Auto-generate a HS256 JWT using only Python stdlib — no extra deps.
    Reads the shared secret from the Docker secret mount that docker-compose
    provides to the dashboard service (/run/secrets/jwt_secret).
    """
    secret_path = Path("/run/secrets/jwt_secret")
    secret = secret_path.read_text().strip() if secret_path.exists() else ""
    if not secret:
        return ""

    now = datetime.now(timezone.utc)
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "iss": "dashboard-mbb",
        "sub": "dashboard",
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=24)).timestamp()),
    }).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = _b64url(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


# ── Session state defaults ────────────────────────────────────────────────────
if "role" not in st.session_state:
    st.session_state.role = "admin"
if "api_url" not in st.session_state:
    st.session_state.api_url = "http://api:8000/api/v1"
if "token" not in st.session_state:
    # Auto-authenticate using the shared JWT secret (internal service, no login form needed)
    st.session_state.token = _make_dashboard_token("admin")

# ── Sidebar: Role selector + Navigation ───────────────────────────────────────
with st.sidebar:
    st.markdown("## MBB ya Kin")
    st.markdown("---")

    role = st.selectbox("Role", ["admin", "hub", "lab"], key="role_select")
    st.session_state.role = role

    st.markdown("---")
    st.markdown("### 📊 Analytics")

    nav_items = [
        "� Conversations",
        "�📊 Funnel",
        "📈 Relance Performance",
        "🌍 Languages",
        "🔍 MAPS Insights",
        "⏱ Response Time",
    ]
    if role == "admin":
        nav_items += ["---", "🔧 Bot Config", "🎫 Escalation Manager", "📝 Content Manager", "⚙️ System Control", "📋 Audit Log"]
    elif role == "hub":
        nav_items += ["---", "👥 Lead Operations", "🎫 Escalation Response"]
    elif role == "lab":
        nav_items += ["---", "🔬 Tone Audit", "🏷️ MAPS Tag Manager"]

    # Filter out separator
    selectable = [i for i in nav_items if i != "---"]
    page = st.radio("Navigate", selectable, label_visibility="collapsed")

    st.markdown("---")
    st.caption(f"Role: **{role}** | v1.0.0-phase1d")

# ── Page routing ──────────────────────────────────────────────────────────────
if page == "� Conversations":
    from pages.hub.conversation_mirror import render
    render()
elif page == "�📊 Funnel":
    from pages.analytics.funnel import render
    render()
elif page == "📈 Relance Performance":
    from pages.analytics.relance import render
    render()
elif page == "🌍 Languages":
    from pages.analytics.languages import render
    render()
elif page == "🔍 MAPS Insights":
    if role in ("admin", "lab"):
        from pages.analytics.maps_insights import render
        render()
    else:
        st.warning("Access restricted to admin and lab roles.")
elif page == "⏱ Response Time":
    if role == "admin":
        from pages.analytics.response_time import render
        render()
    else:
        st.warning("Access restricted to admin role.")
elif page == "🔧 Bot Config" and role == "admin":
    from pages.admin.bot_config import render
    render()
elif page == "🎫 Escalation Manager" and role == "admin":
    from pages.admin.escalation_manager import render
    render()
elif page == "📝 Content Manager" and role == "admin":
    from pages.admin.content_manager import render
    render()
elif page == "⚙️ System Control" and role == "admin":
    from pages.admin.system_control import render
    render()
elif page == "📋 Audit Log" and role == "admin":
    from pages.admin.audit_log import render
    render()
elif page == "👥 Lead Operations" and role == "hub":
    from pages.hub.lead_operations import render
    render()
elif page == "🎫 Escalation Response" and role == "hub":
    from pages.hub.escalation_response import render
    render()
elif page == "🔬 Tone Audit" and role == "lab":
    from pages.lab.tone_audit import render
    render()
elif page == "🏷️ MAPS Tag Manager" and role == "lab":
    from pages.lab.maps_tag_manager import render
    render()
