"""
QA Retrospective  —  Feedback Loop & RAG Self-Improvement
==========================================================
After completing the full pipeline for a card (AC → TC → Automation → Run),
QA fills in a structured retrospective:

  • AC gaps         — what was missed in the acceptance criteria
  • TC issues       — wrong or missing test cases
  • Automation issues — bugs in the generated Playwright code
  • What went well  — positive notes for reinforcement

Feedback is:
  1. Saved to disk  → survives restarts, editable later
  2. Indexed into ChromaDB (source_type="qa_feedback") → future AC/TC
     generation automatically retrieves this wisdom and improves output.

How the improvement loop works
-------------------------------
  Card A  →  QA Retro: "missed carrier-specific notification edge case"
                ↓ stored in ChromaDB
  Card B  →  AC generator retrieves similar retro
                ↓ Claude sees: "past QA noted: always include carrier-specific scenarios"
                → AC for Card B now includes carrier edge cases automatically
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

_FEEDBACK_DIR = Path(config.CHROMA_PATH).parent / "qa_feedback"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ensure_dir() -> Path:
    _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return _FEEDBACK_DIR


def _feedback_path(card_id: str) -> Path:
    return _ensure_dir() / f"{card_id}.json"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class QAFeedback:
    card_id: str
    card_name: str
    date: str                                                    # ISO date string

    ac_misses: list[str] = field(default_factory=list)          # scenarios missed in AC
    tc_issues: list[str] = field(default_factory=list)          # wrong / missing TCs
    automation_issues: list[str] = field(default_factory=list)  # bad selectors, missing waits…
    what_went_well: list[str] = field(default_factory=list)     # positive notes
    overall_notes: str = ""


# ── Disk persistence ──────────────────────────────────────────────────────────

def load_feedback(card_id: str) -> Optional[QAFeedback]:
    """Load saved retrospective for a card. Returns None if none exists."""
    p = _feedback_path(card_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return QAFeedback(**data)
    except Exception as e:
        logger.warning("Failed to load QA feedback for %s: %s", card_id, e)
        return None


def _save_to_disk(feedback: QAFeedback) -> None:
    try:
        _feedback_path(feedback.card_id).write_text(
            json.dumps(feedback.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to save QA feedback for %s: %s", feedback.card_id, e)


# ── ChromaDB document builder ─────────────────────────────────────────────────

def _format_feedback_doc(fb: QAFeedback) -> str:
    """
    Render feedback as a rich text document for embedding.
    Written to sound like QA wisdom so future retrieval gives
    Claude actionable lessons.
    """
    parts = [
        f"QA Retrospective — Feature: {fb.card_name}",
        f"Reviewed on: {fb.date}",
        "",
    ]

    if fb.ac_misses:
        parts.append("ACCEPTANCE CRITERIA GAPS (things the AC missed):")
        for m in fb.ac_misses:
            parts.append(f"  - {m}")
        parts.append("")

    if fb.tc_issues:
        parts.append("TEST CASE ISSUES (wrong or missing test cases):")
        for t in fb.tc_issues:
            parts.append(f"  - {t}")
        parts.append("")

    if fb.automation_issues:
        parts.append("AUTOMATION CODE ISSUES (Playwright bugs found by QA):")
        for a in fb.automation_issues:
            parts.append(f"  - {a}")
        parts.append("")

    if fb.what_went_well:
        parts.append("WHAT WENT WELL:")
        for w in fb.what_went_well:
            parts.append(f"  - {w}")
        parts.append("")

    if fb.overall_notes:
        parts.append(f"OVERALL NOTES: {fb.overall_notes}")

    return "\n".join(parts).strip()


# ── ChromaDB indexer ──────────────────────────────────────────────────────────

def _index_feedback(feedback: QAFeedback) -> int:
    """
    Embed the feedback document into ChromaDB.
    Uses stable IDs so re-saving a card's retro updates it in-place.
    Returns number of chunks added.
    """
    try:
        from langchain_core.documents import Document
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        from rag.vectorstore import upsert_documents

        text = _format_feedback_doc(feedback)
        if not text or len(text) < 30:
            return 0

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.CHUNK_SIZE,
            chunk_overlap=config.CHUNK_OVERLAP,
        )
        chunks = splitter.split_text(text)

        docs = [
            Document(
                page_content=chunk,
                metadata={
                    "source_type": "qa_feedback",
                    "doc_type":    "qa_feedback",
                    "card_id":     feedback.card_id,
                    "card_name":   feedback.card_name,
                    "date":        feedback.date,
                    "chunk_index": i,
                },
            )
            for i, chunk in enumerate(chunks)
        ]

        # Stable IDs — re-saving this card's retro replaces old chunks cleanly
        ids = [f"qa_feedback_{feedback.card_id}_{i}" for i in range(len(docs))]
        upsert_documents(docs, ids)
        logger.info(
            "Indexed QA feedback for '%s' — %d chunk(s)", feedback.card_name, len(docs)
        )
        return len(docs)

    except Exception as e:
        logger.warning("Failed to index QA feedback for %s: %s", feedback.card_id, e)
        return 0


# ── Public API ────────────────────────────────────────────────────────────────

def save_feedback(feedback: QAFeedback) -> dict:
    """
    Persist QA retrospective to disk AND index it into ChromaDB.

    Returns:
        {"ok": bool, "chunks_added": int, "error": str}
    """
    try:
        _save_to_disk(feedback)
        chunks = _index_feedback(feedback)
        return {"ok": True, "chunks_added": chunks, "error": ""}
    except Exception as e:
        logger.error("save_feedback failed for %s: %s", feedback.card_id, e)
        return {"ok": False, "chunks_added": 0, "error": str(e)}


def search_feedback(query: str, k: int = 4) -> list:
    """
    Retrieve past QA feedback relevant to a query.
    Used by AC/TC generators to pull lessons from previous cards.

    Returns list[Document] with source_type="qa_feedback".
    """
    try:
        from rag.vectorstore import get_vectorstore
        vs = get_vectorstore()
        results = vs.similarity_search(
            query, k=k,
            filter={"source_type": "qa_feedback"},
        )
        return results
    except Exception as e:
        logger.debug("Feedback search failed (non-fatal): %s", e)
        return []


def get_feedback_count() -> int:
    """Count total feedback entries saved to disk."""
    if not _FEEDBACK_DIR.exists():
        return 0
    return len(list(_FEEDBACK_DIR.glob("*.json")))


def build_feedback_context(query: str) -> str:
    """
    Fetch relevant past QA feedback and format it as a prompt section.
    Intended to be injected into AC_WRITER_PROMPT and TEST_CASE_PROMPT.

    Returns an empty string if no relevant feedback found.
    """
    docs = search_feedback(query, k=4)
    if not docs:
        return ""

    snippets = []
    for d in docs:
        card = d.metadata.get("card_name", "a previous card")
        snippets.append(f"[From: {card}]\n{d.page_content[:500]}")

    context = "\n\n---\n".join(snippets)
    return (
        "\n\nLessons from past QA retrospectives (apply these learnings to avoid "
        "repeating known mistakes):\n"
        + context
        + "\n"
    )
