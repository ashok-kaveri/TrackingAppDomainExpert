"""
Release Analyser  —  Cross-Card RAG Pre-Screen
================================================
When a release is loaded (e.g. "TrackingApp 2.3.115" with 3 cards), this
module reads ALL cards together and uses the RAG knowledge base to find:

  1. Cross-card conflicts    — two cards touch the same setting/page
  2. Test ordering risks     — Card B will fail if Card A runs first
  3. Missing coverage        — scenarios no card covers but the KB says matter
  4. Release risk level      — overall go/no-go signal
  5. Suggested test order    — safest order to run this release's tests

Per-card validation (domain_validator.py) runs AFTER this — this gives
the big picture before diving into individual cards.

Usage:
    from pipeline.release_analyser import analyse_release, ReleaseAnalysis
    report = analyse_release(release_name="TrackingApp 2.3.115", cards=[card1, card2, card3])
    # report.risk_level      → "LOW" | "MEDIUM" | "HIGH"
    # report.conflicts       → list of conflict descriptions
    # report.ordering        → suggested card order with reasoning
    # report.coverage_gaps   → missing test scenarios across the release
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
from rag.vectorstore import search

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CardSummary:
    card_id: str
    card_name: str
    card_desc: str


@dataclass
class ReleaseAnalysis:
    release_name: str
    risk_level: str                           # "LOW" | "MEDIUM" | "HIGH"
    risk_summary: str                         # one-line verdict
    conflicts: list[dict] = field(default_factory=list)
    # Each conflict: {"cards": ["Card A", "Card B"], "area": "...", "description": "..."}
    ordering: list[dict] = field(default_factory=list)
    # Each entry: {"position": 1, "card_name": "...", "reason": "..."}
    coverage_gaps: list[str] = field(default_factory=list)
    kb_context_summary: str = ""
    sources: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

RELEASE_ANALYSIS_PROMPT = dedent("""\
    You are a senior QA lead and Tracking App domain expert.

    A release is about to go into QA. Your job is to analyse ALL cards
    in this release TOGETHER — looking for cross-card risks before any
    individual card validation or test case generation begins.

    RELEASE: {release_name}

    CARDS IN THIS RELEASE:
    {cards_block}

    KNOWLEDGE BASE CONTEXT (retrieved for this release):
    {context}

    Analyse the cards as a GROUP and respond in this EXACT JSON format
    (no extra text, no markdown fences):
    {{
      "risk_level": "LOW" | "MEDIUM" | "HIGH",
      "risk_summary": "<one sentence — overall risk verdict for this release>",
      "conflicts": [
        {{
          "cards": ["<Card Name A>", "<Card Name B>"],
          "area": "<shared UI area or setting name>",
          "description": "<what conflict or interference could occur>"
        }}
      ],
      "ordering": [
        {{
          "position": 1,
          "card_name": "<exact card name>",
          "reason": "<why this card should run at this position>"
        }}
      ],
      "coverage_gaps": [
        "<scenario or edge case not covered by any card but known from KB to be important>"
      ],
      "kb_context_summary": "<what the KB tells us about this release area — key constraints, behaviours, API limits>"
    }}

    Rules:
    - risk_level = LOW   if cards are independent with no shared settings
    - risk_level = MEDIUM if cards share a settings page or feature area
    - risk_level = HIGH  if one card's change could break another card's tests
    - conflicts: only real conflicts — two cards genuinely affecting the same toggle/setting/API
    - ordering: list ALL cards, even if order does not matter (say why it doesn't)
    - coverage_gaps: only gaps the KB tells us about — do not invent scenarios
    - Keep ALL string values concise — max 1 sentence each (≤ 25 words)
    - If no conflicts, return conflicts = []
    - If no coverage gaps, return coverage_gaps = []
    - Respond ONLY with the JSON object — no preamble, no explanation, no markdown fences
""")


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def analyse_release(
    release_name: str,
    cards: list[CardSummary],
) -> ReleaseAnalysis:
    """
    Run cross-card RAG analysis for an entire release.

    Args:
        release_name:  e.g. "TrackingApp 2.3.115"
        cards:         List of CardSummary objects (id, name, desc)

    Returns:
        ReleaseAnalysis dataclass
    """
    if not config.ANTHROPIC_API_KEY:
        return ReleaseAnalysis(
            release_name=release_name,
            risk_level="MEDIUM",
            risk_summary="Analysis skipped — ANTHROPIC_API_KEY not set.",
            error="ANTHROPIC_API_KEY not set",
        )

    if not cards:
        return ReleaseAnalysis(
            release_name=release_name,
            risk_level="LOW",
            risk_summary="No cards found in this release.",
        )

    # ── Step 1: Build combined RAG query from all card names + descriptions ──
    combined_query = f"Release: {release_name}\n\n"
    combined_query += "\n".join(
        f"{c.card_name}: {c.card_desc[:200]}"
        for c in cards
    )

    try:
        # Retrieve more chunks for cross-card analysis — k = 6 per card (capped at 20)
        k = min(6 * len(cards), 20)
        docs = search(combined_query, k=k)
        context = "\n\n".join(
            f"[{doc.metadata.get('source', 'KB')}]\n{doc.page_content}"
            for doc in docs
        )
        sources = list({
            doc.metadata.get("source_url", doc.metadata.get("source", "Unknown"))
            for doc in docs
        })
    except Exception as e:
        logger.warning("RAG search failed in release analyser: %s", e)
        context = "No context retrieved."
        sources = []

    # ── Step 2: Format cards block ───────────────────────────────────────────
    cards_block = ""
    for i, card in enumerate(cards, 1):
        desc_snippet = (card.card_desc or "(No description)").strip()[:400]
        cards_block += (
            f"Card {i}: {card.card_name}\n"
            f"Description: {desc_snippet}\n\n"
        )

    # ── Step 3: Ask Claude ───────────────────────────────────────────────────
    prompt = RELEASE_ANALYSIS_PROMPT.format(
        release_name=release_name,
        cards_block=cards_block.strip(),
        context=context or "No relevant knowledge base context found.",
    )

    # Scale max_tokens with card count — each card adds ~400 tokens of JSON output.
    # Minimum 3000, cap at 6000 to stay within model limits.
    _max_tokens = min(3000 + len(cards) * 400, 6000)

    try:
        llm = ChatAnthropic(
            model=config.CLAUDE_SONNET_MODEL,   # sonnet — better reasoning for multi-card analysis
            api_key=config.ANTHROPIC_API_KEY,
            temperature=0.1,
            max_tokens=_max_tokens,
        )
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = response.content.strip()
    except Exception as e:
        logger.error("Claude release analysis failed: %s", e)
        return ReleaseAnalysis(
            release_name=release_name,
            risk_level="MEDIUM",
            risk_summary="Analysis could not complete due to an API error.",
            error=str(e),
        )

    # ── Step 4: Parse response ───────────────────────────────────────────────
    try:
        # Strip markdown fences if present
        json_text = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        # If there is preamble text before the JSON object, extract the first {...} block
        if not json_text.startswith("{"):
            m = re.search(r"\{.*\}", json_text, re.DOTALL)
            if m:
                json_text = m.group(0)

        data = json.loads(json_text)

        return ReleaseAnalysis(
            release_name=release_name,
            risk_level=data.get("risk_level", "MEDIUM"),
            risk_summary=data.get("risk_summary", ""),
            conflicts=data.get("conflicts", []),
            ordering=data.get("ordering", []),
            coverage_gaps=data.get("coverage_gaps", []),
            kb_context_summary=data.get("kb_context_summary", ""),
            sources=sources,
        )

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to parse release analysis JSON: %s\nRaw: %s", e, raw[:300])
        return ReleaseAnalysis(
            release_name=release_name,
            risk_level="MEDIUM",
            risk_summary=raw[:200],
            error=f"JSON parse error: {e}",
        )
