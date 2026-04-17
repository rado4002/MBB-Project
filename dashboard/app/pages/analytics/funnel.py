"""Conversion Funnel page."""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import date, timedelta
from utils.auth import api_get
from utils.export import export_csv_button


def render():
    st.title("📊 Conversion Funnel")

    col1, col2 = st.columns(2)
    with col1:
        start = st.date_input("Start", value=date.today() - timedelta(days=30))
    with col2:
        end = st.date_input("End", value=date.today())

    data = api_get("/analytics/funnel", {"period_start": str(start), "period_end": str(end)})
    if not data:
        st.info("No data available. Connect to API to see live metrics.")
        # Show placeholder
        stages = ["Conversations", "Leads", "Qualified", "Converted"]
        values = [342, 245, 156, 47]
        fig = go.Figure(go.Funnel(y=stages, x=values, textinfo="value+percent initial"))
        fig.update_layout(title="Conversion Funnel (sample data)")
        st.plotly_chart(fig, use_container_width=True)
        return

    stages = data.get("stages", [])
    if stages:
        fig = go.Figure(go.Funnel(
            y=[s["stage"].title() for s in stages],
            x=[s["count"] for s in stages],
            textinfo="value+percent initial",
        ))
        fig.update_layout(title=f"Funnel: {start} → {end}")
        st.plotly_chart(fig, use_container_width=True)

    # KPI cards
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Conversations", data.get("total_conversations", 0))
    c2.metric("Leads", data.get("total_leads", 0))
    c3.metric("Conversions", data.get("total_conversions", 0))
    c4.metric("Rate", f"{data.get('overall_conversion_rate', 0):.1f}%")

    # Language breakdown
    lang = data.get("language_breakdown", {})
    if lang:
        st.subheader("Language Breakdown")
        df = pd.DataFrame(list(lang.items()), columns=["Language", "Count"])
        st.bar_chart(df.set_index("Language"))
        export_csv_button(df, "funnel_languages.csv")
