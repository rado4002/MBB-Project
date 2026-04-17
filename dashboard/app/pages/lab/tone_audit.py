"""Tone Audit — Lab Team reviews AI-generated responses for tone compliance."""
import streamlit as st


def render():
    st.title("🔬 Tone Audit Console")

    st.markdown("""
    **Tone Rules:**
    - Feel like a helpful young Congolese friend
    - 2–3 sentences MAX per message
    - Help first, sell second
    - NEVER robotic, pushy, or corporate
    """)

    st.info("Tone audit with conversation samples requires M2 (Conversation Engine) data. Available after Phase 1.B integration.")

    st.subheader("Tone Checklist")
    st.checkbox("Warm and casual", value=True)
    st.checkbox("Max 2-3 sentences", value=True)
    st.checkbox("Help-first approach", value=True)
    st.checkbox("No corporate language", value=True)
    st.checkbox("Respectful opt-out handling", value=True)
