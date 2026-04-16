"""
Tracking App Domain Expert — Pipeline Dashboard
Run with: PYTHONPATH=. streamlit run ui/pipeline_dashboard.py
"""
from __future__ import annotations
import datetime
import json
import logging
import subprocess
import sys
import time
import threading
from pathlib import Path

# Ensure project root is on sys.path when launched via `streamlit run ui/pipeline_dashboard.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import re

import chromadb
import requests
import streamlit as st

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# History & draft persistence
# ---------------------------------------------------------------------------

_HISTORY_FILE   = Path(__file__).resolve().parent.parent / "data" / "pipeline_history.json"
_AC_DRAFTS_FILE = Path(__file__).resolve().parent.parent / "data" / "ac_drafts.json"


def _load_history() -> dict:
    try:
        if _HISTORY_FILE.exists():
            return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_history(runs: dict) -> None:
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(json.dumps(runs, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as _e:
        logger.warning("Could not save history: %s", _e)


def _load_ac_drafts() -> dict:
    try:
        if _AC_DRAFTS_FILE.exists():
            return json.loads(_AC_DRAFTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_ac_draft(card_id: str, ac_text: str, card_name: str = "") -> None:
    try:
        _AC_DRAFTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        drafts = _load_ac_drafts()
        drafts[card_id] = {"card_name": card_name, "ac_text": ac_text}
        _AC_DRAFTS_FILE.write_text(json.dumps(drafts, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as _e:
        logger.warning("Could not save AC draft: %s", _e)


def _delete_ac_draft(card_id: str) -> None:
    try:
        if _AC_DRAFTS_FILE.exists():
            drafts = _load_ac_drafts()
            drafts.pop(card_id, None)
            _AC_DRAFTS_FILE.write_text(json.dumps(drafts, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as _e:
        logger.warning("Could not delete AC draft: %s", _e)


def _init_state():
    if "pipeline_runs" not in st.session_state:
        st.session_state.pipeline_runs = _load_history()
    if "trello_connected" not in st.session_state:
        st.session_state.trello_connected = False
    if "ac_drafts_loaded" not in st.session_state:
        for _cid, _val in _load_ac_drafts().items():
            _key = f"ac_suggestion_{_cid}"
            if _key not in st.session_state:
                _ac = _val["ac_text"] if isinstance(_val, dict) else _val
                st.session_state[_key] = _ac
        st.session_state["ac_drafts_loaded"] = True


def _get_repo_branches() -> list[str]:
    """Return all branches in the automation repo."""
    try:
        result = subprocess.run(
            ["git", "branch", "-a", "--format=%(refname:short)"],
            cwd=config.AUTOMATION_CODEBASE_PATH,
            capture_output=True, text=True, timeout=10,
        )
        branches = []
        for b in result.stdout.splitlines():
            b = b.strip().removeprefix("origin/")
            if b and b != "HEAD" and b not in branches:
                branches.append(b)
        return sorted(branches)
    except Exception:
        return []


def _step_header(num: str, title: str) -> None:
    """Render a numbered step header."""
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:10px;margin:14px 0 6px 0">'
        f'<div style="background:#6366f1;color:white;border-radius:50%;width:24px;height:24px;'
        f'display:flex;align-items:center;justify-content:center;font-size:0.72rem;font-weight:700">{num}</div>'
        f'<div style="font-weight:600;font-size:0.95rem;color:#1e293b">{title}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


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
# Trello helpers
# ---------------------------------------------------------------------------

def _status_badge(label: str, ok: bool, hint: str = "") -> str:
    icon = "✅" if ok else "❌"
    detail = f"<br><span style='font-size:0.7rem;color:#9ca3af'>{hint}</span>" if (not ok and hint) else ""
    return f"<div style='font-size:0.8rem;margin:2px 0'>{icon} {label}{detail}</div>"


@st.cache_data(ttl=120)
def _get_board_lists() -> list[tuple[str, str]]:
    """Cached fetch of all Trello board lists — returns (name, id) pairs."""
    from pipeline.trello_client import TrelloClient
    return [(l.name, l.id) for l in TrelloClient().get_lists()]


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    trello_ok = all([config.TRELLO_API_KEY, config.TRELLO_TOKEN, config.TRELLO_BOARD_ID])
    sheets_ok = bool(os.path.exists(config.GOOGLE_CREDENTIALS_PATH))

    with st.sidebar:
        st.title("🔧 Pipeline Dashboard")
        st.caption("Tracking App Domain Expert")
        st.divider()

        st.markdown("### ⚙️ System Status")
        st.markdown(
            _status_badge("Claude API", bool(config.ANTHROPIC_API_KEY), "Set ANTHROPIC_API_KEY") +
            _status_badge("Trello", trello_ok, "Set TRELLO_* in .env") +
            _status_badge("Google Sheets", sheets_ok, "Add credentials.json") +
            _status_badge("Ollama Embeddings", True),
            unsafe_allow_html=True,
        )

        st.divider()

        # ── Pipeline progress summary ──────────────────────────────────────
        cards      = st.session_state.get("rqa_cards", [])
        approved   = st.session_state.get("rqa_approved", {})
        tc_store   = st.session_state.get("rqa_test_cases", {})
        current_release = st.session_state.get("rqa_release", "")
        n_cards    = len(cards)
        n_approved = sum(1 for c in cards if approved.get(c.id))
        n_tc       = sum(1 for c in cards if c.id in tc_store)
        n_auto     = sum(1 for c in cards if st.session_state.get(f"automation_{c.id}"))
        last_run   = st.session_state.get("last_run_result")

        if current_release:
            st.markdown(f"**📦 Release:** `{current_release}`")
            st.markdown(
                f"""
                <div style="margin-top:8px">
                <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-bottom:4px">
                    <span>📋 Cards</span><strong>{n_cards}</strong>
                </div>
                <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-bottom:4px">
                    <span>🤖 Test cases</span><strong>{n_tc}/{n_cards}</strong>
                </div>
                <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-bottom:4px">
                    <span>✅ Approved</span><strong>{n_approved}/{n_cards}</strong>
                </div>
                <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-bottom:4px">
                    <span>⚙️ Automation</span><strong>{n_auto}/{n_cards}</strong>
                </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if n_cards > 0:
                st.progress(n_approved / n_cards, text=f"{n_approved}/{n_cards} approved")

            if last_run and getattr(last_run, "release", "") == current_release:
                icon = "✅" if last_run.failed == 0 else "❌"
                st.markdown(
                    f"**{icon} Last run:** {last_run.passed}/{last_run.total} passed"
                    f" · {last_run.duration_secs:.0f}s"
                )
        else:
            st.caption("Load a release in 🚀 Release QA to see progress.")

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
    _init_state()
    _render_sidebar()

    trello_ok = all([config.TRELLO_API_KEY, config.TRELLO_TOKEN, config.TRELLO_BOARD_ID])
    sheets_ok = bool(os.path.exists(config.GOOGLE_CREDENTIALS_PATH))

    current_release = st.session_state.get("rqa_release", "")
    release_badge = (
        f"&nbsp;·&nbsp;<span style='color:#818cf8;font-size:0.85rem'>{current_release}</span>"
        if current_release else ""
    )
    st.markdown(
        f"<h1>🔧 TrackingApp QA Pipeline{release_badge}</h1>",
        unsafe_allow_html=True,
    )
    st.caption("Trello card → AC → Test Cases → Automation → Run → Sign Off")

    tab_overview, tab_ingest, tab_sources, tab_release, tab_us, tab_signoff, tab_run, tab_config = st.tabs(
        ["📊 Overview", "▶️ Run Ingest", "🗂️ Sources", "🚀 Release QA",
         "📝 User Story", "✅ Sign Off", "▶️ Run Automation", "⚙️ Config"]
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
    # TAB 4 — Release QA (Trello card loading)
    # -----------------------------------------------------------------------
    with tab_release:
        if not config.ANTHROPIC_API_KEY:
            st.error("❌ ANTHROPIC_API_KEY not set — add it to .env")
        elif not trello_ok:
            st.error("❌ Trello credentials missing — set TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID in .env")
        else:
            from pipeline.trello_client import TrelloClient

            if not sheets_ok:
                st.info(
                    "ℹ️ Google Sheets not connected — test cases will save to Trello only. "
                    "Add `credentials.json` to enable sheet sync."
                )

            # ── ① Select Release ──────────────────────────────────────────
            st.markdown("**① Select Release**")

            if st.button("🔄 Refresh Trello lists", use_container_width=False):
                st.cache_data.clear()
                st.rerun()

            col_list, col_load = st.columns([4, 1])
            with col_list:
                try:
                    all_lists = _get_board_lists()

                    show_all = st.toggle("Show all lists", value=False)
                    if show_all:
                        filtered_lists = all_lists
                    else:
                        filtered_lists = [
                            (name, lid) for name, lid in all_lists
                            if "ready for qa" in name.lower() or "qa" in name.lower()
                        ]

                    if not filtered_lists:
                        filtered_lists = all_lists

                    list_names = [name for name, _ in filtered_lists]
                    default_idx = next(
                        (i for i, n in enumerate(list_names)
                         if "tracking" in n.lower() and "ready for qa" in n.lower()), 0
                    )
                    selected_list_name = st.selectbox(
                        f"Select release list ({len(list_names)} lists)",
                        list_names,
                        index=default_idx,
                    )
                    selected_list_id = next(
                        lid for name, lid in filtered_lists if name == selected_list_name
                    )

                except Exception as e:
                    st.error(f"❌ Could not fetch Trello lists: {e}")
                    selected_list_name = ""
                    selected_list_id = ""

            with col_load:
                st.write("")
                st.write("")
                load_btn = st.button(
                    "📥 Load Cards",
                    use_container_width=True,
                    disabled=not selected_list_id,
                )

            # ── Release version label ──────────────────────────────────────
            def _extract_release(list_name: str) -> str:
                m = re.search(r'(tracking\w*\s+[\d.]+)', list_name, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
                m2 = re.search(r'(v?[\d]+\.[\d]+[\d.]*)', list_name)
                return m2.group(1) if m2 else list_name

            if selected_list_name:
                release_label = st.text_input(
                    "🏷️ Release version",
                    value=_extract_release(selected_list_name),
                    placeholder="e.g. TrackingApp 2.3.116",
                    help="Recorded in the 'Release' column of the master sheet",
                )
            else:
                release_label = ""

            # ── Load cards + auto-validate ────────────────────────────────
            if load_btn and selected_list_id:
                from pipeline.domain_validator import validate_card
                trello = TrelloClient()
                with st.spinner(f"Loading cards from **{selected_list_name}**…"):
                    cards = trello.get_cards_in_list(selected_list_id)
                st.session_state["rqa_cards"] = cards
                st.session_state["rqa_list_name"] = selected_list_name
                st.session_state["rqa_release"] = release_label
                st.session_state["rqa_test_cases"] = {}
                st.session_state["rqa_approved"] = {}
                # Clear old validations
                for c in cards:
                    st.session_state.pop(f"validation_{c.id}", None)

                # Auto-validate all cards
                st.info(f"Loaded {len(cards)} cards — running Domain Expert validation…")
                progress = st.progress(0)
                for idx, c in enumerate(cards):
                    with st.spinner(f"🧠 Validating '{c.name}'…"):
                        st.session_state[f"validation_{c.id}"] = validate_card(
                            card_name=c.name,
                            card_desc=c.desc or "",
                            acceptance_criteria=c.desc or "",
                        )
                    progress.progress((idx + 1) / len(cards))
                progress.empty()

                # Cross-card release analysis
                from pipeline.release_analyser import analyse_release, CardSummary as RASummary
                ra_cards = [
                    RASummary(card_id=c.id, card_name=c.name, card_desc=c.desc or "")
                    for c in cards
                ]
                with st.spinner("🔬 Running cross-card release analysis…"):
                    st.session_state["release_analysis"] = analyse_release(
                        release_name=release_label,
                        cards=ra_cards,
                    )
                st.rerun()

            # ── Display loaded cards ───────────────────────────────────────
            if st.session_state.get("rqa_cards"):
                from pipeline.domain_validator import validate_card, ValidationReport
                cards = st.session_state["rqa_cards"]
                loaded_list = st.session_state.get("rqa_list_name", "")

                # ── Health summary metrics ─────────────────────────────────
                st.divider()
                val_statuses = [st.session_state.get(f"validation_{c.id}") for c in cards]
                n_pass   = sum(1 for v in val_statuses if v and v.overall_status == "PASS")
                n_review = sum(1 for v in val_statuses if v and v.overall_status == "NEEDS_REVIEW")
                n_fail   = sum(1 for v in val_statuses if v and v.overall_status == "FAIL")

                tc_store      = st.session_state.setdefault("rqa_test_cases", {})
                approved_store = st.session_state.setdefault("rqa_approved", {})
                current_release = st.session_state.get("rqa_release", "")
                approved_count = sum(1 for v in approved_store.values() if v)

                hcols = st.columns(5)
                hcols[0].metric("📦 Total Cards", len(cards))
                hcols[1].metric("🟢 Pass", n_pass)
                hcols[2].metric("🟡 Needs Review", n_review)
                hcols[3].metric("🔴 Fail", n_fail)
                hcols[4].metric("✅ Approved", approved_count)

                # ── Release Intelligence (cross-card analysis) ─────────────
                from pipeline.release_analyser import ReleaseAnalysis
                ra: ReleaseAnalysis | None = st.session_state.get("release_analysis")
                if ra and not ra.error:
                    risk_colors = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}
                    risk_icon = risk_colors.get(ra.risk_level, "⚪")
                    with st.expander(
                        f"{risk_icon} **Release Intelligence — {ra.risk_level} RISK** · {ra.risk_summary}",
                        expanded=True,
                    ):
                        if ra.kb_context_summary:
                            st.info(f"📚 **KB Context:** {ra.kb_context_summary}")

                        col_left, col_right = st.columns(2)

                        with col_left:
                            if ra.conflicts:
                                st.markdown("##### ⚠️ Cross-Card Conflicts")
                                for conflict in ra.conflicts:
                                    cards_involved = " & ".join(conflict.get("cards", []))
                                    area = conflict.get("area", "")
                                    desc = conflict.get("description", "")
                                    st.warning(f"**{cards_involved}** — *{area}*\n\n{desc}")
                            else:
                                st.success("✅ No cross-card conflicts detected")

                            if ra.coverage_gaps:
                                st.markdown("##### 🕳️ Coverage Gaps")
                                for gap in ra.coverage_gaps:
                                    st.caption(f"• {gap}")

                        with col_right:
                            if ra.ordering:
                                st.markdown("##### 📋 Suggested Test Order")
                                for o in ra.ordering:
                                    pos = o.get("position", "")
                                    cname = o.get("card_name", "")
                                    reason = o.get("reason", "")
                                    st.markdown(f"**{pos}.** {cname}")
                                    st.caption(f"   ↳ {reason}")

                        if ra.sources:
                            st.caption(
                                "KB sources: " + " · ".join(
                                    f"[link]({s})" if s.startswith("http") else s
                                    for s in ra.sources[:4]
                                )
                            )
                elif ra and ra.error:
                    st.warning(f"⚠️ Release analysis incomplete: {ra.error}")

                st.divider()
                st.subheader(f"Cards — {loaded_list}")

                from pipeline.card_processor import (
                    generate_test_cases, regenerate_with_feedback, write_test_cases_to_card
                )
                from pipeline.sheets_writer import (
                    append_to_sheet, detect_tab, SHEET_TABS,
                    check_duplicates, parse_test_cases_to_rows,
                )

                for card in cards:
                    is_approved = approved_store.get(card.id, False)
                    vr: ValidationReport | None = st.session_state.get(f"validation_{card.id}")
                    val_icon = {"PASS": "🟢", "NEEDS_REVIEW": "🟡", "FAIL": "🔴"}.get(
                        vr.overall_status if vr else "", "⚪"
                    )
                    appr_icon = "✅ " if is_approved else ""

                    with st.expander(f"{appr_icon}{val_icon} {card.name}", expanded=not is_approved):

                        # Card meta row
                        col_a, col_b = st.columns([5, 1])
                        with col_b:
                            if card.labels:
                                for lb in card.labels:
                                    st.badge(lb)
                            if card.url:
                                st.markdown(f"[Open in Trello ↗]({card.url})")

                        with col_a:
                            # ── Step 1: Requirements ───────────────────────
                            st.markdown("**📋 Requirements**")
                            if card.desc:
                                st.markdown(card.desc[:600] + ("…" if len(card.desc) > 600 else ""))
                            else:
                                st.caption("_(No description on this card)_")

                        # ── Step 2: Domain Expert Validation ───────────────
                        st.markdown("---")
                        st.markdown("**🧠 Domain Expert Validation**")

                        if vr:
                            status_color = {"PASS": "🟢", "NEEDS_REVIEW": "🟡", "FAIL": "🔴"}.get(
                                vr.overall_status, "⚪"
                            )
                            st.markdown(f"{status_color} **{vr.overall_status}** — {vr.summary}")

                            if vr.kb_insights:
                                with st.expander("📚 Knowledge Base context", expanded=False):
                                    st.markdown(vr.kb_insights)
                                    if vr.sources:
                                        st.caption("Sources: " + " · ".join(
                                            f"[link]({s})" if s.startswith("http") else s
                                            for s in vr.sources[:4]
                                        ))

                            has_issues = any([vr.requirement_gaps, vr.ac_gaps,
                                              vr.accuracy_issues, vr.suggestions])
                            if has_issues:
                                c1, c2 = st.columns(2)
                                with c1:
                                    if vr.accuracy_issues:
                                        st.error("**❌ Accuracy Issues**")
                                        for issue in vr.accuracy_issues:
                                            st.markdown(f"- {issue}")
                                    if vr.requirement_gaps:
                                        st.warning("**⚠️ Requirement Gaps**")
                                        for gap in vr.requirement_gaps:
                                            st.markdown(f"- {gap}")
                                with c2:
                                    if vr.ac_gaps:
                                        st.warning("**📋 Missing AC Scenarios**")
                                        for gap in vr.ac_gaps:
                                            st.markdown(f"- {gap}")
                                    if vr.suggestions:
                                        st.info("**💡 Suggestions**")
                                        for s in vr.suggestions:
                                            st.markdown(f"- {s}")

                                st.caption("👆 Fix the card on Trello, then re-validate below")
                                if st.button("🔄 Re-validate after fix", key=f"reval_{card.id}"):
                                    with st.spinner("Fetching updated card from Trello…"):
                                        fresh = TrelloClient().get_card(card.id)
                                    with st.spinner("Re-validating…"):
                                        st.session_state[f"validation_{card.id}"] = validate_card(
                                            card_name=fresh.name,
                                            card_desc=fresh.desc or "",
                                            acceptance_criteria=fresh.desc or "",
                                        )
                                        card.desc = fresh.desc
                                    st.rerun()
                            else:
                                st.success("✅ Requirements & AC look complete")

                        else:
                            if st.button("🧠 Validate", key=f"val_{card.id}"):
                                with st.spinner("Validating…"):
                                    st.session_state[f"validation_{card.id}"] = validate_card(
                                        card_name=card.name,
                                        card_desc=card.desc or "",
                                        acceptance_criteria=card.desc or "",
                                    )
                                st.rerun()

                        if card.checklists:
                            st.markdown("---")
                            for cl in card.checklists:
                                st.markdown(f"**☑ {cl['name']}**")
                                for item in cl.get("items", []):
                                    icon = "✅" if item["state"] == "complete" else "⬜"
                                    st.caption(f"{icon} {item['name']}")

                        # ── STEP 3: Generate Test Cases ─────────────────────
                        if not is_approved:
                            st.markdown("---")
                            _step_header("3", "Generate Test Cases")
                            tc = tc_store.get(card.id)

                            if not tc:
                                col_gen, col_ctx = st.columns([2, 3])
                                with col_ctx:
                                    extra_ctx = st.text_input(
                                        "Extra context (optional)",
                                        placeholder="e.g. focus on carrier-specific notification edge cases",
                                        key=f"tc_ctx_{card.id}",
                                        label_visibility="collapsed",
                                    )
                                with col_gen:
                                    if st.button("🤖 Generate Test Cases", key=f"gen_tc_{card.id}",
                                                 use_container_width=True, type="primary"):
                                        with st.spinner("Claude is generating test cases…"):
                                            tc_store[card.id] = generate_test_cases(
                                                card, extra_context=extra_ctx
                                            )
                                        st.rerun()
                            else:
                                st.markdown(tc)

                                col_fb, col_regen = st.columns([3, 1])
                                with col_fb:
                                    feedback = st.text_input(
                                        "✏️ Request changes",
                                        placeholder="e.g. Add edge case for expired tracking number",
                                        key=f"feedback_{card.id}",
                                    )
                                with col_regen:
                                    st.write("")
                                    if st.button("🔄 Regenerate", key=f"regen_{card.id}",
                                                 use_container_width=True):
                                        if feedback.strip():
                                            with st.spinner("Updating test cases…"):
                                                tc_store[card.id] = regenerate_with_feedback(
                                                    card, tc, feedback
                                                )
                                            st.rerun()
                                        else:
                                            st.warning("Type your feedback first")

                                # ── STEP 4: Approve & Save ─────────────────────
                                st.markdown("---")
                                _step_header("4", "Review & Approve")

                                if sheets_ok:
                                    suggested_tab = detect_tab(card.name, tc)
                                    tab_idx = list(SHEET_TABS).index(suggested_tab) if suggested_tab in SHEET_TABS else 0
                                    chosen_tab = st.selectbox(
                                        "📊 Add to sheet tab",
                                        list(SHEET_TABS),
                                        index=tab_idx,
                                        key=f"tab_{card.id}",
                                    )
                                else:
                                    chosen_tab = "Draft Plan"

                                col_approve, col_skip = st.columns(2)
                                with col_approve:
                                    if st.button("✅ Approve & Save", key=f"approve_{card.id}",
                                                 use_container_width=True, type="primary"):
                                        with st.spinner("Saving to Trello…"):
                                            write_test_cases_to_card(
                                                card.id, tc, TrelloClient(),
                                                release=current_release,
                                                card_name=card.name,
                                            )

                                        if sheets_ok:
                                            with st.spinner(f"Adding to '{chosen_tab}' sheet…"):
                                                try:
                                                    result = append_to_sheet(
                                                        card_name=card.name,
                                                        test_cases_markdown=tc,
                                                        tab_name=chosen_tab,
                                                        release=current_release,
                                                    )
                                                    st.success(
                                                        f"✅ Saved! [{result['rows_added']} rows → '{result['tab']}']"
                                                        f"({result['sheet_url']})"
                                                    )
                                                except Exception as _e:
                                                    st.warning(f"Trello saved ✅ but Sheets failed: {_e}")
                                        else:
                                            st.success("✅ Saved to Trello card!")

                                        approved_store[card.id] = True

                                        # Update RAG knowledge base
                                        try:
                                            from pipeline.rag_updater import update_rag_from_card
                                            with st.spinner("📚 Updating knowledge base…"):
                                                rag_result = update_rag_from_card(
                                                    card_id=card.id,
                                                    card_name=card.name,
                                                    description=card.desc or "",
                                                    acceptance_criteria=card.desc or "",
                                                    test_cases=tc,
                                                    release=current_release,
                                                )
                                            st.caption(f"📚 {rag_result.get('chunks_added', 0)} RAG chunks added")
                                        except Exception:
                                            pass

                                        # Save to History
                                        st.session_state.pipeline_runs[card.id] = {
                                            "card_name":   card.name,
                                            "card_url":    card.url or "",
                                            "release":     current_release,
                                            "test_cases":  tc[:500],
                                            "approved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                        }
                                        _save_history(st.session_state.pipeline_runs)
                                        st.rerun()

                                with col_skip:
                                    if st.button("⏭️ Skip — use existing", key=f"skip_approve_{card.id}",
                                                 use_container_width=True):
                                        approved_store[card.id] = True
                                        st.rerun()
                        else:
                            # Already approved — show Write Automation option
                            st.success("✅ Approved and saved to Trello")
                            tc = tc_store.get(card.id, "")

                            _step_header("5", "Write Automation Code")
                            auto_key = f"automation_{card.id}"
                            auto_result = st.session_state.get(auto_key)

                            if auto_result:
                                files = auto_result.get("files_written", [])
                                branch = auto_result.get("branch", "")
                                err = auto_result.get("error", "")
                                if err:
                                    st.error(f"❌ Automation failed: {err}")
                                    if st.button("🔄 Retry", key=f"retry_auto_{card.id}"):
                                        del st.session_state[auto_key]
                                        st.rerun()
                                else:
                                    st.success(f"✅ {len(files)} file(s) written")
                                    for f in files:
                                        st.caption(f"  📄 `{f}`")
                                    if branch:
                                        st.info(f"📦 Branch: `{branch}`")
                            else:
                                col_auto, col_branch = st.columns([3, 2])
                                with col_branch:
                                    _NEW_BRANCH_OPTION = "➕ New branch…"
                                    existing_branches = _get_repo_branches()
                                    default_slug = f"automation/{re.sub(r'[^a-z0-9]+', '-', card.name.lower()).strip('-')[:30]}"
                                    branch_options = existing_branches + [_NEW_BRANCH_OPTION]
                                    default_idx = branch_options.index(default_slug) if default_slug in branch_options else len(branch_options) - 1
                                    selected_branch = st.selectbox(
                                        "Branch",
                                        options=branch_options,
                                        index=default_idx,
                                        key=f"branch_select_{card.id}",
                                        label_visibility="collapsed",
                                    )
                                    auto_branch_input = (
                                        st.text_input("New branch name", value=default_slug,
                                                      key=f"branch_input_{card.id}", label_visibility="collapsed")
                                        if selected_branch == _NEW_BRANCH_OPTION
                                        else selected_branch
                                    )
                                with col_auto:
                                    dry_auto = st.checkbox("Dry run", key=f"dry_auto_{card.id}", value=False)
                                    push_auto = st.checkbox("Push to origin", key=f"push_auto_{card.id}")

                                qa_context_key = f"qa_ctx_{card.id}"
                                qa_context_val = st.text_area(
                                    "🧪 QA Test Context (optional)",
                                    key=qa_context_key,
                                    placeholder=(
                                        "e.g. Use tracking number TRK123456 for UPS\n"
                                        "Test with carrier 'USPS' on notification template 'Shipped'"
                                    ),
                                    height=80,
                                )

                                auto_fix_enabled = st.toggle(
                                    "🔄 Auto-run & fix until passing",
                                    key=f"auto_fix_{card.id}",
                                    value=False,
                                )

                                if st.button("⚙️ Write Automation Code", key=f"auto_{card.id}",
                                             use_container_width=True, type="primary"):
                                    from pipeline.automation_writer import write_automation
                                    fix_status_placeholder = st.empty()
                                    def _on_fix_progress(iteration, status, output, _ph=fix_status_placeholder):
                                        _ph.info(f"🔄 **Auto-fix iteration {iteration}/3** — {status}")
                                    with st.spinner("✍️ Claude is writing Playwright tests…"):
                                        result = write_automation(
                                            card_name=card.name,
                                            test_cases_markdown=tc,
                                            acceptance_criteria=card.desc or "",
                                            branch_name=auto_branch_input,
                                            dry_run=dry_auto,
                                            push=push_auto,
                                            qa_context=qa_context_val,
                                            auto_fix=auto_fix_enabled,
                                            on_fix_progress=_on_fix_progress if auto_fix_enabled else None,
                                        )
                                    st.session_state[auto_key] = result
                                    st.rerun()

            elif not load_btn:
                st.info("Select a release list above and click **📥 Load Cards** to begin.")

    # -----------------------------------------------------------------------
    # TAB 5 — User Story Writer
    # -----------------------------------------------------------------------
    with tab_us:
        st.subheader("📝 User Story Writer")
        st.caption("Describe what you need — AI generates a User Story + Acceptance Criteria using the codebase and domain knowledge.")

        feature_request = st.text_area(
            "What do you want to build?",
            placeholder="e.g. We need to allow merchants to configure custom notification delays per carrier...",
            height=150,
            key="us_feature_request",
        )

        us_key = "generated_user_story"
        col_gen, col_reset = st.columns([2, 1])
        with col_gen:
            if st.button("🤖 Generate", key="us_gen_btn", type="primary", disabled=not (feature_request or "").strip()):
                from pipeline.user_story_writer import generate_user_story
                with st.spinner("Claude is writing the User Story…"):
                    result = generate_user_story(feature_request)
                st.session_state[us_key] = result
                st.rerun()
        with col_reset:
            if st.button("🔄 Start Over", key="us_reset"):
                st.session_state.pop(us_key, None)
                st.rerun()

        if st.session_state.get(us_key):
            st.divider()
            st.markdown(st.session_state[us_key])

            st.divider()
            st.markdown("**✏️ Refine**")
            change_req = st.text_area("What to change?", key="us_change_req", height=80)
            if st.button("✏️ Refine User Story", key="us_refine_btn", disabled=not (change_req or "").strip()):
                from pipeline.user_story_writer import refine_user_story
                with st.spinner("Refining…"):
                    refined = refine_user_story(st.session_state[us_key], change_req)
                st.session_state[us_key] = refined
                st.rerun()

            if trello_ok:
                st.divider()
                st.markdown("**📤 Push to Trello**")
                col_tl1, col_tl2 = st.columns(2)
                with col_tl1:
                    target_list = st.text_input("Trello list", value="Iteration Backlog", key="us_trello_list")
                with col_tl2:
                    card_title = st.text_input("Card title", value=(feature_request or "")[:80], key="us_card_title")
                if st.button("📤 Push to Trello", key="us_push_trello", type="primary", disabled=not (card_title or "").strip()):
                    from pipeline.trello_client import TrelloClient as _TC
                    with st.spinner("Creating Trello card…"):
                        _tc_client = _TC()
                        _new_card = _tc_client.create_card(
                            list_name=target_list,
                            name=card_title,
                            desc=st.session_state[us_key],
                        )
                    st.success(f"✅ Card created: [{_new_card.name}]({_new_card.url})")

    # -----------------------------------------------------------------------
    # TAB 6 — Sign Off
    # -----------------------------------------------------------------------
    with tab_signoff:
        st.subheader("✅ QA Sign Off")

        _so_cards = st.session_state.get("rqa_cards", [])
        _approved_store = st.session_state.get("rqa_approved", {})
        _tc_store_so = st.session_state.get("rqa_test_cases", {})
        _release_so = st.session_state.get("rqa_release", "")

        if not _so_cards:
            st.info("Load a release in 🚀 Release QA first.")
        else:
            _approved_cards = [c for c in _so_cards if _approved_store.get(c.id)]
            _pending_cards = [c for c in _so_cards if not _approved_store.get(c.id)]

            col_so1, col_so2, col_so3 = st.columns(3)
            col_so1.metric("Total Cards", len(_so_cards))
            col_so2.metric("✅ Approved", len(_approved_cards))
            col_so3.metric("⏳ Pending", len(_pending_cards))

            if _pending_cards:
                st.warning(f"⏳ {len(_pending_cards)} card(s) still pending — approve them in 🚀 Release QA first.")

            st.divider()
            st.subheader("🐛 Bug Summary")
            for _card in _so_cards:
                _bugs = st.session_state.get(f"bugs_{_card.id}", [])
                _icon = "✅" if _approved_store.get(_card.id) else "⏳"
                with st.expander(f"{_icon} {_card.name} — {len(_bugs)} bug(s)", expanded=False):
                    if _bugs:
                        for _bug in _bugs:
                            _sev = _bug.get("severity", "P3")
                            _sev_icon = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "🟢"}.get(_sev, "⚪")
                            _bug_url = _bug.get("url", "")
                            _link = f"[Trello]({_bug_url})" if _bug_url else ""
                            st.markdown(f"{_sev_icon} **[{_sev}]** {_bug.get('name', '')} {_link}")
                    else:
                        st.caption("No bugs raised for this card.")

            if sheets_ok and _release_so:
                st.divider()
                st.subheader("📊 Create Release Sheet")
                st.caption(f"Creates a Google Sheets tab named **{_release_so}** with one row per card.")
                if st.button("📊 Create Release Sheet", key="create_release_sheet", type="primary"):
                    from pipeline.sheets_writer import create_release_sheet
                    _bugs_by_card = {c.id: st.session_state.get(f"bugs_{c.id}", []) for c in _so_cards}
                    with st.spinner("Creating release sheet…"):
                        _rs = create_release_sheet(
                            release_name=_release_so,
                            cards=_so_cards,
                            list_name=st.session_state.get("rqa_list_name", ""),
                            bugs_by_card=_bugs_by_card,
                        )
                    if _rs.get("sheet_url"):
                        st.success(f"✅ Release sheet created — [Open Sheet]({_rs['sheet_url']})")
                    else:
                        st.error("❌ Failed to create release sheet.")

            st.divider()
            st.subheader("🐛 Raise a Bug")
            st.caption("Describe the bug — Claude formats it and checks for duplicates in the Trello backlog.")
            _bug_desc = st.text_area("Bug description", placeholder="e.g. Tracking status shows 'In Transit' after delivery...", height=100, key="so_bug_desc")
            _bug_ctx = st.text_input("Feature context", placeholder="e.g. Tracking Page > Delivery Status", key="so_bug_ctx")
            if st.button("🔍 Check & Draft Bug", key="so_check_bug", disabled=not (_bug_desc or "").strip()):
                from pipeline.bug_tracker import check_and_draft_bug
                with st.spinner("Checking backlog for duplicates…"):
                    _bug_result = check_and_draft_bug(_bug_desc, _bug_ctx, release=_release_so)
                st.session_state["bug_check_result"] = _bug_result

            _br = st.session_state.get("bug_check_result")
            if _br:
                if _br.is_duplicate and _br.duplicate_card:
                    st.warning(f"⚠️ Possible duplicate: **{_br.duplicate_card.name}**")
                    st.caption(f"Reason: {_br.duplicate_reason}")
                    if _br.duplicate_card.url:
                        st.markdown(f"[Open existing card ↗]({_br.duplicate_card.url})")
                elif _br.draft:
                    st.markdown(_br.draft.to_display_markdown())
                    if st.button("✅ Raise Bug in Trello", key="so_raise_bug", type="primary"):
                        from pipeline.bug_tracker import raise_bug
                        with st.spinner("Creating bug card…"):
                            _raised = raise_bug(_br.draft)
                        st.success(f"✅ Bug raised: [{_raised.name}]({_raised.url})")
                        st.session_state.pop("bug_check_result", None)
                        st.rerun()
                elif _br.error:
                    st.error(f"❌ {_br.error}")

    # -----------------------------------------------------------------------
    # TAB 7 — Run Automation
    # -----------------------------------------------------------------------
    with tab_run:
        from pathlib import Path as _Path
        st.subheader("▶️ Run Automation")
        st.caption("Execute Playwright tests for the loaded release.")

        _codebase = config.AUTOMATION_CODEBASE_PATH
        if not _codebase or not _Path(_codebase).exists():
            st.error(f"❌ Automation codebase not found at: `{_codebase}`\nSet `AUTOMATION_CODEBASE_PATH` in `.env`")
        else:
            _release_run = st.session_state.get("rqa_release", "")
            st.markdown(f"**Release:** `{_release_run or '(none loaded)'}`")

            # Spec file selector
            _specs_dir = _Path(_codebase) / "tests"
            _spec_files = sorted(
                str(p.relative_to(_codebase)) for p in _specs_dir.rglob("*.spec.ts")
            ) if _specs_dir.exists() else []

            st.markdown("**Select spec files:**")
            _selected = []
            if _spec_files:
                _run_all = st.checkbox("Run all specs", value=False, key="run_all_specs")
                if not _run_all:
                    _cols = st.columns(2)
                    for _i, _spec in enumerate(_spec_files):
                        with _cols[_i % 2]:
                            if st.checkbox(_spec, key=f"spec_{_spec}"):
                                _selected.append(_spec)
                else:
                    _selected = _spec_files
            else:
                st.info("No `.spec.ts` files found — index the automation codebase first.")

            col_r1, col_r2 = st.columns(2)
            with col_r1:
                _headed = st.toggle("🖥️ Headed mode", value=False, key="run_headed")
            with col_r2:
                _grep = st.text_input("--grep pattern (optional)", placeholder="@smoke", key="run_grep")

            if st.button("▶️ Run Tests", key="run_tests_btn", type="primary",
                         disabled=not (_selected or _grep)):
                from pipeline.test_runner import run_tests, RunConfig
                _cfg = RunConfig(
                    release=_release_run,
                    spec_files=_selected,
                    grep=_grep,
                    headed=_headed,
                )
                with st.spinner("Running Playwright tests… (this may take several minutes)"):
                    _run_result = run_tests(_cfg)
                st.session_state["last_run_result"] = _run_result
                st.rerun()

            _last = st.session_state.get("last_run_result")
            if _last:
                st.divider()
                _r_icon = "✅" if _last.failed == 0 else "❌"
                st.markdown(f"### {_r_icon} {_last.status}")
                col_ra, col_rb, col_rc, col_rd = st.columns(4)
                col_ra.metric("Total", _last.total)
                col_rb.metric("✅ Passed", _last.passed)
                col_rc.metric("❌ Failed", _last.failed)
                col_rd.metric("⏭️ Skipped", _last.skipped)
                st.caption(f"Duration: {_last.duration_secs:.1f}s · Pass rate: {_last.pass_rate}")

                if _last.failed_tests:
                    with st.expander("❌ Failed Tests", expanded=True):
                        for _t in _last.failed_tests:
                            st.markdown(f"- `{_t}`")

                _slack_ready = bool(
                    os.getenv("SLACK_WEBHOOK_URL") or
                    (os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_CHANNEL"))
                )
                if _slack_ready:
                    if st.button("📢 Post Results to Slack", key="post_slack_run"):
                        from pipeline.slack_client import SlackClient
                        try:
                            _sc = SlackClient()
                            _sc.post_test_results(_last)
                            st.success("✅ Posted to Slack!")
                        except Exception as _se:
                            st.error(f"❌ {_se}")

    # -----------------------------------------------------------------------
    # TAB 8 — Config
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
        st.markdown("**Trello**")
        col_t1, col_t2 = st.columns(2)
        with col_t1:
            st.text_input("Trello API Key", value="***" if config.TRELLO_API_KEY else "(not set)", disabled=True)
            st.text_input("Trello Board ID", value=config.TRELLO_BOARD_ID or "(not set)", disabled=True)
        with col_t2:
            st.text_input("Trello Token", value="***" if config.TRELLO_TOKEN else "(not set)", disabled=True)

        st.divider()
        st.markdown("**Seed URLs**")
        for url in config.PLUGINHIVE_SEED_URLS:
            st.caption(url)


if __name__ == "__main__":
    main()
