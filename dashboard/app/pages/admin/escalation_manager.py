"""Escalation Manager — admin view of all escalation tickets."""
import streamlit as st
import pandas as pd
from utils.auth import api_get, api_put


def render():
    st.title("🎫 Escalation Manager")

    status_filter = st.selectbox("Status", ["all", "open", "in_progress", "resolved", "closed"])
    params = {"limit": 50, "offset": 0}
    if status_filter != "all":
        params["status"] = status_filter

    data = api_get("/maps/escalations", params)

    if not data or not data.get("tickets"):
        st.info("No escalation tickets found.")
        return

    tickets = data["tickets"]
    df = pd.DataFrame(tickets)
    st.dataframe(df, use_container_width=True)

    st.markdown("---")
    st.subheader("Resolve Ticket")
    ticket_id = st.text_input("Ticket ID")
    notes = st.text_area("Resolution Notes")
    if st.button("Resolve") and ticket_id and notes:
        result = api_put(f"/maps/escalations/{ticket_id}/resolve", {
            "resolution_notes": notes,
            "resolved_by": "admin",
        })
        if result:
            st.success(f"Ticket {ticket_id} resolved")
            st.rerun()
