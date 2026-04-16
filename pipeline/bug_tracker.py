"""
Bug Tracker  —  QA Issue Raise & Backlog Check
================================================
When a QA engineer finds a bug during manual or agent exploration:

  1. Claude formats the raw report into Jira-style bug card
  2. Trello backlog is searched for similar existing issues
  3. Claude compares the new bug against existing cards to detect duplicates
  4. If duplicate found  → show the existing card (no new card created)
  5. If new issue       → show Jira-format draft for QA engineer approval
  6. On approval        → create the card in Trello under Iteration Backlog

Usage:
    from pipeline.bug_tracker import check_and_draft_bug, raise_bug
    result = check_and_draft_bug(
        issue_description="Tracking page broken, notifications not sending after carrier update",
        feature_context="Tracking Page — Carrier Integration",
        release="TrackingApp 2.3.115",
    )
    if result.is_duplicate:
        # result.duplicate_card  → existing TrelloCard
    else:
        # result.draft           → BugDraft (formatted, ready to show user)
        # On approval:
        card = raise_bug(result.draft)
"""
from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field
from textwrap import dedent

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

import config
from pipeline.trello_client import TrelloClient, TrelloCard

logger = logging.getLogger(__name__)

# Trello list name where new bugs land — matches the "Backlog" column on the pH WIP board
BACKLOG_LIST_NAME = "Backlog"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class BugDraft:
    """Jira-style bug card ready to be reviewed and raised."""
    title: str                        # One-line summary (card name in Trello)
    severity: str                     # P1 | P2 | P3 | P4
    feature_area: str                 # Which feature/section
    steps_to_reproduce: list[str]     # Numbered steps
    expected_behavior: str
    actual_behavior: str
    environment: str = "QA"
    labels: list[str] = field(default_factory=list)  # e.g. ["Bug", "QA-Found", "P2"]
    release: str = ""
    raw_description: str = ""         # Original QA engineer text

    def to_trello_desc(self) -> str:
        """Format as Trello card description (markdown)."""
        steps = "\n".join(f"{i+1}. {s}" for i, s in enumerate(self.steps_to_reproduce))
        labels_str = " · ".join(f"`{lb}`" for lb in self.labels)
        return dedent(f"""\
            ## 🐛 Bug Report

            **Type:** Bug
            **Severity:** {self.severity}
            **Feature Area:** {self.feature_area}
            **Environment:** {self.environment}
            **Release:** {self.release or 'Unreleased'}
            **Labels:** {labels_str}

            ---

            ### Steps to Reproduce
            {steps}

            ### Expected Behaviour
            {self.expected_behavior}

            ### Actual Behaviour
            {self.actual_behavior}

            ---
            *Raised via TrackingApp QA Pipeline — Bug Tracker*
        """).strip()

    def to_display_markdown(self) -> str:
        """Formatted display for the Streamlit dashboard preview."""
        steps = "\n".join(f"{i+1}. {s}" for i, s in enumerate(self.steps_to_reproduce))
        sev_colors = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "🟢"}
        sev_icon = sev_colors.get(self.severity, "⚪")
        return dedent(f"""\
            ### {sev_icon} [{self.severity}] {self.title}

            | Field | Value |
            |-------|-------|
            | **Severity** | {self.severity} |
            | **Feature Area** | {self.feature_area} |
            | **Environment** | {self.environment} |
            | **Release** | {self.release or 'Unreleased'} |
            | **Labels** | {', '.join(self.labels)} |

            **Steps to Reproduce**
            {steps}

            **Expected Behaviour**
            {self.expected_behavior}

            **Actual Behaviour**
            {self.actual_behavior}
        """).strip()


@dataclass
class BugCheckResult:
    """Result of check_and_draft_bug()."""
    is_duplicate: bool
    duplicate_card: TrelloCard | None = None   # set if is_duplicate=True
    duplicate_reason: str = ""                  # why Claude thinks it's a match
    draft: BugDraft | None = None              # set if is_duplicate=False
    error: str = ""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

