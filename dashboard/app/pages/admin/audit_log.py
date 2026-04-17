"""Audit Log viewer."""
import streamlit as st
import pandas as pd
from utils.auth import api_get
from utils.export import export_csv_button


def render():
    st.title("📋 Audit Log")

    col1, col2 = st.columns(2)
    with col1:
        actor = st.text_input("Filter by actor (optional)")
    with col2:
        limit = st.number_input("Limit", min_value=10, max_value=200, value=50)

    params = {"limit": limit, "offset": 0}
    if actor:
        params["actor"] = actor

    data = api_get("/admin/audit-logs", params)

    if not data or not data.get("entries"):
        st.info("No audit log entries yet.")
        return

    df = pd.DataFrame(data["entries"])
    st.dataframe(df, use_container_width=True)
    export_csv_button(df, "audit_log.csv")
