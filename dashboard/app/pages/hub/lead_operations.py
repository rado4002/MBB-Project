"""Lead Operations — Hub Team view for lead management."""
import streamlit as st
import pandas as pd
from utils.auth import api_get


def render():
    st.title("👥 Lead Operations")

    score_filter = st.selectbox("Score Filter", ["all", "hot", "warm", "cold"])
    params = {"limit": 50, "offset": 0}
    if score_filter != "all":
        params["score"] = score_filter

    data = api_get("/leads", params)

    if not data or not data.get("leads"):
        st.info("No leads found. Leads will appear once conversations are qualified.")
        return

    df = pd.DataFrame(data["leads"])
    st.dataframe(df, use_container_width=True)

    # Quick stats
    if not df.empty:
        c1, c2, c3 = st.columns(3)
        c1.metric("Hot", len(df[df.get("score") == "hot"]) if "score" in df.columns else 0)
        c2.metric("Warm", len(df[df.get("score") == "warm"]) if "score" in df.columns else 0)
        c3.metric("Cold", len(df[df.get("score") == "cold"]) if "score" in df.columns else 0)
