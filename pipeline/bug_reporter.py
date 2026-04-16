"""
Bug Reporter  —  QA team helpers and bug notification pipeline.

Provides:
  - _is_qa()       — detect QA team members by name
  - get_card_devs() — list non-QA assignees on a Trello card
"""
from __future__ import annotations

QA_NAMES = ["QA", "Anuja", "Ashok", "Tester"]  # extend as needed


def _is_qa(full_name: str) -> bool:
    """Return True if the given full name belongs to a QA team member."""
    name_lower = full_name.lower()
    return any(n.lower() in name_lower for n in QA_NAMES)


def get_card_devs(card, trello) -> list[str]:
    """Return list of developer names assigned to a card (non-QA members)."""
    try:
        members = trello.get_card_members(card.id)
        return [m["fullName"] for m in members if not _is_qa(m.get("fullName", ""))]
    except Exception:
        return []
