"""System Control — health check, maintenance mode, test messages."""
import streamlit as st
from utils.auth import api_get, api_post


def render():
    st.title("⚙️ System Control")

    # Health check
    st.subheader("System Health")
    if st.button("Check Health"):
        health = api_get("/admin/system-health")
        if health:
            status = health.get("status", "unknown")
            if status == "healthy":
                st.success(f"System: {status}")
            else:
                st.warning(f"System: {status}")

            components = health.get("components", {})
            for name, state in components.items():
                if state == "healthy":
                    st.success(f"  {name}: {state}")
                else:
                    st.error(f"  {name}: {state}")
        else:
            st.error("Cannot reach API")

    st.markdown("---")

    # Maintenance mode
    st.subheader("Maintenance Mode")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔴 Enable Maintenance"):
            api_post("/admin/maintenance?enabled=true")
            st.warning("Maintenance mode ENABLED")
    with col2:
        if st.button("🟢 Disable Maintenance"):
            api_post("/admin/maintenance?enabled=false")
            st.success("Maintenance mode DISABLED")

    st.markdown("---")

    # Test message
    st.subheader("Send Test Message (Dev Only)")
    phone = st.text_input("Phone (international E.164, for example +243...)")
    if st.button("Send") and phone:
        result = api_post(f"/admin/test-message?phone_number={phone}")
        if result:
            st.success("Test message sent")
