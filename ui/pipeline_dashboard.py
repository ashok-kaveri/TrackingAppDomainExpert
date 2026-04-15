"""
Tracking App Domain Expert — Pipeline Dashboard
Run with: PYTHONPATH=. streamlit run ui/pipeline_dashboard.py
"""
from __future__ import annotations
import logging
import subprocess
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path when launched via `streamlit run ui/pipeline_dashboard.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb
import requests
import streamlit as st

import config

logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Pipeline Dashboard — Tracking App",
    page_icon="🔧",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------

ALL_SOURCES = [
    {
        "key": "pluginhive_seeds",
        "label": "PluginHive Seed URLs",
        "icon": "🌐",
        "description": "Product page, setup guide, tracking statuses (3 pages — fast)",
        "default": True,
    },
    {
        "key": "codebase",
        "label": "Automation Codebase",
        "icon": "💻",
        "description": "Playwright / TypeScript test automation codebase",
        "default": True,
    },
    {
        "key": "pdf",
        "label": "PDF Test Cases",
        "icon": "📄",
        "description": "TrackingApp Master sheet test cases PDF",
        "default": True,
    },
    {
        "key": "wiki",
        "label": "Internal Wiki",
        "icon": "📖",
        "description": "Markdown knowledge base (requires WIKI_PATH in .env)",
        "default": True,
    },
    {
        "key": "pluginhive",
        "label": "PluginHive Full Crawl",
        "icon": "🕷️",
        "description": "Full BFS crawl of PluginHive tracking docs — slow, up to 100 pages",
        "default": False,
    },
    {
        "key": "shopify",
        "label": "Shopify App Store",
        "icon": "🛒",
        "description": "Shopify App Store listing page",
        "default": False,
    },
    {
        "key": "shopify_actions",
        "label": "Shopify Actions",
        "icon": "⚙️",
        "description": "Shopify CLI bulk order creation tool (requires SHOPIFY_ACTIONS_PATH)",
        "default": False,
    },
]

SOURCE_TYPES = ["pluginhive_seeds", "pluginhive_docs", "codebase", "pdf", "wiki",
                "shopify_app_store", "shopify_actions", "automation", "backend", "frontend"]


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def _check_ollama() -> tuple[bool, str]:
    try:
        r = requests.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=3)
        if r.status_code == 200:
            models = [m["name"] for m in r.json().get("models", [])]
            embed_ok = any(config.EMBEDDING_MODEL in m for m in models)
            if embed_ok:
                return True, f"Running — `{config.EMBEDDING_MODEL}` available"
            return True, f"Running — ⚠️ `{config.EMBEDDING_MODEL}` not found (pull it first)"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)


def _check_chromadb() -> tuple[bool, str, int]:
    try:
        client = chromadb.PersistentClient(path=config.CHROMA_PATH)
        collections = client.list_collections()
        names = [c.name for c in collections]
        if config.CHROMA_COLLECTION in names:
            col = client.get_collection(config.CHROMA_COLLECTION)
            count = col.count()
            return True, f"Connected — `{config.CHROMA_COLLECTION}` ({count} docs)", count
        return True, f"Connected — collection `{config.CHROMA_COLLECTION}` not yet created", 0
    except Exception as e:
        return False, str(e), 0


def _check_api_key() -> tuple[bool, str]:
    if config.ANTHROPIC_API_KEY and config.ANTHROPIC_API_KEY.startswith("sk-ant-"):
        masked = config.ANTHROPIC_API_KEY[:12] + "…" + config.ANTHROPIC_API_KEY[-4:]
        return True, masked
    if config.ANTHROPIC_API_KEY:
        return False, "Key present but unexpected format"
    return False, "Not set — add ANTHROPIC_API_KEY to .env"


# ---------------------------------------------------------------------------
# Source counts
# ---------------------------------------------------------------------------

def _get_all_source_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        from rag.vectorstore import get_source_count
        for st_key in SOURCE_TYPES:
            counts[st_key] = get_source_count(st_key)
    except Exception:
        pass
    return counts


# ---------------------------------------------------------------------------
# Path checks
# ---------------------------------------------------------------------------

def _check_paths() -> list[dict]:
    checks = [
        {"label": "Automation Codebase", "path": config.AUTOMATION_CODEBASE_PATH, "required": True},
        {"label": "PDF Test Cases", "path": config.PDF_TEST_CASES_PATH, "required": True},
        {"label": "Internal Wiki", "path": config.WIKI_PATH, "required": False},
        {"label": "Backend Code", "path": config.BACKEND_CODE_PATH, "required": False},
        {"label": "Frontend Code", "path": config.FRONTEND_CODE_PATH, "required": False},
        {"label": "Shopify Actions", "path": config.SHOPIFY_ACTIONS_PATH, "required": False},
    ]
    results = []
    for c in checks:
        path = c["path"]
        if not path:
            results.append({**c, "ok": not c["required"], "status": "Not configured"})
        elif Path(path).exists():
            results.append({**c, "ok": True, "status": "Found"})
        else:
            results.append({**c, "ok": not c["required"], "status": "Not found"})
    return results


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    with st.sidebar:
        st.title("🔧 Pipeline Dashboard")
        st.caption("Tracking App Domain Expert")
        st.divider()
        st.markdown("[💬 Open Domain Expert Chat](http://localhost:8502)", unsafe_allow_html=False)
        st.divider()
        st.subheader("Collections")
        st.caption(f"Main: `{config.CHROMA_COLLECTION}`")
        st.caption(f"Code: `{config.CHROMA_CODE_COLLECTION}`")
        st.divider()
        st.subheader("Models")
        st.caption(f"Expert: `{config.DOMAIN_EXPERT_MODEL}`")
        st.caption(f"Embed: `{config.EMBEDDING_MODEL}`")
        st.divider()
        st.subheader("RAG Settings")
        st.caption(f"Chunk size: `{config.CHUNK_SIZE}` chars")
        st.caption(f"Overlap: `{config.CHUNK_OVERLAP}` chars")
        st.caption(f"Top-K: `{config.TOP_K_RESULTS}`")
        st.caption(f"Memory window: `{config.MEMORY_WINDOW}` turns")

        # ── Code Knowledge Base ───────────────────────────────────────────
        st.divider()
        st.markdown("### 🗂️ Code Knowledge Base")
        st.caption("RAG over source code — TCs + automation scripts use real patterns.")

        from rag.code_indexer import get_index_stats, index_codebase, sync_from_git, get_repo_info
        _code_stats = get_index_stats()
        _auto_cnt   = _code_stats.get("automation", 0)
        _be_cnt     = _code_stats.get("backend", 0)
        _fe_cnt     = _code_stats.get("frontend", 0)
        _auto_sync  = _code_stats.get("automation_sync", {})
        _be_sync    = _code_stats.get("backend_sync", {})
        _fe_sync    = _code_stats.get("frontend_sync", {})

        def _sync_badge(cnt, sync):
            if cnt == 0:
                return "⬜ Not indexed"
            commit = sync.get("commit", "")
            synced = sync.get("synced_at", "")
            tag  = f" · `{commit}`" if commit else ""
            date = f" · {synced}" if synced else ""
            return f"✅ {cnt:,} chunks{tag}{date}"

        st.markdown(
            f"<div style='font-size:0.75rem;line-height:1.8'>"
            f"<b>🧪 Automation:</b> {_sync_badge(_auto_cnt, _auto_sync)}<br>"
            f"<b>🖥️ Backend:</b> {_sync_badge(_be_cnt, _be_sync)}<br>"
            f"<b>🌐 Frontend:</b> {_sync_badge(_fe_cnt, _fe_sync)}"
            f"</div>",
            unsafe_allow_html=True,
        )

        # ── Automation Code ───────────────────────────────────────────────
        with st.expander("🧪 Automation Code", expanded=(_auto_cnt == 0)):
            _auto_default = config.AUTOMATION_CODEBASE_PATH or ""
            _auto_path = st.text_input(
                "Automation repo path",
                value=st.session_state.get("automation_code_path", _auto_default),
                placeholder="/Users/you/projects/tracking-test-automation",
                key="automation_code_path_input",
            )
            st.caption("Spec files, POMs, helpers — used when writing automation scripts.")
            _auto_branch = None
            if _auto_path.strip():
                _auto_repo = get_repo_info(_auto_path.strip())
                if _auto_repo.get("branches"):
                    _auto_branch = st.selectbox(
                        "Branch to pull",
                        options=_auto_repo["branches"],
                        index=_auto_repo["branches"].index(_auto_repo["current_branch"])
                              if _auto_repo["current_branch"] in _auto_repo["branches"] else 0,
                        key="auto_branch_select",
                    )
                    st.caption(f"Current: `{_auto_repo['current_branch']}` @ `{_auto_repo['commit']}`")
            _ac1, _ac2 = st.columns(2)
            with _ac1:
                if st.button("🔄 Pull & Sync", key="sync_auto_btn",
                             use_container_width=True, type="primary",
                             disabled=not _auto_path.strip()):
                    st.session_state["automation_code_path"] = _auto_path.strip()
                    with st.spinner("git pull → syncing…"):
                        _res = sync_from_git(
                            _auto_path.strip(), source_type="automation",
                            branch=_auto_branch if _auto_branch != _auto_repo.get("current_branch") else None,
                        )
                    if _res.get("error"):
                        st.error(f"❌ {_res['error']}")
                    elif _res.get("message"):
                        st.info(f"ℹ️ {_res['message']}")
                    else:
                        st.success(
                            f"✅ `{_res['commit_before']}` → `{_res['commit_after']}` "
                            f"| {_res['files_changed']} changed, {_res['chunks_updated']} chunks"
                        )
                    st.rerun()
            with _ac2:
                if st.button("📥 Full Re-index", key="index_auto_btn",
                             use_container_width=True,
                             disabled=not _auto_path.strip()):
                    st.session_state["automation_code_path"] = _auto_path.strip()
                    with st.spinner("Indexing all automation files…"):
                        _res = index_codebase(
                            _auto_path.strip(), source_type="automation",
                            clear_existing=True, extensions=[".ts", ".tsx", ".js"],
                        )
                    if _res.get("error"):
                        st.error(f"❌ {_res['error']}")
                    else:
                        st.success(f"✅ {_res['files_indexed']} files → {_res['chunks_added']} chunks")
                    st.rerun()

        # ── Backend Code ──────────────────────────────────────────────────
        with st.expander("🖥️ Backend Code", expanded=(_be_cnt == 0)):
            _be_path = st.text_input(
                "Backend repo path",
                value=st.session_state.get("backend_code_path", config.BACKEND_CODE_PATH or ""),
                placeholder="/Users/you/projects/tracking-backend",
                key="be_repo_path",
            )
            _be_branch = None
            if _be_path.strip():
                _be_repo = get_repo_info(_be_path.strip())
                if _be_repo.get("branches"):
                    _be_branch = st.selectbox(
                        "Branch to pull",
                        options=_be_repo["branches"],
                        index=_be_repo["branches"].index(_be_repo["current_branch"])
                              if _be_repo["current_branch"] in _be_repo["branches"] else 0,
                        key="be_branch_select",
                    )
                    st.caption(f"Current: `{_be_repo['current_branch']}` @ `{_be_repo['commit']}`")
            _bc1, _bc2 = st.columns(2)
            with _bc1:
                if st.button("🔄 Pull & Sync", key="sync_be_btn",
                             use_container_width=True, type="primary",
                             disabled=not _be_path.strip()):
                    st.session_state["backend_code_path"] = _be_path.strip()
                    with st.spinner("git pull → syncing…"):
                        _res = sync_from_git(
                            _be_path.strip(), source_type="backend",
                            branch=_be_branch if _be_branch != _be_repo.get("current_branch") else None,
                        )
                    if _res.get("error"):
                        st.error(f"❌ {_res['error']}")
                    elif _res.get("message"):
                        st.info(f"ℹ️ {_res['message']}")
                    else:
                        st.success(
                            f"✅ `{_res['commit_before']}` → `{_res['commit_after']}` "
                            f"| {_res['files_changed']} changed, {_res['chunks_updated']} chunks"
                        )
                    st.rerun()
            with _bc2:
                if st.button("📥 Full Re-index", key="index_be_btn",
                             use_container_width=True,
                             disabled=not _be_path.strip()):
                    st.session_state["backend_code_path"] = _be_path.strip()
                    with st.spinner("Indexing all backend files…"):
                        _res = index_codebase(_be_path.strip(), source_type="backend", clear_existing=True)
                    if _res.get("error"):
                        st.error(f"❌ {_res['error']}")
                    else:
                        st.success(f"✅ {_res['files_indexed']} files → {_res['chunks_added']} chunks")
                    st.rerun()

        # ── Frontend Code ─────────────────────────────────────────────────
        with st.expander("🌐 Frontend Code", expanded=False):
            _fe_path = st.text_input(
                "Frontend repo path",
                value=st.session_state.get("frontend_code_path", config.FRONTEND_CODE_PATH or ""),
                placeholder="/Users/you/projects/tracking-frontend",
                key="fe_repo_path",
            )
            _fe_branch = None
            if _fe_path.strip():
                _fe_repo = get_repo_info(_fe_path.strip())
                if _fe_repo.get("branches"):
                    _fe_branch = st.selectbox(
                        "Branch to pull",
                        options=_fe_repo["branches"],
                        index=_fe_repo["branches"].index(_fe_repo["current_branch"])
                              if _fe_repo["current_branch"] in _fe_repo["branches"] else 0,
                        key="fe_branch_select",
                    )
                    st.caption(f"Current: `{_fe_repo['current_branch']}` @ `{_fe_repo['commit']}`")
            _fc1, _fc2 = st.columns(2)
            with _fc1:
                if st.button("🔄 Pull & Sync", key="sync_fe_btn",
                             use_container_width=True, type="primary",
                             disabled=not _fe_path.strip()):
                    st.session_state["frontend_code_path"] = _fe_path.strip()
                    with st.spinner("git pull → syncing…"):
                        _res = sync_from_git(
                            _fe_path.strip(), source_type="frontend",
                            branch=_fe_branch if _fe_branch != _fe_repo.get("current_branch") else None,
                        )
                    if _res.get("error"):
                        st.error(f"❌ {_res['error']}")
                    elif _res.get("message"):
                        st.info(f"ℹ️ {_res['message']}")
                    else:
                        st.success(
                            f"✅ `{_res['commit_before']}` → `{_res['commit_after']}` "
                            f"| {_res['files_changed']} changed, {_res['chunks_updated']} chunks"
                        )
                    st.rerun()
            with _fc2:
                if st.button("📥 Full Re-index", key="index_fe_btn",
                             use_container_width=True,
                             disabled=not _fe_path.strip()):
                    st.session_state["frontend_code_path"] = _fe_path.strip()
                    with st.spinner("Indexing all frontend files…"):
                        _res = index_codebase(_fe_path.strip(), source_type="frontend", clear_existing=True)
                    if _res.get("error"):
                        st.error(f"❌ {_res['error']}")
                    else:
                        st.success(f"✅ {_res['files_indexed']} files → {_res['chunks_added']} chunks")
                    st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _render_sidebar()

    st.title("🔧 TrackingAppDomainExpert Pipeline")
    st.caption("Monitor knowledge base health, trigger ingestion, and manage data sources.")

    tab_overview, tab_ingest, tab_sources, tab_config = st.tabs(
        ["📊 Overview", "▶️ Run Ingest", "🗂️ Sources", "⚙️ Config"]
    )

    # -----------------------------------------------------------------------
    # TAB 1 — Overview
    # -----------------------------------------------------------------------
    with tab_overview:
        st.subheader("System Health")

        col_ollama, col_chroma, col_key = st.columns(3)

        ollama_ok, ollama_msg = _check_ollama()
        chroma_ok, chroma_msg, total_docs = _check_chromadb()
        key_ok, key_msg = _check_api_key()

        with col_ollama:
            status = "✅" if ollama_ok else "❌"
            st.metric("Ollama (Embeddings)", status)
            st.caption(ollama_msg)

        with col_chroma:
            status = "✅" if chroma_ok else "❌"
            st.metric("ChromaDB", status)
            st.caption(chroma_msg)

        with col_key:
            status = "✅" if key_ok else "❌"
            st.metric("Anthropic API Key", status)
            st.caption(key_msg)

        st.divider()
        st.subheader("Knowledge Base — Document Counts")

        if total_docs == 0:
            st.warning("No documents indexed yet. Run ingestion to populate the knowledge base.")
        else:
            counts = _get_all_source_counts()
            active_counts = {k: v for k, v in counts.items() if v > 0}

            col_total, col_seeds, col_code, col_pdf, col_wiki = st.columns(5)
            with col_total:
                st.metric("Total Chunks", total_docs)
            with col_seeds:
                st.metric("PluginHive Seeds", active_counts.get("pluginhive_seeds", 0))
            with col_code:
                st.metric("Codebase", active_counts.get("codebase", 0))
            with col_pdf:
                st.metric("PDF Test Cases", active_counts.get("pdf", 0))
            with col_wiki:
                st.metric("Wiki", active_counts.get("wiki", 0))

            if active_counts:
                st.divider()
                st.subheader("Breakdown by Source Type")
                # Build bar chart data
                chart_data = {
                    "Source": list(active_counts.keys()),
                    "Chunks": list(active_counts.values()),
                }
                import pandas as pd
                df = pd.DataFrame(chart_data).set_index("Source")
                st.bar_chart(df)

        st.divider()
        st.subheader("Data Paths")
        path_checks = _check_paths()
        for pc in path_checks:
            icon = "✅" if pc["ok"] else ("❌" if pc["required"] else "⚠️")
            label = pc["label"]
            path = pc["path"] or "(not set)"
            status = pc["status"]
            st.markdown(f"{icon} **{label}** — `{path}` — *{status}*")

    # -----------------------------------------------------------------------
    # TAB 2 — Run Ingest
    # -----------------------------------------------------------------------
    with tab_ingest:
        st.subheader("Run Ingestion Pipeline")
        st.caption(
            "Select one or more sources and click **Run Ingest**. "
            "The pipeline will clear the existing collection and rebuild it."
        )

        st.warning(
            "⚠️ Running ingest **clears the entire collection** and rebuilds from selected sources. "
            "Make sure all required sources are checked.",
            icon="⚠️",
        )

        st.markdown("**Select Sources:**")
        selected_sources: list[str] = []
        for src in ALL_SOURCES:
            col_check, col_info = st.columns([1, 6])
            with col_check:
                checked = st.checkbox(
                    label=src["key"],
                    value=src["default"],
                    key=f"src_{src['key']}",
                    label_visibility="collapsed",
                )
            with col_info:
                st.markdown(f"{src['icon']} **{src['label']}** — {src['description']}")
            if checked:
                selected_sources.append(src["key"])

        st.divider()

        col_run, col_seeds_only = st.columns(2)

        with col_run:
            run_clicked = st.button(
                "▶️ Run Ingest (selected sources)",
                type="primary",
                use_container_width=True,
                disabled=len(selected_sources) == 0,
            )

        with col_seeds_only:
            seeds_clicked = st.button(
                "⚡ Quick Refresh (seeds only)",
                use_container_width=True,
                help="Fastest option — re-scrapes only the 3 PluginHive seed pages",
            )

        if run_clicked or seeds_clicked:
            sources_to_run = ["pluginhive_seeds"] if seeds_clicked else selected_sources
            st.info(f"Running ingest for: `{', '.join(sources_to_run)}`")

            log_container = st.empty()
            progress_bar = st.progress(0, text="Starting ingestion…")

            cmd = [
                sys.executable, "-m", "ingest.run_ingest",
                "--sources", *sources_to_run,
            ]

            start_time = time.time()
            with st.spinner("Ingesting… this may take a few minutes."):
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(config.BASE_DIR),
                )
            elapsed = time.time() - start_time
            progress_bar.progress(1.0, text="Done")

            if result.returncode == 0:
                st.success(f"✅ Ingestion complete in {elapsed:.1f}s")
                output_lines = result.stdout.strip()
                if output_lines:
                    with st.expander("📋 Ingestion Output", expanded=True):
                        st.code(output_lines, language="text")
            else:
                st.error("❌ Ingestion failed")
                with st.expander("📋 Error Output", expanded=True):
                    st.code(result.stderr or result.stdout or "(no output)", language="text")

    # -----------------------------------------------------------------------
    # TAB 3 — Sources (manage individual source types)
    # -----------------------------------------------------------------------
    with tab_sources:
        st.subheader("Manage Individual Sources")
        st.caption("View chunk counts per source and selectively delete source data from ChromaDB.")

        counts = _get_all_source_counts()

        if not any(counts.values()):
            st.info("No documents indexed yet.")
        else:
            for src in ALL_SOURCES:
                skey = src["key"]
                # Map ingest key → source_type stored in metadata
                meta_key = skey  # same for most; shopify → shopify_app_store
                if skey == "shopify":
                    meta_key = "shopify_app_store"

                count = counts.get(meta_key, 0)
                col_icon, col_label, col_count, col_btn = st.columns([1, 4, 2, 2])

                with col_icon:
                    st.markdown(src["icon"])
                with col_label:
                    st.markdown(f"**{src['label']}**")
                with col_count:
                    st.markdown(f"`{count}` chunks")
                with col_btn:
                    if count > 0:
                        if st.button(
                            "🗑️ Delete",
                            key=f"del_{skey}",
                            help=f"Delete all {count} chunks with source_type={meta_key!r}",
                        ):
                            from rag.vectorstore import delete_by_source_type
                            deleted = delete_by_source_type(meta_key)
                            st.success(f"Deleted {deleted} chunks from `{meta_key}`")
                            st.rerun()
                    else:
                        st.caption("—")

        st.divider()
        st.subheader("Danger Zone")
        if st.button("🔥 Clear Entire Collection", type="secondary", use_container_width=False):
            st.session_state["confirm_clear"] = True

        if st.session_state.get("confirm_clear"):
            st.error("This will delete ALL indexed documents. This cannot be undone.")
            col_yes, col_no = st.columns(2)
            with col_yes:
                if st.button("✅ Yes, clear everything", type="primary"):
                    from rag.vectorstore import clear_collection
                    clear_collection()
                    st.session_state["confirm_clear"] = False
                    st.success("Collection cleared.")
                    st.rerun()
            with col_no:
                if st.button("❌ Cancel"):
                    st.session_state["confirm_clear"] = False
                    st.rerun()

    # -----------------------------------------------------------------------
    # TAB 4 — Config
    # -----------------------------------------------------------------------
    with tab_config:
        st.subheader("Active Configuration")
        st.caption("Values loaded from `.env` at project root. Edit `.env` to change.")

        st.markdown("**Claude Models**")
        col1, col2 = st.columns(2)
        with col1:
            st.text_input("Domain Expert Model", value=config.DOMAIN_EXPERT_MODEL, disabled=True)
            st.text_input("Sonnet Model", value=config.CLAUDE_SONNET_MODEL, disabled=True)
        with col2:
            st.text_input("Haiku Model", value=config.CLAUDE_HAIKU_MODEL, disabled=True)
            st.text_input("Embedding Model", value=config.EMBEDDING_MODEL, disabled=True)

        st.divider()
        st.markdown("**Paths**")
        st.text_input("ChromaDB Path", value=config.CHROMA_PATH, disabled=True)
        st.text_input("Automation Codebase", value=config.AUTOMATION_CODEBASE_PATH, disabled=True)
        st.text_input("PDF Test Cases", value=config.PDF_TEST_CASES_PATH, disabled=True)
        st.text_input("Wiki Path", value=config.WIKI_PATH or "(not set)", disabled=True)
        st.text_input("Backend Code Path", value=config.BACKEND_CODE_PATH or "(not set)", disabled=True)
        st.text_input("Frontend Code Path", value=config.FRONTEND_CODE_PATH or "(not set)", disabled=True)

        st.divider()
        st.markdown("**RAG Settings**")
        col_r1, col_r2, col_r3, col_r4 = st.columns(4)
        with col_r1:
            st.metric("Chunk Size", config.CHUNK_SIZE)
        with col_r2:
            st.metric("Chunk Overlap", config.CHUNK_OVERLAP)
        with col_r3:
            st.metric("Top-K Results", config.TOP_K_RESULTS)
        with col_r4:
            st.metric("Memory Window", config.MEMORY_WINDOW)

        st.divider()
        st.markdown("**Seed URLs**")
        for url in config.PLUGINHIVE_SEED_URLS:
            st.caption(url)


if __name__ == "__main__":
    main()
