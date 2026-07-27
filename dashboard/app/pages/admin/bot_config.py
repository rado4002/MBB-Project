"""Bot Configuration page — feature flags + config keys."""
import os
import streamlit as st
from utils.auth import api_get, api_post, api_put


def render():
    st.title("🔧 Bot Configuration")

    # WhatsApp Mode Status (read-only display)
    st.subheader("📱 WhatsApp Connection")
    mode = os.getenv("WHATSAPP_MODE", "baileys")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Current Mode", mode.upper(), help="Set via WHATSAPP_MODE environment variable")
    
    with col2:
        if mode == "baileys":
            st.info("🔧 Development Mode (Baileys)", icon="🔧")
            st.caption("QR code pairing required")
        elif mode == "official":
            st.warning("Official API selected (not connected)", icon="⚠️")
            st.caption("No validated Official WhatsApp API connection")
        else:
            st.warning(f"Unknown WhatsApp mode: {mode}", icon="⚠️")
            st.caption("Use a supported, separately validated transport mode")
    
    with col3:
        # Connection health check is not implemented on this page.
        st.metric("Status", "Not checked", delta_color="normal")
        st.caption("Connection health unavailable")

    with st.expander("ℹ️ How to Switch Modes"):
        st.markdown("""
        **WhatsApp mode is configured at deployment time via environment variable.**
        
        To switch modes:
        1. Edit `docker-compose.yml` or `.env` file:
           ```bash
           WHATSAPP_MODE=baileys  # Development (Baileys)
           WHATSAPP_MODE=official # Not connected or validated
           ```
        2. Restart API containers:
           ```bash
           docker compose restart api celery_worker
           ```
        
        **Mode Differences:**
        - **Baileys (Dev):** Free, QR pairing, session persistence, good for testing
        - **Official API:** Configuration label only; the adapter is disconnected and unvalidated

        Do not select Official API until its adapter is implemented and separately validated.
        """)

    st.markdown("---")

    # Feature flags
    st.subheader("Feature Flags")
    flags = api_get("/admin/feature-flags")
    if flags:
        col1, col2, col3 = st.columns(3)
        auto_respond = col1.toggle("Auto Respond", value=flags.get("auto_respond", True))
        relance_enabled = col2.toggle("Relance Enabled", value=flags.get("relance_enabled", True))
        maps_enabled = col3.toggle("MAPS Enabled", value=flags.get("maps_enabled", True))

        if st.button("Save Flags"):
            result = api_post("/admin/feature-flags", {
                "auto_respond": auto_respond,
                "relance_enabled": relance_enabled,
                "maps_enabled": maps_enabled,
            })
            if result:
                st.success("Flags updated")
    else:
        st.info("Cannot connect to API — flags unavailable.")

    st.markdown("---")

    # Config keys
    st.subheader("Configuration Keys")
    configs = api_get("/admin/config")
    if configs and configs.get("configs"):
        for cfg in configs["configs"]:
            st.text(f"{cfg['key']} = {cfg['value']}")
    else:
        st.info("No config keys set yet.")

    with st.expander("Set Config Key"):
        key = st.text_input("Key")
        value = st.text_input("Value")
        desc = st.text_input("Description (optional)")
        if st.button("Save Config") and key and value:
            api_put(f"/admin/config/{key}", {"key": key, "value": value, "description": desc})
            st.success(f"Config '{key}' saved")
            st.rerun()