FORMAT_BUG_PROMPT = dedent("""\
    You are a senior QA engineer writing a bug report for the Tracking App.

    A team member reported this issue during manual QA:
    {raw_description}

    Feature context: {feature_context}
    Release: {release}

    Format this into a structured bug report. Respond ONLY in this JSON (no markdown fences):
    {{
      "title": "<concise one-line bug summary — max 80 chars>",
      "severity": "P1" | "P2" | "P3" | "P4",
      "feature_area": "<which page or feature area, e.g. Tracking Page, Notifications, Carriers>",
      "steps_to_reproduce": [
        "<step 1>",
        "<step 2>",
        "<step 3>"
      ],
      "expected_behavior": "<what should happen>",
      "actual_behavior": "<what actually happens>",
      "labels": ["Bug", "QA-Found", "<P1|P2|P3|P4>"]
    }}

    Severity guide:
    - P1: App crash, tracking page broken, notifications not sending, data loss
    - P2: Core feature broken, wrong tracking status, carrier integration fails
    - P3: UI inconsistency, incorrect display, non-blocking setting issue
    - P4: Minor UX issue, typo, cosmetic

    Rules:
    - steps_to_reproduce: minimum 2, maximum 6 steps
    - Be specific — include exact UI element names, page names, setting values
    - expected_behavior and actual_behavior: one sentence each
    - If severity is unclear from the report, default to P3
    - labels ALWAYS includes "QA Reported" and "TRACKING-APP" plus the severity label (P1/P2/P3/P4)
    - example labels output: ["QA Reported", "TRACKING-APP", "P2"]
""")

