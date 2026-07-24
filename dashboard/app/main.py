"""
MBB ya Kin — Streamlit Dashboard

Roles: admin (Toronto), hub (Hub Team), lab (Lab Team)
All write ops routed through FastAPI /admin/* endpoints.
"""
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="MBB ya Kin — Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _load_dashboard_api_token() -> str:
    """Load the explicitly provisioned API token without granting signing access."""
    token_path = Path("/run/secrets/dashboard_api_token")
    try:
        token = token_path.read_text().strip()
    except OSError:
        return ""
    return token if token.count(".") == 2 else ""


dashboard_api_token = _load_dashboard_api_token()
if not dashboard_api_token:
    st.error("Dashboard authentication is not configured.")
    st.stop()


# ── Session state defaults ────────────────────────────────────────────────────
if "role" not in st.session_state:
    st.session_state.role = "admin"
if "api_url" not in st.session_state:
    st.session_state.api_url = "http://api:8000/api/v1"
st.session_state.token = dashboard_api_token

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
