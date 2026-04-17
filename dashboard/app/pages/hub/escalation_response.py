"""Escalation Response — Hub Team handles assigned tickets."""
import streamlit as st
import pandas as pd
from utils.auth import api_get, api_put


def render():
    st.title("🎫 Escalation Response")

    data = api_get("/maps/escalations", {"status": "open", "limit": 20})

    if not data or not data.get("tickets"):
        st.success("No open escalations — all clear!")
        return

    tickets = data["tickets"]
    df = pd.DataFrame(tickets)
    st.dataframe(df[["ticket_id", "reason", "priority", "status", "created_at"]], use_container_width=True)

    st.markdown("---")
    ticket_id = st.selectbox("Select Ticket", [t["ticket_id"] for t in tickets])
    notes = st.text_area("Resolution Notes")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Assign to Me") and ticket_id:
            api_put(f"/maps/escalations/{ticket_id}/assign", {"assigned_to": "hub_agent"})
            st.success("Assigned")
            st.rerun()
    with col2:
        if st.button("Resolve") and ticket_id and notes:
            api_put(f"/maps/escalations/{ticket_id}/resolve", {
                "resolution_notes": notes,
                "resolved_by": "hub_agent",
            })
            st.success("Resolved")
            st.rerun()
