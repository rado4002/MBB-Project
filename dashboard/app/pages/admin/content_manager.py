"""Content Manager — manage bot response templates and tone."""
import streamlit as st


def render():
    st.title("📝 Content Manager")

    st.info("Content template management will be implemented in Phase 2 with the CRM adapter.")

    st.subheader("Current Response Tone Settings")
    st.markdown("""
    - **Tone**: Warm, casual, helpful young Congolese friend
    - **Max length**: 2-3 sentences per message
    - **Languages**: Lingala, French, Swahili (auto-detected)
    - **Opt-out keywords**: stop, arrête, yaka te, tika
    """)

    st.subheader("Test Tone")
    test_input = st.text_area("Sample customer message")
    if st.button("Generate Response Preview") and test_input:
        st.info("Tone preview requires Claude API connection (available in production).")
