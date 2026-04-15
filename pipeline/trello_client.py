"""
Trello Client
Thin wrapper around the Trello REST API for reading cards and writing
acceptance criteria back to them.

Required .env vars:
    TRELLO_API_KEY   — from https://trello.com/power-ups/admin
    TRELLO_TOKEN     — OAuth token for your account
    TRELLO_BOARD_ID  — the board that holds the delivery pipeline lists
"""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

TRELLO_BASE = "https://api.trello.com/1"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class TrelloCard:
    id: str
    name: str                       # Raw feature title / one-liner
    desc: str                       # Current card description
    list_id: str
    list_name: str
    labels: list[str] = field(default_factory=list)
    url: str = ""
    attachments: list[dict] = field(default_factory=list)   # [{name, url}]
    checklists: list[dict] = field(default_factory=list)    # [{name, items:[{name,state}]}]
    comments: list[str] = field(default_factory=list)       # plain text comments


@dataclass
class TrelloList:
    id: str
    name: str


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TrelloClient:
    def __init__(
        self,
        api_key: str | None = None,
        token: str | None = None,
        board_id: str | None = None,
    ):
        self.api_key = api_key or os.getenv("TRELLO_API_KEY", "")
        self.token = token or os.getenv("TRELLO_TOKEN", "")
        self.board_id = board_id or os.getenv("TRELLO_BOARD_ID", "")

        if not all([self.api_key, self.token, self.board_id]):
            raise ValueError(
                "Trello credentials missing.\n"
                "Set TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID in .env"
            )

    # -- helpers -----------------------------------------------------------

    @property
    def _auth(self) -> dict[str, str]:
        return {"key": self.api_key, "token": self.token}

    def _get(self, path: str, **params) -> Any:
        resp = requests.get(
            f"{TRELLO_BASE}/{path}",
            params={**self._auth, **params},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, **data) -> Any:
        resp = requests.put(
            f"{TRELLO_BASE}/{path}",
            params=self._auth,
            json=data,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, **data) -> Any:
        resp = requests.post(
            f"{TRELLO_BASE}/{path}",
            params=self._auth,
            json=data,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # -- board/list queries ------------------------------------------------

    def get_lists(self) -> list[TrelloList]:
        """Return all lists on the board."""
        raw = self._get(f"boards/{self.board_id}/lists", filter="open")
        return [TrelloList(id=l["id"], name=l["name"]) for l in raw]

    def get_list_by_name(self, name: str) -> TrelloList | None:
        """Find a list by exact name (case-insensitive)."""
        for lst in self.get_lists():
            if lst.name.strip().lower() == name.strip().lower():
                return lst
        return None

    def create_list(self, name: str, pos: str = "bottom") -> TrelloList:
        """Create a new list on the board and return it."""
        raw = self._post(f"boards/{self.board_id}/lists", name=name, pos=pos)
        return TrelloList(id=raw["id"], name=raw["name"])

    def get_board_members(self) -> list[dict]:
        """Return all members of the board as list of {id, fullName, username}."""
        try:
            raw = self._get(f"boards/{self.board_id}/members")
            return [{"id": m["id"], "fullName": m.get("fullName", m.get("username", "")),
                     "username": m.get("username", "")} for m in raw]
        except Exception as e:
            logger.warning("get_board_members failed: %s", e)
            return []

    def create_card_in_list(
        self,
        list_id: str,
        name: str,
        desc: str = "",
        member_ids: list[str] | None = None,
        list_name: str = "",
    ) -> TrelloCard:
        """Create a card directly by list ID (use after create_list).

        Args:
            list_id:    Trello list ID
            name:       Card title
            desc:       Card description (markdown)
            member_ids: List of Trello member IDs to assign to the card
            list_name:  Human-readable list name (optional, for TrelloCard metadata)
        """
        payload: dict = dict(idList=list_id, name=name, desc=desc, pos="bottom")
        if member_ids:
            payload["idMembers"] = ",".join(member_ids)
        raw = self._post("cards", **payload)
        return TrelloCard(
            id=raw["id"],
            name=raw["name"],
            desc=raw.get("desc", ""),
            list_id=list_id,
            list_name=list_name,
            url=raw.get("url", raw.get("shortUrl", "")),
            labels=[],
            attachments=[],
            checklists=[],
            comments=[],
        )

    # -- card queries ------------------------------------------------------

    def _parse_extra(self, card_id: str) -> tuple[list[dict], list[dict], list[str]]:
        """Fetch attachments, checklists, and comments for a card."""
        try:
            raw_att = self._get(f"cards/{card_id}/attachments")
            attachments = [{"name": a.get("name", ""), "url": a.get("url", "")}
                           for a in raw_att if a.get("url")]
        except Exception:
            attachments = []
        try:
            raw_cl = self._get(f"cards/{card_id}/checklists")
            checklists = [
                {"name": cl["name"],
                 "items": [{"name": i["name"], "state": i["state"]} for i in cl.get("checkItems", [])]}
                for cl in raw_cl
            ]
        except Exception:
            checklists = []
        try:
            raw_actions = self._get(f"cards/{card_id}/actions", filter="commentCard")
            comments = [a["data"]["text"] for a in raw_actions if a.get("data", {}).get("text")]
        except Exception:
            comments = []
        return attachments, checklists, comments

    def get_cards_in_list(self, list_id: str) -> list[TrelloCard]:
        """Return all open cards in a list."""
        raw = self._get(f"lists/{list_id}/cards", filter="open")
        cards = []
        for c in raw:
            attachments, checklists, comments = self._parse_extra(c["id"])
            cards.append(TrelloCard(
                id=c["id"],
                name=c["name"],
                desc=c.get("desc", ""),
                list_id=list_id,
                list_name="",
                labels=[lb["name"] for lb in c.get("labels", [])],
                url=c.get("url", ""),
                attachments=attachments,
                checklists=checklists,
                comments=comments,
            ))
        return cards

    def get_backlog_cards(self, list_name: str = "Backlog") -> list[TrelloCard]:
        """Return cards from the iteration backlog list."""
        lst = self.get_list_by_name(list_name)
        if lst is None:
            logger.warning("List %r not found on board. Available lists: %s",
                           list_name, [l.name for l in self.get_lists()])
            return []
        cards = self.get_cards_in_list(lst.id)
        logger.info("Found %d cards in '%s'", len(cards), list_name)
        return cards

    def get_card(self, card_id: str) -> TrelloCard:
        """Fetch a single card by ID."""
        c = self._get(f"cards/{card_id}")
        attachments, checklists, comments = self._parse_extra(card_id)
        return TrelloCard(
            id=c["id"],
            name=c["name"],
            desc=c.get("desc", ""),
            list_id=c["idList"],
            list_name="",
            labels=[lb["name"] for lb in c.get("labels", [])],
            url=c.get("url", ""),
            attachments=attachments,
            checklists=checklists,
            comments=comments,
        )

    # -- card mutations ----------------------------------------------------

    def update_card_description(self, card_id: str, new_desc: str) -> None:
        """Overwrite the card description (where AC lives)."""
        self._put(f"cards/{card_id}", desc=new_desc)
        logger.info("Updated description on card %s", card_id)

    def add_comment(self, card_id: str, text: str) -> None:
        """Add a comment to a card (e.g. pipeline status updates)."""
        self._post(f"cards/{card_id}/actions/comments", text=text)
        logger.info("Added comment to card %s", card_id)

    def add_label(self, card_id: str, label_color: str, label_name: str) -> None:
        """Add a coloured label to a card."""
        # Get or create label on the board
        labels = self._get(f"boards/{self.board_id}/labels")
        label_id = None
        for lb in labels:
            if lb.get("name") == label_name and lb.get("color") == label_color:
                label_id = lb["id"]
                break
        if not label_id:
            new_label = self._post(
                f"boards/{self.board_id}/labels",
                name=label_name,
                color=label_color,
            )
            label_id = new_label["id"]
        self._post(f"cards/{card_id}/idLabels", value=label_id)
        logger.info("Added label '%s' to card %s", label_name, card_id)

    def move_card_to_list(self, card_id: str, list_name: str) -> None:
        """Move a card to a different list by name."""
        lst = self.get_list_by_name(list_name)
        if lst is None:
            raise ValueError(f"List '{list_name}' not found on board.")
        self._put(f"cards/{card_id}", idList=lst.id)
        logger.info("Moved card %s to '%s'", card_id, list_name)

    def create_card(
        self,
        list_name: str,
        name: str,
        desc: str = "",
        label_names: list[str] | None = None,
        pos: str = "bottom",
    ) -> TrelloCard:
        """
        Create a new card in the specified list.

        Args:
            list_name:    Target list name (e.g. "Iteration Backlog")
            name:         Card title / one-liner
            desc:         Card description (markdown supported)
            label_names:  List of label names to add (created if not existing)
            pos:          "top" | "bottom" | float position value

        Returns:
            TrelloCard for the newly created card.
        """
        lst = self.get_list_by_name(list_name)
        if lst is None:
            available = [l.name for l in self.get_lists()]
            raise ValueError(
                f"List '{list_name}' not found on board. "
                f"Available: {available}"
            )

        raw = self._post(
            "cards",
            idList=lst.id,
            name=name,
            desc=desc,
            pos=pos,
        )

        card = TrelloCard(
            id=raw["id"],
            name=raw["name"],
            desc=raw.get("desc", ""),
            list_id=lst.id,
            list_name=list_name,
            labels=[lb["name"] for lb in raw.get("labels", [])],
            url=raw.get("url", ""),
        )

        # Attach labels if requested
        if label_names:
            label_colors = {
                # Severity
                "P1": "red",
                "P2": "orange",
                "P3": "yellow",
                "P4": "green",
                # Board standard labels (matches pH WIP board)
                "QA Reported": "orange",
                "FEDEX-APP": "purple",
                "FEDEX_REST": "blue",
                "INVESTIGATE": "pink",
                "L3-DEV": "sky",
                "MCSL": "lime",
            }
            for label_name in label_names:
                color = label_colors.get(label_name, "blue")
                try:
                    self.add_label(card.id, color, label_name)
                except Exception as exc:
                    logger.warning("Could not add label '%s': %s", label_name, exc)

        logger.info("Created card '%s' in '%s' (id=%s)", name, list_name, card.id)
        return card

    def get_card_members(self, card_id: str) -> list[dict]:
        """
        Return members assigned to a card.
        Each dict: {"id": str, "fullName": str, "username": str}
        """
        try:
            raw = self._get(f"cards/{card_id}/members")
            return [
                {
                    "id": m.get("id", ""),
                    "fullName": m.get("fullName", ""),
                    "username": m.get("username", ""),
                }
                for m in raw
            ]
        except Exception as exc:
            logger.warning("get_card_members failed for %s: %s", card_id, exc)
            return []

    def search_cards_on_board(self, query: str) -> list[TrelloCard]:
        """
        Search all open cards on this board by title keyword.
        Uses Trello search API scoped to the board.
        """
        try:
            raw = self._get(
                "search",
                query=query,
                idBoards=self.board_id,
                modelTypes="cards",
                cards_limit=10,
                card_fields="id,name,desc,idList,labels,url",
            )
            cards = []
            for c in raw.get("cards", []):
                cards.append(TrelloCard(
                    id=c["id"],
                    name=c["name"],
                    desc=c.get("desc", ""),
                    list_id=c.get("idList", ""),
                    list_name="",
                    labels=[lb["name"] for lb in c.get("labels", [])],
                    url=c.get("url", ""),
                ))
            return cards
        except Exception as exc:
            logger.warning("Board search failed: %s", exc)
            return []
