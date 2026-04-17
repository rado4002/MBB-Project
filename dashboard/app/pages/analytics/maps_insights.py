"""MAPS Insights page — demand signals, silence reasons, conversion triggers."""
import streamlit as st
import pandas as pd
import plotly.express as px
from utils.auth import api_get
from utils.export import export_csv_button


def render():
    st.title("🔍 MAPS Insights")

    category = st.selectbox("Category", ["All", "demand_signal", "objection", "opt_out_reason", "urgency_cue", "language_preference"])
    cat_param = None if category == "All" else category

    data = api_get("/maps/insights", {"category": cat_param, "limit": 50} if cat_param else {"limit": 50})

    if not data or not data.get("insights"):
        st.info("No MAPS tags collected yet.")
        sample = pd.DataFrame({
            "Pattern": ["price_objection", "product_interest", "urgency_now"],
            "Category": ["objection", "demand_signal", "urgency_cue"],
            "Count": [45, 120, 32],
        })
        fig = px.bar(sample, x="Pattern", y="Count", color="Category", title="MAPS Tags (sample)")
        st.plotly_chart(fig, use_container_width=True)
        return

    insights = data["insights"]
    df = pd.DataFrame(insights)
    fig = px.bar(df, x="pattern", y="count", color="category", title="MAPS Tag Distribution")
    st.plotly_chart(fig, use_container_width=True)

    st.dataframe(df, use_container_width=True)
    export_csv_button(df, "maps_insights.csv")