DUPLICATE_CHECK_PROMPT = dedent("""\
    You are a QA lead checking if a new bug report already exists in the backlog.

    NEW BUG BEING REPORTED:
    Title: {new_title}
    Description: {new_desc}

    EXISTING BACKLOG CARDS (title and description snippet):
    {existing_cards}

    Are any of the existing cards describing the same bug or the same root cause?

    Respond ONLY in this JSON (no markdown fences):
    {{
      "is_duplicate": true | false,
      "matching_card_index": <0-based index of the matching card, or -1 if none>,
      "confidence": "HIGH" | "MEDIUM" | "LOW",
      "reason": "<one sentence: why this is or is not a duplicate>"
    }}

    Rules:
    - is_duplicate = true only if HIGH or MEDIUM confidence
    - Two bugs are duplicates if they describe the same broken behaviour in the same feature
    - Different symptoms of the same root cause = duplicate
    - Same symptom in different features = NOT duplicate
    - If is_duplicate = false, set matching_card_index = -1
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_claude() -> ChatAnthropic:
    return ChatAnthropic(
        model=config.CLAUDE_SONNET_MODEL,
        api_key=config.ANTHROPIC_API_KEY,
        temperature=0.1,
        max_tokens=1024,
    )


def _ask_claude(claude: ChatAnthropic, prompt: str) -> dict:
    """Invoke Claude and parse JSON response."""
    resp = claude.invoke([HumanMessage(content=prompt)])
    raw = resp.content.strip()
    json_text = re.sub(r"```(?:json)?\n?", "", raw).strip().rstrip("`")
    return json.loads(json_text)


def _fetch_backlog_cards(list_name: str = BACKLOG_LIST_NAME) -> list[TrelloCard]:
    """Fetch all open cards from the Iteration Backlog list."""
    try:
        trello = TrelloClient()
        return trello.get_backlog_cards(list_name)
    except Exception as exc:
        logger.warning("Could not fetch backlog cards: %s", exc)
        return []


def _quick_keyword_filter(
    new_title: str,
    cards: list[TrelloCard],
    top_n: int = 15,
) -> list[TrelloCard]:
    """
    Pre-filter backlog cards by keyword overlap before sending to Claude.
    Avoids hitting Claude with 200+ card descriptions.
    Keeps top_n most keyword-overlapping cards.
    """
    new_words = set(re.sub(r"[^a-z0-9 ]", " ", new_title.lower()).split())
    # Remove stop words
    stop = {"the", "a", "an", "is", "in", "on", "at", "to", "for", "and", "or",
            "not", "with", "of", "it", "this", "that", "are", "was", "has"}
    new_words -= stop

    scored: list[tuple[int, TrelloCard]] = []
    for card in cards:
        card_words = set(re.sub(r"[^a-z0-9 ]", " ", card.name.lower()).split()) - stop
        overlap = len(new_words & card_words)
        scored.append((overlap, card))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [card for _, card in scored[:top_n]]


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def check_and_draft_bug(
    issue_description: str,
    feature_context: str = "",
    release: str = "",
    backlog_list_name: str = BACKLOG_LIST_NAME,
) -> BugCheckResult:
    """
    Format a QA-reported issue and check if it already exists in the backlog.

    Steps:
      1. Claude formats the raw report into a Jira-style BugDraft
      2. Trello backlog cards are fetched
      3. Claude checks the top keyword-matched cards for duplicates
      4. Returns BugCheckResult (duplicate or new draft)

    Args:
        issue_description:  Raw text from the QA engineer
        feature_context:    Optional feature/page context (e.g. "Tracking Page")
        release:            Release label (e.g. "TrackingApp 2.3.115")
        backlog_list_name:  Trello list to search (default: "Backlog")

    Returns:
        BugCheckResult
    """
    if not config.ANTHROPIC_API_KEY:
        return BugCheckResult(
            is_duplicate=False,
            error="ANTHROPIC_API_KEY not set",
        )

    if not issue_description.strip():
        return BugCheckResult(
            is_duplicate=False,
            error="Issue description is empty",
        )

    claude = _get_claude()

    # ── Step 1: Format into Jira-style draft ─────────────────────────────
    logger.info("Formatting bug report: %s…", issue_description[:80])
    try:
        fmt_prompt = FORMAT_BUG_PROMPT.format(
            raw_description=issue_description.strip(),
            feature_context=feature_context or "Tracking App",
            release=release or "Unknown",
        )
        data = _ask_claude(claude, fmt_prompt)

        draft = BugDraft(
            title=data.get("title", issue_description[:80]),
            severity=data.get("severity", "P3"),
            feature_area=data.get("feature_area", feature_context or "Unknown"),
            steps_to_reproduce=data.get("steps_to_reproduce", [issue_description]),
            expected_behavior=data.get("expected_behavior", ""),
            actual_behavior=data.get("actual_behavior", issue_description),
            labels=data.get("labels", ["Bug", "QA-Found", "TRACKING-APP", "P3"]),
            release=release,
            raw_description=issue_description,
        )
    except Exception as exc:
        logger.error("Bug formatting failed: %s", exc)
        return BugCheckResult(
            is_duplicate=False,
            error=f"Could not format bug report: {exc}",
        )

    # ── Step 2: Fetch backlog and keyword-filter ──────────────────────────
    logger.info("Fetching backlog cards to check for duplicates…")
    backlog_cards = _fetch_backlog_cards(backlog_list_name)

    if not backlog_cards:
        logger.info("Backlog empty or unreachable — skipping duplicate check")
        return BugCheckResult(is_duplicate=False, draft=draft)

    candidate_cards = _quick_keyword_filter(draft.title, backlog_cards, top_n=15)
    if not candidate_cards:
        return BugCheckResult(is_duplicate=False, draft=draft)

    # ── Step 3: Claude duplicate check ───────────────────────────────────
    existing_cards_text = ""
    for i, card in enumerate(candidate_cards):
        desc_snippet = (card.desc or "").strip()[:200]
        existing_cards_text += f"[{i}] {card.name}\n    {desc_snippet}\n\n"

    logger.info("Checking %d candidate backlog cards for duplicates…", len(candidate_cards))
    try:
        dup_prompt = DUPLICATE_CHECK_PROMPT.format(
            new_title=draft.title,
            new_desc=draft.actual_behavior,
            existing_cards=existing_cards_text.strip(),
        )
        dup_data = _ask_claude(claude, dup_prompt)

        is_dup  = dup_data.get("is_duplicate", False)
        idx     = dup_data.get("matching_card_index", -1)
        reason  = dup_data.get("reason", "")

        if is_dup and 0 <= idx < len(candidate_cards):
            matched_card = candidate_cards[idx]
            logger.info("Duplicate found: '%s' matches '%s'", draft.title, matched_card.name)
            return BugCheckResult(
                is_duplicate=True,
                duplicate_card=matched_card,
                duplicate_reason=reason,
                draft=draft,   # kept so UI can show what was detected
            )

    except Exception as exc:
        logger.warning("Duplicate check failed: %s — proceeding as new bug", exc)

    # ── No duplicate found — return the draft for approval ────────────────
    logger.info("No duplicate found — draft ready for QA approval")
    return BugCheckResult(is_duplicate=False, draft=draft)


def raise_bug(
    draft: BugDraft,
    backlog_list_name: str = BACKLOG_LIST_NAME,
) -> TrelloCard:
    """
    Create the approved bug draft as a Trello card in the Iteration Backlog.

    Args:
        draft:              Approved BugDraft
        backlog_list_name:  Trello list to create the card in

    Returns:
        The created TrelloCard (with .url for direct link)
    """
    trello = TrelloClient()
    card = trello.create_card(
        list_name=backlog_list_name,
        name=draft.title,
        desc=draft.to_trello_desc(),
        label_names=draft.labels,
        pos="top",   # new bugs go to the top of the backlog
    )
    logger.info("Bug raised in Trello: %s → %s", card.name, card.url)
    return card
