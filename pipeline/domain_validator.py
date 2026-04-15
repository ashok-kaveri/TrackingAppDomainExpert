"""
Domain Validator  —  Pipeline Step 1.5
=======================================
Uses the RAG knowledge base (ChromaDB + nomic-embed-text) to validate
a Trello card's description and acceptance criteria BEFORE test cases
are generated.

Checks:
  1. Requirement gaps   — known behaviours missing from the card description
  2. AC gaps            — acceptance criteria scenarios not covered
  3. Accuracy issues    — anything contradicting actual Tracking App behaviour
  4. App-specific       — carrier constraints, notification rules, edge cases
  5. Suggestions        — improvements to the card wording or scope

Returns a ValidationReport dataclass shown in the dashboard UI.
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
# Prompt
# ---------------------------------------------------------------------------

VALIDATION_PROMPT = dedent("""\
    You are a senior domain expert and QA lead for the PluginHive Shopify Shipment Tracking & Notifications App.

    A new Trello card has come in. Your job is to validate the card's requirements and
    acceptance criteria against the knowledge base before test cases are generated.

    Knowledge base context (retrieved for this feature):
    {context}

    ---
    Card Name: {card_name}

    Card Description / Requirements:
    {card_desc}

    Acceptance Criteria (if already written):
    {acceptance_criteria}
    ---

    Analyse carefully and respond in this EXACT JSON format (no extra text, no markdown fences):
    {{
      "overall_status": "PASS" | "NEEDS_REVIEW" | "FAIL",
      "summary": "<one sentence — what this card is about and your overall verdict>",
      "requirement_gaps": [
        "<requirement or behaviour known from the KB that is missing from the card>"
      ],
      "ac_gaps": [
        "<acceptance criteria scenario not covered — e.g. error state, edge case, boundary>"
      ],
      "accuracy_issues": [
        "<anything in the description that contradicts how the Tracking App actually works>"
      ],
      "suggestions": [
        "<improvement to wording, scope, or test coverage>"
      ],
      "kb_insights": "<what the knowledge base tells us about this feature — key facts, constraints, known behaviours>"
    }}

    Rules:
    - overall_status = PASS if no significant gaps or issues
    - overall_status = NEEDS_REVIEW if minor gaps or suggestions only
    - overall_status = FAIL if accuracy issues or critical missing requirements
    - Keep each item concise (1–2 sentences max)
    - If a list has nothing to report, return an empty array []
    - Only reference what is in the knowledge base context above — do not invent
""")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ValidationReport:
    overall_status: str                  # "PASS" | "NEEDS_REVIEW" | "FAIL"
    summary: str
    requirement_gaps: list[str] = field(default_factory=list)
    ac_gaps: list[str] = field(default_factory=list)
    accuracy_issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    kb_insights: str = ""
    sources: list[str] = field(default_factory=list)
    error: str = ""                      # set if validation itself failed


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate_card(
    card_name: str,
    card_desc: str,
    acceptance_criteria: str = "",
) -> ValidationReport:
    """
    Validate a Trello card against the Tracking App knowledge base.

    Args:
        card_name:           Title of the Trello card
        card_desc:           Full description / requirements on the card
        acceptance_criteria: AC already written (may be empty for new cards)

    Returns:
        ValidationReport dataclass
    """
    if not config.ANTHROPIC_API_KEY:
        return ValidationReport(
            overall_status="NEEDS_REVIEW",
            summary="Validation skipped — ANTHROPIC_API_KEY not set.",
            error="ANTHROPIC_API_KEY not set",
        )

    # ── Step 1: Retrieve relevant context from RAG knowledge base ────────────
    query = f"{card_name} {card_desc[:300]}"
    try:
        docs = search(query, k=config.TOP_K_RESULTS)
        context = "\n\n".join(
            f"[Source: {doc.metadata.get('source_url', doc.metadata.get('source', 'KB'))}]\n{doc.page_content}"
            for doc in docs
        )
        sources = list({
            doc.metadata.get("source_url", doc.metadata.get("source", "Unknown"))
            for doc in docs
        })
    except Exception as e:
        logger.warning("RAG search failed during validation: %s", e)
        context = "No context retrieved — knowledge base may not be indexed yet."
        sources = []

    # ── Step 2: Build prompt ─────────────────────────────────────────────────
    prompt = VALIDATION_PROMPT.format(
        context=context or "No relevant context found in knowledge base.",
        card_name=card_name,
        card_desc=card_desc.strip() or "(No description provided)",
        acceptance_criteria=acceptance_criteria.strip() or "(Not yet written)",
    )

    # ── Step 3: Call Claude ──────────────────────────────────────────────────
    try:
        llm = ChatAnthropic(
            model=config.CLAUDE_HAIKU_MODEL,   # fast + cheap for validation
            api_key=config.ANTHROPIC_API_KEY,
            temperature=0.1,
            max_tokens=1500,
        )
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = response.content.strip()
    except Exception as e:
        logger.error("Claude validation call failed: %s", e)
        return ValidationReport(
            overall_status="NEEDS_REVIEW",
            summary="Validation could not complete due to an API error.",
            error=str(e),
        )

    # ── Step 4: Parse JSON response ──────────────────────────────────────────
    try:
        json_text = re.sub(r"```(?:json)?", "", raw).strip()
        data = json.loads(json_text)

        return ValidationReport(
            overall_status=data.get("overall_status", "NEEDS_REVIEW"),
            summary=data.get("summary", ""),
            requirement_gaps=data.get("requirement_gaps", []),
            ac_gaps=data.get("ac_gaps", []),
            accuracy_issues=data.get("accuracy_issues", []),
            suggestions=data.get("suggestions", []),
            kb_insights=data.get("kb_insights", ""),
            sources=sources,
        )

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to parse validation JSON: %s\nRaw: %s", e, raw[:300])
        return ValidationReport(
            overall_status="NEEDS_REVIEW",
            summary=raw[:300],
            error=f"JSON parse error: {e}",
        )
