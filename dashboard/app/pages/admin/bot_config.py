"""Bot Configuration page — feature flags + config keys."""
import streamlit as st
from utils.auth import api_get, api_post, api_put


def render():
    st.title("🔧 Bot Configuration")

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
