"""
Code Indexer  —  Source Code RAG for TC Generation
====================================================
Indexes backend and frontend source code into a dedicated ChromaDB collection
so `generate_test_cases()` can retrieve real implementation context:
  - Which API endpoints / services are involved
  - What validations and error conditions exist in the code
  - Field names, constants, and business logic

Collections:
  fedex_code_knowledge  (separate from the QA knowledge collection)

Each chunk stores metadata:
  source_type : "backend" | "frontend"
  file_path   : relative path from the indexed root
  language    : "typescript" | "php" | "python" | etc.

Usage:
  from rag.code_indexer import index_codebase, search_code, get_index_stats

  # Index backend code (run once, or when code changes)
  result = index_codebase("/path/to/backend", source_type="backend")

  # At TC generation time
  snippets = search_code("dry ice dangerous goods shipment", k=5)
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

import config
from rag.vectorstore import get_embeddings

logger = logging.getLogger(__name__)

# Persists last-sync state (commit hash + timestamp) per source_type
_SYNC_STATE_FILE = Path(config.CHROMA_PATH).parent / "code_sync_state.json"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LANGUAGE_MAP: dict[str, str] = {
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".php":  "php",
    ".java": "java",
    ".py":   "python",
    ".go":   "go",
    ".rb":   "ruby",
    ".cs":   "csharp",
}

# Directories to always skip (node_modules, build output, etc.)
_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", "__pycache__",
    "vendor", "coverage", ".cache", "out", ".venv", "venv",
}

# Max file size to index (skip huge generated/minified files)
_MAX_FILE_BYTES = 100_000   # 100 KB

_CODE_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=800,
    chunk_overlap=100,
    separators=["\n\n\n", "\n\n", "\n", " ", ""],
)

# ---------------------------------------------------------------------------
# Vectorstore (separate collection from QA knowledge)
# ---------------------------------------------------------------------------

_code_vs_instance: Chroma | None = None


def _get_code_vectorstore() -> Chroma:
    global _code_vs_instance
    if _code_vs_instance is None:
        _code_vs_instance = Chroma(
            collection_name=config.CHROMA_CODE_COLLECTION,
            embedding_function=get_embeddings(),
            persist_directory=config.CHROMA_PATH,
            collection_metadata={
                "hnsw:space": "cosine",
                "hnsw:construction_ef": 100,
                "hnsw:search_ef": 100,
                "hnsw:M": 16,
            },
        )
    return _code_vs_instance


def _reset_code_vectorstore() -> None:
    global _code_vs_instance
    _code_vs_instance = None


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _walk_code_files(root: Path, extensions: list[str]) -> list[Path]:
    """Recursively collect source files, skipping known non-source dirs."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if any(fname.endswith(ext) for ext in extensions):
                files.append(Path(dirpath) / fname)
    return files


