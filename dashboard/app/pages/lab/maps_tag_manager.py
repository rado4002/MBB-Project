"""MAPS Tag Manager — Lab Team validates and corrects auto-generated tags."""
import streamlit as st
import pandas as pd
from utils.auth import api_get, api_put
from utils.export import export_csv_button


def render():
    st.title("🏷️ MAPS Tag Manager")

    col1, col2 = st.columns(2)
    with col1:
        category = st.selectbox("Category", ["all", "demand_signal", "objection", "opt_out_reason", "urgency_cue", "language_preference"])
    with col2:
        validated = st.selectbox("Validation Status", ["all", "validated", "unvalidated"])

    params = {"limit": 50, "offset": 0}
    if category != "all":
        params["category"] = category
    if validated != "all":
        params["validated"] = validated == "validated"

    data = api_get("/maps/tags", params)

    if not data or not data.get("tags"):
        st.info("No MAPS tags to review.")
        return

    tags = data["tags"]
    df = pd.DataFrame(tags)
    st.dataframe(df, use_container_width=True)
    export_csv_button(df, "maps_tags.csv")

    st.markdown("---")
    st.subheader("Validate / Correct Tag")
    tag_id = st.text_input("Tag ID")
    corrected_tag = st.text_input("Corrected Pattern (optional)")
    corrected_cat = st.selectbox("Corrected Category (optional)",
                                  ["", "demand_signal", "objection", "opt_out_reason", "urgency_cue", "language_preference"])

    if st.button("Validate Tag") and tag_id:
        body = {"validated": True}
        if corrected_tag:
            body["corrected_tag"] = corrected_tag
        if corrected_cat:
            body["corrected_category"] = corrected_cat
        result = api_put(f"/maps/tags/{tag_id}/validate", body)
        if result:
            st.success(f"Tag {tag_id} validated")
            st.rerun()
