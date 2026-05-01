"""
Conversation Mirror — WhatsApp-style chat viewer.

Shows all conversations with full message history, like looking
at the customer's WhatsApp screen in real time.
"""
import streamlit as st
import pandas as pd
from datetime import datetime
from utils.auth import api_get, api_post, api_put


def _render_message_bubble(msg: dict):
    """Render a single message as a WhatsApp-style bubble."""
    direction = msg.get("direction", "inbound")
    content = msg.get("content", "")
    content_type = msg.get("content_type", "text")
    ts = msg.get("timestamp", "")
    lang = msg.get("language", "")

    # Parse timestamp
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        time_str = dt.strftime("%H:%M")
    except Exception:
        time_str = ""

    if direction == "inbound":
        # Customer message — left aligned, green-ish
        st.markdown(
            f"""<div style="
                background-color: #dcf8c6;
                border-radius: 12px 12px 12px 0;
                padding: 8px 14px;
                margin: 4px 20% 4px 0;
                max-width: 80%;
                word-wrap: break-word;
                font-size: 14px;
                color: #111;
            ">
                {'🎙️ ' if content_type == 'voice_note' else '🖼️ ' if content_type == 'image' else ''}{content}
                <div style="text-align: right; font-size: 11px; color: #666; margin-top: 2px;">
                    {time_str} · {lang}
                </div>
            </div>""",
            unsafe_allow_html=True,
        )
    else:
        # Bot message — right aligned, white
        st.markdown(
            f"""<div style="
                background-color: #ffffff;
                border-radius: 12px 12px 0 12px;
                padding: 8px 14px;
                margin: 4px 0 4px 20%;
                max-width: 80%;
                word-wrap: break-word;
                font-size: 14px;
                color: #111;
                border: 1px solid #e0e0e0;
            ">
                🤖 {content}
                <div style="text-align: right; font-size: 11px; color: #666; margin-top: 2px;">
                    {time_str} · Bot
                </div>
            </div>""",
            unsafe_allow_html=True,
        )


def render():
    st.title("💬 Conversation Mirror")
    st.caption("Real-time view of all WhatsApp conversations — like looking at the customer's phone.")

    # ── Conversation list (left panel) ────────────────────────────────────────
    col_list, col_chat = st.columns([1, 2])

    with col_list:
        st.subheader("Conversations")

        status_filter = st.selectbox(
            "Status", ["all", "active", "qualifying", "nurturing", "escalated", "converted", "dormant"],
            key="conv_status_filter",
        )

        params = {"limit": 30, "offset": 0}
        if status_filter != "all":
            params["status"] = status_filter

        data = api_get("/conversations", params)

        if not data or not data.get("conversations"):
            st.info("No conversations yet. Messages will appear once customers start chatting.")

            # Show sample for demo
            st.markdown("---")
            st.markdown("**Sample conversations (demo)**")
            sample_convs = [
                {"customer_name": "Marie", "customer_phone": "+243991234567", "status": "active", "message_count": 12, "last_message_time": "2026-04-17T10:30:00"},
                {"customer_name": "Patrick", "customer_phone": "+243971234567", "status": "escalated", "message_count": 8, "last_message_time": "2026-04-17T09:15:00"},
                {"customer_name": None, "customer_phone": "+243851234567", "status": "qualifying", "message_count": 3, "last_message_time": "2026-04-17T08:00:00"},
            ]
            for c in sample_convs:
                name = c.get("customer_name") or c["customer_phone"]
                status_emoji = {"active": "🟢", "escalated": "🔴", "qualifying": "🟡", "nurturing": "🔵", "converted": "✅", "dormant": "⚪"}.get(c["status"], "⚪")
                st.markdown(f"{status_emoji} **{name}** · {c['message_count']} msgs · {c['status']}")
            return

        conversations = data["conversations"]
        conv_options = {}
        for c in conversations:
            name = c.get("customer_name") or c["customer_phone"]
            status_emoji = {"active": "🟢", "escalated": "🔴", "qualifying": "🟡", "nurturing": "🔵", "converted": "✅", "dormant": "⚪"}.get(c["status"], "⚪")
            label = f"{status_emoji} {name} ({c['message_count']} msgs)"
            conv_options[label] = c["conversation_id"]

        selected = st.radio("Select conversation", list(conv_options.keys()), label_visibility="collapsed")
        selected_id = conv_options.get(selected)

    # ── Chat view (right panel) ───────────────────────────────────────────────
    with col_chat:
        if not data or not data.get("conversations"):
            return

        if not selected_id:
            st.info("Select a conversation from the list.")
            return

        # Fetch full conversation with messages
        conv_data = api_get(f"/conversations/{selected_id}", {"limit": 100, "include_context": True})

        if not conv_data:
            st.error("Could not load conversation.")
            return

        # Header
        customer = conv_data.get("customer", {})
        customer_name = customer.get("name") or customer.get("phone_number", "Unknown")
        conv_status = conv_data.get("status", "unknown")
        language = conv_data.get("language_detected", "")
        lead = conv_data.get("lead", {})

        st.markdown(f"### {customer_name}")
        header_cols = st.columns(4)
        header_cols[0].metric("Status", conv_status)
        header_cols[1].metric("Language", language)
        header_cols[2].metric("Messages", conv_data.get("message_count", 0))
        if lead:
            header_cols[3].metric("Lead", f"{lead.get('score', 'N/A')}")

        st.markdown("---")

        # Chat area with WhatsApp-style bubbles
        messages = conv_data.get("messages", [])
        stored_count = conv_data.get("message_count", 0)
        if not messages:
            if stored_count > 0:
                st.warning(
                    f"Message history unavailable — {stored_count} messages are recorded "
                    "but could not be loaded. Try refreshing or check the database."
                )
            else:
                st.info("No messages in this conversation yet.")
        else:
            # Chat container with scrollable area
            chat_container = st.container(height=500)
            with chat_container:
                for msg in messages:
                    _render_message_bubble(msg)

        # ── Actions ───────────────────────────────────────────────────────────
        st.markdown("---")
        action_cols = st.columns(3)

        with action_cols[0]:
            if conv_status != "escalated":
                if st.button("🔴 Escalate", key="escalate_btn"):
                    st.session_state["show_escalate_form"] = True

            if st.session_state.get("show_escalate_form"):
                reason = st.text_input("Reason", key="esc_reason")
                if st.button("Confirm Escalate", key="confirm_esc") and reason:
                    api_post(f"/conversations/{selected_id}/escalate", {
                        "conversation_id": selected_id,
                        "reason": reason,
                        "priority": "high",
                        "escalation_type": "complex_issue",
                    })
                    st.success("Escalated!")
                    st.session_state["show_escalate_form"] = False
                    st.rerun()

        with action_cols[1]:
            if conv_status == "escalated":
                if st.button("🤖 Return to Bot", key="handoff_bot"):
                    api_put(f"/conversations/{selected_id}/handoff", {"mode": "bot"})
                    st.success("Returned to bot control")
                    st.rerun()
            else:
                if st.button("👤 Handoff to Human", key="handoff_human"):
                    api_put(f"/conversations/{selected_id}/handoff", {"mode": "human"})
                    st.success("Handed off to human")
                    st.rerun()

        with action_cols[2]:
            if st.button("🔄 Refresh", key="refresh_chat"):
                st.rerun()

        # Context panel (collapsible)
        context = conv_data.get("context")
        if context:
            with st.expander("📋 Conversation Context (JSONB)"):
                st.json(context)
