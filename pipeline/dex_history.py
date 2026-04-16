from __future__ import annotations
"""Persist Domain Expert Q&A history per card to disk so it survives Streamlit restarts."""
import json
import logging
from pathlib import Path
import config

logger = logging.getLogger(__name__)
_HISTORY_DIR = Path(config.CHROMA_PATH).parent / "dex_history"


def _path(card_id: str) -> Path:
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return _HISTORY_DIR / f"{card_id}.json"


def load_history(card_id: str) -> list[dict]:
    """Load conversation history for a card. Returns [] if none."""
    p = _path(card_id)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load DEX history for %s: %s", card_id, e)
        return []


def save_history(card_id: str, history: list[dict]) -> None:
    """Save conversation history for a card to disk."""
    try:
        _path(card_id).write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save DEX history for %s: %s", card_id, e)


def clear_history(card_id: str) -> None:
    """Delete saved history for a card."""
    try:
        p = _path(card_id)
        if p.exists():
            p.unlink()
    except Exception as e:
        logger.warning("Failed to clear DEX history for %s: %s", card_id, e)
