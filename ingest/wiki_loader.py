"""
Wiki Loader
===========
Reads all Markdown files from the internal Tracking App wiki and
returns chunked LangChain Documents ready for indexing into ChromaDB.

Wiki path is configured via WIKI_PATH in config.py.
"""
from __future__ import annotations
import logging
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

import config

logger = logging.getLogger(__name__)

# Folder-name → human-readable category label
_CATEGORY_MAP: dict[str, str] = {
    "00_dashboard":      "Dashboard & Metrics",
    "01_product":        "Product & Features",
    "02_engineering":    "Engineering & APIs",
    "03_customers":      "Customer Issues & Support",
    "04_operations":     "Operations",
    "05_knowledge_base": "Knowledge Base & Guides",
    "99_archive":        "Archive",
    "_templates":        "Templates",
}

_TEXT_EXTENSIONS = {".md", ".mdx", ".txt"}


def _category_from_path(file_path: Path, wiki_root: Path) -> str:
    """Derive a human-readable category from the file's parent folder name."""
    try:
        relative = file_path.relative_to(wiki_root)
        top_folder = relative.parts[0].lower() if len(relative.parts) > 1 else ""
        return _CATEGORY_MAP.get(top_folder, top_folder or "wiki")
    except Exception:
        return "wiki"


def load_wiki_docs() -> list[Document]:
    """
    Walk the wiki directory, read every Markdown/text file, chunk the content,
    and return LangChain Documents tagged with source_type="wiki".

    Returns an empty list (with a warning) if the wiki directory doesn't exist.
    """
    wiki_root = Path(config.WIKI_PATH)
    if not wiki_root.exists():
        logger.warning(
            "Wiki path does not exist: %s — skipping wiki ingestion. "
            "Set WIKI_PATH env var to the tracking app wiki directory.",
            wiki_root,
        )
        return []

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
    )

    documents: list[Document] = []
    files_read = 0
    files_skipped = 0

    for file_path in sorted(wiki_root.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in _TEXT_EXTENSIONS:
            files_skipped += 1
            continue

        try:
            text = file_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as e:
            logger.warning("Could not read wiki file %s: %s", file_path, e)
            continue

        if not text or len(text) < 50:
            continue

        category = _category_from_path(file_path, wiki_root)
        source_id = f"wiki:{file_path.relative_to(wiki_root)}"

        chunks = splitter.split_text(text)
        for i, chunk in enumerate(chunks):
            documents.append(Document(
                page_content=chunk,
                metadata={
                    "source":      source_id,
                    "source_url":  source_id,
                    "source_type": "wiki",
                    "category":    category,
                    "file_name":   file_path.name,
                    "file_path":   str(file_path),
                    "chunk_index": i,
                },
            ))

        files_read += 1
        logger.debug("Wiki: %s → %d chunks (category: %s)", file_path.name, len(chunks), category)

    logger.info(
        "Wiki loader: %d files read, %d files skipped → %d total chunks",
        files_read, files_skipped, len(documents),
    )
    return documents
