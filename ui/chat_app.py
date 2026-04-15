"""
Tracking App Domain Expert — Streamlit Web Chat UI
Run with: streamlit run ui/chat_app.py
"""
from __future__ import annotations
import logging
import subprocess
import sys
from pathlib import Path

# Ensure project root is on sys.path when launched via `streamlit run ui/chat_app.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

import config
from rag.chain import ask, build_chain, SimpleConversationalChain

logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Tracking App Domain Expert",
    page_icon="📦",
    layout="wide",
)

QUICK_ASKS = [
    "Take me on a tour of the Tracking App",
    "How does shipment tracking work?",
    "What carriers are supported?",
    "How do I set up email notifications?",
    "What are the different tracking statuses?",
    "How do I embed the tracking page in my store?",
    "How does the branded tracking portal work?",
    "What is the difference between In Transit and Out for Delivery?",
]


def _init_session() -> None:
    if "chain" not in st.session_state:
        with st.spinner("Loading domain expert model…"):
            st.session_state.chain = build_chain()
    if "messages" not in st.session_state:
        st.session_state.messages = []


def _render_sidebar() -> None:
    with st.sidebar:
        st.title("🚚 Tracking App Domain Expert")
        st.caption(f"Model: `{config.DOMAIN_EXPERT_MODEL}`")

        st.divider()
        st.subheader("⚡ Quick Questions")
        for question in QUICK_ASKS:
            if st.button(question, use_container_width=True, key=f"q_{hash(question)}"):
                st.session_state.pending_question = question

        st.divider()
        st.subheader("📚 Knowledge Base")
        st.caption("🌐 PluginHive Product Page")
        st.caption("📖 PluginHive Setup Guide")
        st.caption("🔖 Order Tracking Statuses")
        st.caption("💻 Automation Codebase")

        st.divider()
        if st.button("🔄 Refresh Knowledge Base", use_container_width=True):
            with st.spinner("Re-ingesting all documents… (takes a few minutes)"):
                result = subprocess.run(
                    [sys.executable, "-m", "ingest.run_ingest"],
                    capture_output=True,
                    text=True,
                    cwd=str(config.BASE_DIR),
                )
            if result.returncode == 0:
                st.success("✅ Knowledge base refreshed!")
            else:
                st.error(f"❌ Ingestion failed:\n{result.stderr[:400]}")

        if st.button("🗑️ Clear Chat History", use_container_width=True):
            st.session_state.messages = []
            st.session_state.chain = build_chain()
            st.rerun()


def main() -> None:
    _init_session()
    _render_sidebar()

    st.header("💬 Ask your Tracking App Expert")
    st.caption(
        "Ask anything about the PluginHive Shopify Shipment Tracking & Notifications App — "
        "features, carrier support, notifications, tracking statuses, setup, and more."
    )

    # Render chat history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant" and msg.get("sources"):
                with st.expander("📚 Sources", expanded=False):
                    for src in msg["sources"]:
                        st.caption(src)

    # Resolve question — quick-ask button or chat input
    question: str | None = None
    if "pending_question" in st.session_state:
        question = st.session_state.pop("pending_question")
    else:
        question = st.chat_input("Ask anything about the Tracking App…")

    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                result = ask(question, st.session_state.chain)
            st.markdown(result["answer"])
            if result["sources"]:
                with st.expander("📚 Sources", expanded=False):
                    for src in result["sources"]:
                        st.caption(src)

        st.session_state.messages.append({
            "role": "assistant",
            "content": result["answer"],
            "sources": result["sources"],
        })
        st.rerun()


if __name__ == "__main__":
    main()
