"""Response Time monitoring page."""
import streamlit as st


def render():
    st.title("⏱ Response Time")

    c1, c2, c3 = st.columns(3)
    c1.metric("Avg Response", "< 45s", "Target: < 60s")
    c2.metric("P95 Response", "52s")
    c3.metric("Automation Rate", "82%", "Target: 80-85%")

    st.info("Live response time metrics require Prometheus integration (Phase 2).")
