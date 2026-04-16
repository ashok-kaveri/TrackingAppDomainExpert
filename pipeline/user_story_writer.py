"""
User Story Writer — RAG-powered US + AC generation.

Given a plain-English description of a feature need, queries the code + domain
knowledge base and asks Claude to produce a User Story with Acceptance Criteria.
Supports an iterative review loop via refine_user_story().
"""
from __future__ import annotations

import logging

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

US_WRITER_PROMPT = """\
You are a senior Product Owner / Business Analyst for the PluginHive Shopify Shipment Tracking & Notifications App.
The app provides shipment tracking, carrier notifications, branded tracking portal,
delivery status updates, carrier integrations, and more into Shopify stores via a Shopify embedded app.

## Task
Write a well-structured User Story with Acceptance Criteria for the feature request below.
Use the codebase and domain knowledge provided as context — reference existing behaviour
where relevant so the story is grounded in how the app actually works.

## Output Format
Return ONLY markdown in this exact structure (no preamble, no extra headings):

### User Story
**As a** [role],
**I want** [feature/capability],
**So that** [business benefit].

### Acceptance Criteria
1. **Given** [precondition] **When** [action] **Then** [expected outcome]
2. ...
(minimum 4 ACs — be specific and testable)

### Notes
[Edge cases, open questions, or implementation hints — omit section if nothing to add]

---

## Feature Request
{feature_request}

## Relevant Codebase Context
{code_context}

## Domain Knowledge Context
{domain_context}
"""

US_REFINE_PROMPT = """\
You previously generated this User Story:

{previous_us}

---

The product owner has requested the following changes:

{change_request}

---

Please regenerate the complete User Story + Acceptance Criteria incorporating these changes.
Keep exactly the same output format (### User Story, ### Acceptance Criteria, ### Notes).
Return ONLY the updated markdown — no preamble.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_claude(model: str | None = None) -> ChatAnthropic:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    return ChatAnthropic(
        model=model or config.CLAUDE_SONNET_MODEL,
        api_key=config.ANTHROPIC_API_KEY,
        temperature=0.3,
        max_tokens=2048,
    )


def _fetch_domain_context(query: str, k: int = 5) -> str:
    try:
        from rag.vectorstore import search as rag_search
        docs = rag_search(query, k=k)
        if not docs:
            return "No domain context available."
        return "\n\n".join(
            f"[{d.metadata.get('source', 'doc')}]\n{d.page_content[:500]}"
            for d in docs
        )
    except Exception as e:
        logger.debug("Domain RAG search skipped: %s", e)
        return "No domain context available."


def _fetch_code_context(query: str, k: int = 5) -> str:
    try:
        from rag.code_indexer import search_code
        docs = search_code(query, k=k)
        if not docs:
            return "No codebase context available."
        return "\n\n".join(
            f"[{d.metadata.get('file_path', 'code')}]\n```\n{d.page_content[:400]}\n```"
            for d in docs
        )
    except Exception as e:
        logger.debug("Code RAG search skipped: %s", e)
        return "No codebase context available."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_user_story(feature_request: str, model: str | None = None) -> str:
    """
    Generate a User Story + Acceptance Criteria from a plain-English feature request.

    Queries both the domain knowledge base (PluginHive docs) and the indexed
    codebase (backend + frontend) to ground the output in real app behaviour.

    Args:
        feature_request: PO's plain-English description of what they need
        model:           Claude model override (defaults to CLAUDE_SONNET_MODEL)

    Returns:
        Markdown string containing User Story + AC + Notes
    """
    domain_context = _fetch_domain_context(feature_request)
    code_context = _fetch_code_context(feature_request)

    prompt = US_WRITER_PROMPT.format(
        feature_request=feature_request.strip(),
        code_context=code_context,
        domain_context=domain_context,
    )

    claude = _get_claude(model)
    response = claude.invoke([HumanMessage(content=prompt)])
    return response.content.strip()


def refine_user_story(
    previous_us: str,
    change_request: str,
    model: str | None = None,
) -> str:
    """
    Refine an existing User Story based on PO feedback.

    Args:
        previous_us:    The previously generated US markdown
        change_request: PO's description of what to change
        model:          Claude model override

    Returns:
        Updated markdown string
    """
    prompt = US_REFINE_PROMPT.format(
        previous_us=previous_us.strip(),
        change_request=change_request.strip(),
    )
    claude = _get_claude(model)
    response = claude.invoke([HumanMessage(content=prompt)])
    return response.content.strip()