def index_codebase(
    code_path: str,
    source_type: str = "backend",   # "backend" | "frontend"
    extensions: list[str] | None = None,
    clear_existing: bool = False,
) -> dict:
    """
    Walk a source code directory and embed all matching files into the
    code knowledge ChromaDB collection.

    Args:
        code_path:      Absolute path to the codebase root
        source_type:    "backend" or "frontend" — stored in metadata
        extensions:     File extensions to index (defaults to config.CODE_FILE_EXTENSIONS)
        clear_existing: If True, removes all chunks for this source_type before indexing

    Returns:
        {"files_indexed": N, "chunks_added": M, "skipped": K, "error": ""}
    """
    root = Path(code_path)
    if not root.exists():
        return {"files_indexed": 0, "chunks_added": 0, "skipped": 0,
                "error": f"Path does not exist: {code_path}"}

    exts = extensions or config.CODE_FILE_EXTENSIONS
    files = _walk_code_files(root, exts)
    if not files:
        return {"files_indexed": 0, "chunks_added": 0, "skipped": 0,
                "error": f"No source files found in {code_path} with extensions {exts}"}

    logger.info("Found %d source files in '%s' (%s)", len(files), code_path, source_type)

    vs = _get_code_vectorstore()

    # Optionally clear previous index for this source_type
    if clear_existing:
        try:
            client = chromadb.PersistentClient(path=config.CHROMA_PATH)
            col = client.get_collection(config.CHROMA_CODE_COLLECTION)
            # ChromaDB where filter
            col.delete(where={"source_type": source_type})
            _reset_code_vectorstore()
            vs = _get_code_vectorstore()
            logger.info("Cleared existing '%s' code chunks", source_type)
        except Exception as e:
            logger.warning("Clear existing failed (ok on first run): %s", e)

    all_docs:  list[Document] = []
    all_ids:   list[str]      = []
    skipped = 0

    for fpath in files:
        try:
            size = fpath.stat().st_size
            if size > _MAX_FILE_BYTES:
                logger.debug("Skipping large file (%d bytes): %s", size, fpath)
                skipped += 1
                continue
            if size == 0:
                skipped += 1
                continue

            code = fpath.read_text(encoding="utf-8", errors="ignore")
            rel_path = str(fpath.relative_to(root))
            language = _LANGUAGE_MAP.get(fpath.suffix, fpath.suffix.lstrip("."))

            doc = Document(
                page_content=f"// File: {rel_path}\n\n{code}",
                metadata={
                    "source_type": source_type,
                    "file_path":   rel_path,
                    "language":    language,
                    "source":      "codebase",
                },
            )
            chunks = _CODE_SPLITTER.split_documents([doc])

            for i, chunk in enumerate(chunks):
                # Stable ID: source_type + relative path + chunk index
                safe_path = rel_path.replace("/", "_").replace("\\", "_").replace(".", "_")
                chunk_id = f"{source_type}__{safe_path}__c{i}"
                all_docs.append(chunk)
                all_ids.append(chunk_id)

        except Exception as e:
            logger.warning("Failed to read %s: %s", fpath, e)
            skipped += 1

    if not all_docs:
        return {"files_indexed": 0, "chunks_added": 0, "skipped": skipped,
                "error": "No readable content found in any source file"}

    # Upsert in batches
    _BATCH = 200
    for start in range(0, len(all_docs), _BATCH):
        batch_docs = all_docs[start: start + _BATCH]
        batch_ids  = all_ids[start: start + _BATCH]
        try:
            vs.delete(ids=batch_ids)
        except Exception:
            pass
        vs.add_documents(batch_docs, ids=batch_ids)
        logger.info(
            "Code index batch %d–%d / %d chunks (%s)",
            start + 1, min(start + _BATCH, len(all_docs)), len(all_docs), source_type,
        )

    files_indexed = len(files) - skipped
    logger.info(
        "Code index complete: %d files, %d chunks (%s)",
        files_indexed, len(all_docs), source_type,
    )
    return {
        "files_indexed": files_indexed,
        "chunks_added":  len(all_docs),
        "skipped":       skipped,
        "error":         "",
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_code(
    query: str,
    k: int = 5,
    source_type: str | None = None,   # None = search both backend + frontend
) -> list[Document]:
    """
    Retrieve the most relevant source code chunks for a given query.

    Args:
        query:       Feature description or card name to search against
        k:           Number of chunks to return
        source_type: "backend", "frontend", or None for both

    Returns:
        List of LangChain Document objects (empty if nothing indexed yet)
    """
    try:
        vs = _get_code_vectorstore()
        if source_type:
            results = vs.similarity_search(
                query, k=k,
                filter={"source_type": source_type},
            )
        else:
            results = vs.similarity_search(query, k=k)
        return results
    except Exception as e:
        err = str(e).lower()
        if "does not exist" in err or "collection" in err or "no documents" in err:
            return []   # collection not yet created — silently return empty
        logger.warning("Code search failed for query %r: %s", query, e)
        return []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_index_stats() -> dict:
    """
    Return counts of indexed chunks per source_type plus last-sync state.
    Returns {"backend": N, "frontend": M, "total": N+M,
             "backend_sync": {...}, "frontend_sync": {...}, "error": ""}
    """
    try:
        client = chromadb.PersistentClient(path=config.CHROMA_PATH)
        try:
            col = client.get_collection(config.CHROMA_CODE_COLLECTION)
        except Exception:
            return {"backend": 0, "frontend": 0, "total": 0,
                    "backend_sync": {}, "frontend_sync": {}, "error": ""}

        total = col.count()

        def _count(stype):
            try:
                return len(col.get(where={"source_type": stype}, include=[])["ids"])
            except Exception:
                return 0

        automation_cnt = _count("automation")
        backend_cnt    = _count("backend")
        frontend_cnt   = _count("frontend")

        sync_state = _load_sync_state()
        return {
            "automation":      automation_cnt,
            "backend":         backend_cnt,
            "frontend":        frontend_cnt,
            "total":           total,
            "automation_sync": sync_state.get("automation", {}),
            "backend_sync":    sync_state.get("backend", {}),
            "frontend_sync":   sync_state.get("frontend", {}),
            "error":           "",
        }
    except Exception as e:
        logger.warning("get_index_stats failed: %s", e)
        return {"backend": 0, "frontend": 0, "total": 0,
                "backend_sync": {}, "frontend_sync": {}, "error": str(e)}


# ---------------------------------------------------------------------------
# Sync state helpers
# ---------------------------------------------------------------------------

def _load_sync_state() -> dict:
    """Load persisted sync state (commit hash, timestamp) per source_type."""
    try:
        if _SYNC_STATE_FILE.exists():
            return json.loads(_SYNC_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_sync_state(source_type: str, commit: str, files_updated: int) -> None:
    state = _load_sync_state()
    state[source_type] = {
        "commit":        commit,
        "synced_at":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "files_updated": files_updated,
    }
    _SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SYNC_STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str) -> str:
    """Run a git command and return stdout. Raises on non-zero exit."""
    import os as _os
    # Prevent git from prompting for SSH passphrase or credentials — any
    # network operation that needs auth will fail silently instead of blocking
    # the Streamlit render thread waiting for terminal input.
    _env = {
        **_os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=no",
    }
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env=_env,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def _get_current_commit(cwd: str) -> str:
    try:
        return _git(["rev-parse", "--short", "HEAD"], cwd)
    except Exception:
        return "unknown"


def get_repo_info(code_path: str) -> dict:
    """
    Return current branch, all local+remote branches, and current commit for a repo.
    Safe to call — returns empty dict with error string on failure.

    Does a silent `git fetch origin` first so that remote branches (e.g. `main`
    that was never checked out locally) always appear in the branch list.
    """
    if not Path(code_path).exists():
        return {"current_branch": "", "branches": [], "commit": "", "error": "Path not found"}
    try:
        # Use only local git data — no network call so the sidebar renders instantly.
        # Remote-tracking refs cached from the last pull/fetch still appear via -a.
        current = _git(["rev-parse", "--abbrev-ref", "HEAD"], code_path)
        branches_raw = _git(["branch", "-a", "--format=%(refname:short)"], code_path)
        branches = [
            b.strip().replace("origin/", "")
            for b in branches_raw.splitlines()
            if b.strip() and "HEAD" not in b
        ]
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_branches = [b for b in branches if not (b in seen or seen.add(b))]  # type: ignore[func-returns-value]
        commit = _get_current_commit(code_path)
        return {
            "current_branch": current,
            "branches":       unique_branches,
            "commit":         commit,
            "error":          "",
        }
    except Exception as e:
        return {"current_branch": "", "branches": [], "commit": "", "error": str(e)}


def _get_changed_files_since(cwd: str, since_commit: str) -> tuple[list[str], list[str]]:
    """
    Return (modified_files, deleted_files) relative paths since `since_commit`.
    Uses git diff --name-status to classify changes.
    """
    try:
        output = _git(
            ["diff", "--name-status", f"{since_commit}..HEAD"],
            cwd,
        )
    except Exception:
        # If since_commit is unknown/stale, return all tracked files as modified
        output = _git(["ls-files"], cwd)
        return output.splitlines(), []

    modified: list[str] = []
    deleted:  list[str] = []

    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, fpath = parts[0].strip(), parts[1].strip()
        if status.startswith("D"):
            deleted.append(fpath)
        else:
            # A = Added, M = Modified, R = Renamed (R100\told\tnew format handled below)
            # For renames git gives "R<score>\told_path\tnew_path"
            if "\t" in fpath:
                # rename: take the new path (second field)
                fpath = fpath.split("\t")[-1]
            modified.append(fpath)

    return modified, deleted


def _remove_file_chunks(source_type: str, rel_path: str) -> int:
    """Delete all RAG chunks for a specific file. Returns count removed."""
    try:
        client = chromadb.PersistentClient(path=config.CHROMA_PATH)
        col = client.get_collection(config.CHROMA_CODE_COLLECTION)
        existing = col.get(
            where={"$and": [
                {"source_type": {"$eq": source_type}},
                {"file_path":   {"$eq": rel_path}},
            ]},
            include=[],
        )
        ids = existing.get("ids", [])
        if ids:
            col.delete(ids=ids)
            logger.debug("Removed %d chunks for %s/%s", len(ids), source_type, rel_path)
        return len(ids)
    except Exception as e:
        logger.debug("_remove_file_chunks failed for %s: %s", rel_path, e)
        return 0


def _index_single_file(
    fpath: Path,
    root: Path,
    source_type: str,
    vs: Chroma,
) -> int:
    """Index one file into the vectorstore. Returns number of chunks added."""
    try:
        if fpath.stat().st_size > _MAX_FILE_BYTES or fpath.stat().st_size == 0:
            return 0
        code     = fpath.read_text(encoding="utf-8", errors="ignore")
        rel_path = str(fpath.relative_to(root))
        language = _LANGUAGE_MAP.get(fpath.suffix, fpath.suffix.lstrip("."))

        # Remove old chunks for this file first
        _remove_file_chunks(source_type, rel_path)

        doc = Document(
            page_content=f"// File: {rel_path}\n\n{code}",
            metadata={
                "source_type": source_type,
                "file_path":   rel_path,
                "language":    language,
                "source":      "codebase",
            },
        )
        chunks = _CODE_SPLITTER.split_documents([doc])
        if not chunks:
            return 0

        safe_path = rel_path.replace("/", "_").replace("\\", "_").replace(".", "_")
        ids = [f"{source_type}__{safe_path}__c{i}" for i in range(len(chunks))]

        try:
            vs.delete(ids=ids)
        except Exception:
            pass
        vs.add_documents(chunks, ids=ids)
        return len(chunks)
    except Exception as e:
        logger.warning("Failed to index %s: %s", fpath, e)
        return 0


# ---------------------------------------------------------------------------
# Git-diff incremental sync  (the main entry point for day-to-day use)
# ---------------------------------------------------------------------------

def sync_from_git(
    code_path: str,
    source_type: str = "backend",
    extensions: list[str] | None = None,
    branch: str | None = None,
) -> dict:
    """
    Pull latest code then re-index ONLY the changed files.

    Flow:
      1. (Optional) git checkout <branch>
      2. Record commit hash BEFORE pull
      3. git pull
      4. git diff <before>..HEAD --name-status  → changed / deleted files
      5. For deleted files  → remove their chunks from ChromaDB
      6. For modified/added → re-index those files only
      7. Save new commit hash to sync state

    Args:
        code_path:   Path to the git repo root
        source_type: "automation" | "backend" | "frontend"
        extensions:  File extensions to consider (defaults to config.CODE_FILE_EXTENSIONS)
        branch:      Branch to checkout before pulling (None = stay on current branch)

    Returns:
        {
          "pulled": bool,
          "commit_before": str,
          "commit_after":  str,
          "files_changed": int,
          "files_deleted": int,
          "chunks_updated": int,
          "diff_summary": [str],   # list of changed file paths
          "error": str,
        }
    """
    root = Path(code_path)
    if not root.exists():
        return {"pulled": False, "error": f"Path not found: {code_path}",
                "commit_before": "", "commit_after": "", "files_changed": 0,
                "files_deleted": 0, "chunks_updated": 0, "diff_summary": []}

    exts = extensions or config.CODE_FILE_EXTENSIONS

    try:
        commit_before = _get_current_commit(code_path)
    except Exception as e:
        return {"pulled": False, "error": f"Not a git repo: {e}",
                "commit_before": "", "commit_after": "", "files_changed": 0,
                "files_deleted": 0, "chunks_updated": 0, "diff_summary": []}

    # ── (Optional) checkout requested branch ─────────────────────────────────
    if branch:
        try:
            _git(["checkout", branch], code_path)
            logger.info("Checked out branch '%s' for %s", branch, source_type)
        except Exception as e:
            return {"pulled": False, "error": f"git checkout {branch} failed: {e}",
                    "commit_before": commit_before, "commit_after": commit_before,
                    "files_changed": 0, "files_deleted": 0,
                    "chunks_updated": 0, "diff_summary": []}

    # ── git fetch + pull ─────────────────────────────────────────────────────
    try:
        # Fetch first so remote-tracking refs are up to date before pull
        try:
            _git(["fetch", "origin"], code_path)
        except Exception:
            pass  # fetch failure is non-fatal; pull will still try
        # Always specify the branch explicitly — without it, git pull origin
        # fails when the local branch has no upstream tracking configured.
        current_branch = branch or _git(["rev-parse", "--abbrev-ref", "HEAD"], code_path).strip()
        pull_out = _git(["pull", "origin", current_branch], code_path)
        logger.info("git pull origin %s (%s): %s", current_branch, source_type, pull_out[:120])
        pulled = True
    except Exception as e:
        return {"pulled": False, "error": f"git pull failed: {e}",
                "commit_before": commit_before, "commit_after": commit_before,
                "files_changed": 0, "files_deleted": 0,
                "chunks_updated": 0, "diff_summary": []}

    commit_after = _get_current_commit(code_path)

    # ── Nothing changed? ─────────────────────────────────────────────────────
    if commit_before == commit_after:
        return {
            "pulled":         True,
            "commit_before":  commit_before,
            "commit_after":   commit_after,
            "files_changed":  0,
            "files_deleted":  0,
            "chunks_updated": 0,
            "diff_summary":   [],
            "error":          "",
            "message":        "Already up to date — nothing to re-index",
        }

    # ── Get diff ─────────────────────────────────────────────────────────────
    modified_paths, deleted_paths = _get_changed_files_since(code_path, commit_before)

    # Filter to relevant file extensions only
    modified_paths = [p for p in modified_paths
                      if any(p.endswith(ext) for ext in exts)]
    deleted_paths  = [p for p in deleted_paths
                      if any(p.endswith(ext) for ext in exts)]

    vs = _get_code_vectorstore()
    total_chunks = 0
    diff_summary: list[str] = []

    # ── Remove deleted files ──────────────────────────────────────────────────
    for rel_path in deleted_paths:
        removed = _remove_file_chunks(source_type, rel_path)
        diff_summary.append(f"🗑️ {rel_path} ({removed} chunks removed)")
        logger.info("Removed deleted file from RAG: %s", rel_path)

    # ── Re-index modified/added files ─────────────────────────────────────────
    for rel_path in modified_paths:
        fpath = root / rel_path
        if not fpath.exists():
            continue
        n = _index_single_file(fpath, root, source_type, vs)
        total_chunks += n
        diff_summary.append(f"{'✅' if n else '⬜'} {rel_path} ({n} chunks)")
        logger.info("Re-indexed %s → %d chunks", rel_path, n)

    # ── Save sync state ───────────────────────────────────────────────────────
    _save_sync_state(source_type, commit_after, len(modified_paths) + len(deleted_paths))

    logger.info(
        "Git sync complete (%s): %s → %s | %d changed, %d deleted, %d chunks updated",
        source_type, commit_before, commit_after,
        len(modified_paths), len(deleted_paths), total_chunks,
    )
    return {
        "pulled":         pulled,
        "commit_before":  commit_before,
        "commit_after":   commit_after,
        "files_changed":  len(modified_paths),
        "files_deleted":  len(deleted_paths),
        "chunks_updated": total_chunks,
        "diff_summary":   diff_summary,
        "error":          "",
    }
