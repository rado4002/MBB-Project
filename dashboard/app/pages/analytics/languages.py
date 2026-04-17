"""Language breakdown page."""
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date, timedelta
from utils.auth import api_get


def render():
    st.title("🌍 Language Breakdown")

    col1, col2 = st.columns(2)
    with col1:
        start = st.date_input("Start", value=date.today() - timedelta(days=30), key="lang_start")
    with col2:
        end = st.date_input("End", value=date.today(), key="lang_end")

    data = api_get("/analytics/funnel", {"period_start": str(start), "period_end": str(end)})
    lang = data.get("language_breakdown", {}) if data else {}

    if not lang:
        st.info("No language data available yet.")
        lang = {"Lingala": 145, "French": 98, "Swahili": 52}

    df = pd.DataFrame(list(lang.items()), columns=["Language", "Conversations"])
    fig = px.pie(df, names="Language", values="Conversations", title="Conversations by Language")
    st.plotly_chart(fig, use_container_width=True)

    st.dataframe(df, use_container_width=True)
