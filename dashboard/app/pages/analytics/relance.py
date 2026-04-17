"""Relance Performance page."""
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date, timedelta
from utils.auth import api_get
from utils.export import export_csv_button


def render():
    st.title("📈 Relance Performance")

    col1, col2 = st.columns(2)
    with col1:
        start = st.date_input("Start", value=date.today() - timedelta(days=30), key="rel_start")
    with col2:
        end = st.date_input("End", value=date.today(), key="rel_end")

    data = api_get("/analytics/relance-performance", {"period_start": str(start), "period_end": str(end)})
    if not data:
        st.info("No data available. Connect to API to see live metrics.")
        # Sample data
        df = pd.DataFrame({
            "Attempt": [1, 2, 3],
            "Response Rate": [42.0, 28.0, 15.0],
        })
        fig = px.bar(df, x="Attempt", y="Response Rate", title="Response Rate by Attempt (sample)")
        st.plotly_chart(fig, use_container_width=True)
        return

    # KPI cards
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Relances", data.get("total_relances", 0))
    c2.metric("Response Rate", f"{data.get('overall_response_rate', 0):.1f}%")
    c3.metric("Best Hook", data.get("best_hook_type", "N/A"))
    c4.metric("Opt-outs", data.get("opt_out_triggered", 0))

    # Per-attempt chart
    attempts = data.get("attempts", [])
    if attempts:
        df = pd.DataFrame(attempts)
        fig = px.bar(df, x="attempt_number", y="response_rate", title="Response Rate by Attempt #")
        st.plotly_chart(fig, use_container_width=True)
        export_csv_button(df, "relance_performance.csv")
