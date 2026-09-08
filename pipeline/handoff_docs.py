"""
Handoff Docs  —  Release Support Guide / Business Brief generation
==================================================================
Builds support-ready and merchant-facing release documents from approved
Trello cards, and renders them to the shared PluginHive handoff PDF style.

Ported from MCSLDomainExpert. The PDF renderer below is kept identical across
the MCSL / FedEx / AU Post / Tracking App repos so every release package looks
the same; only PDF_BRAND and the domain-specific parts (app navigation, carrier
detection, prompts) differ per repo.

Entry points:
    build_handoff_context()            — Trello card -> HandoffDocContext
    generate_combined_support_guide()  — one release Support Guide (markdown)
    generate_combined_business_brief() — one release Business Brief (markdown)
    render_pdf_bytes()                 — markdown -> styled PDF bytes
"""
from __future__ import annotations

import datetime as _dt
import io
import logging
import os
import re
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)

# Per-request LLM timeout (seconds). Without this a stalled socket hangs the
# generation indefinitely.
_LLM_TIMEOUT_SECONDS = int(os.environ.get("TRACKING_LLM_TIMEOUT_SECONDS", "90"))

# Max output tokens per doc generation. Detailed cards (multi-part walkthroughs)
# overflow a smaller cap and get truncated mid-section.
_LLM_MAX_TOKENS = int(os.environ.get("TRACKING_DOC_MAX_TOKENS", "6000"))


# The Tracking App is a Shopify-only app, so there is one platform and one
# app-orientation block rather than MCSL's per-platform matrix.
PLATFORM_NAME = "Shopify"

STANDARD_TRACKING_NAVIGATION: tuple[str, ...] = (
    "Open Shopify Admin > Apps > PluginHive Shipment Tracking & Notifications.",
    "Use Orders for the imported order list, manual order import, and per-order shipment detail.",
    "Use Carriers for carrier setup, credentials, and carrier exclusions.",
    "Use General Settings for store-wide tracking preferences, and Notifications for email/SMS templates and triggers.",
    "Use Shopify Admin Orders for cross-checking fulfilment, carrier name, and tracking data at source.",
)


def detect_platform_scope(*texts: str) -> list[str]:
    """Platform scope for the Tracking App.

    The app only ships on Shopify, so this always reports Shopify. It exists so
    the document builders and PDF header keep the same shape as the other repos.
    """
    return [PLATFORM_NAME]


REQUEST_LOG_CALLOUT_RE = re.compile(
    r"^[-*]\s*(?:Request node|Request nodes|Request/response nodes|Request/log fields) to verify:",
    flags=re.IGNORECASE,
)


def is_request_log_callout(markdown_line: str) -> bool:
    """Return true when a markdown bullet should render as a request/log callout."""
    return bool(REQUEST_LOG_CALLOUT_RE.match((markdown_line or "").strip()))


# An H2 that opens a card section in a combined release package:
# "TRX-025 - Amazon orders fail import" or "941 - Add delivery estimate".
CARD_SECTION_HEADING_RE = re.compile(r"^(?:[A-Z]{1,4}-\d{1,5}|\d{1,6})\s+-\s+\S")

# H2s that belong to the package itself rather than to a story card.
PACKAGE_LEVEL_HEADINGS = frozenset({
    "included story cards",
    "included updates",
    "release overview",
    "availability",
    "how support should use this package",
})


def is_card_section_heading(heading_text: str, combined_package: bool = False) -> bool:
    """Return true when a heading starts a new story-card section.

    Inside a combined package every H2 except the package-level ones is a card,
    because each card's own sections were demoted to H3. That matters for cards
    whose title carries no story id, which an id pattern alone would miss and
    leave sharing a page with the card above.
    """
    text = (heading_text or "").strip()
    if not text or text.lower().rstrip(":") in PACKAGE_LEVEL_HEADINGS:
        return False
    if CARD_SECTION_HEADING_RE.match(text):
        return True
    return combined_package


def is_combined_package(markdown_lines: list[str]) -> bool:
    """True when the document is a release package with an index page."""
    return any(
        line.strip().lower() in ("## included story cards", "## included updates")
        for line in markdown_lines or []
    )


@dataclass
class HandoffDocContext:
    card_id: str
    card_name: str
    card_url: str = ""
    release_name: str = ""
    approved_at: str = ""
    card_description: str = ""
    card_comments: list[str] = field(default_factory=list)
    card_checklists: list[dict] = field(default_factory=list)
    acceptance_criteria: str = ""
    test_cases: str = ""
    ai_qa_summary: str = ""
    ai_qa_evidence: str = ""
    signoff_summary: str = ""
    developer_names: list[str] = field(default_factory=list)
    tester_names: list[str] = field(default_factory=list)
    toggle_names: list[str] = field(default_factory=list)
    carrier_names: list[str] = field(default_factory=list)
    platform_names: list[str] = field(default_factory=list)
    likely_navigation: list[str] = field(default_factory=list)
    generated_on: str = field(default_factory=lambda: _dt.datetime.now().strftime("%Y-%m-%d %H:%M"))


def split_card_members(members: list[dict]) -> tuple[list[str], list[str]]:
    try:
        from pipeline.bug_reporter import _is_qa as is_qa_name
    except Exception:
        def is_qa_name(_: str) -> bool:
            return False

    testers: list[str] = []
    developers: list[str] = []
    for member in members or []:
        full_name = (member.get("fullName") or member.get("username") or "").strip()
        if not full_name:
            continue
        if is_qa_name(full_name):
            if full_name not in testers:
                testers.append(full_name)
        elif full_name not in developers:
            developers.append(full_name)
    return developers, testers


# A toggle key: dotted segments, allowing the {accountUUID} placeholder cards use.
_TOGGLE_NAME = r"[A-Za-z0-9_{}-]+(?:\.[A-Za-z0-9_{}-]+)+"

# Dotted strings that look like toggles but are filenames, domains, or versions.
_NOT_A_TOGGLE_SUFFIX = (
    "js", "jsx", "ts", "tsx", "py", "md", "json", "yml", "yaml", "sql", "xml", "csv", "sh",
    "html", "css", "scss", "php", "mov", "mp4", "png", "jpg", "jpeg", "gif", "pdf", "zip",
    "com", "io", "org", "net", "dev", "log", "txt",
)


def detect_toggles(*texts: str) -> list[str]:
    """Pull feature-toggle keys out of card text.

    Card authors wrap keys in markdown ("**{accountUUID}.x.y.enabled**") and put
    bold between the colon and the value, so emphasis characters are stripped
    before matching.
    """
    patterns = [
        # "{accountUUID}.x.y.enabled": true
        rf"[`\"“”']?({_TOGGLE_NAME})[`\"“”']?\s*:\s*(?:true|false)",
        # the toggle {accountUUID}.tracking.page.enabled is ON
        rf"\btoggles?\b[^\n]{{0,40}}?({_TOGGLE_NAME})",
        rf"\bfeature flag\b[^\n]{{0,40}}?({_TOGGLE_NAME})",
        # a bare key on its own line, as cards often list it
        rf"^\s*({_TOGGLE_NAME}\.(?:enabled|disabled))\s*,?\s*$",
        # a bare {placeholder}-prefixed key on its own line
        r"^\s*(\{[A-Za-z0-9_]+\}\.[A-Za-z0-9_{}-]+(?:\.[A-Za-z0-9_{}-]+)*)\s*,?\s*$",
        rf"\brollout\b\s*[:=-]\s*({_TOGGLE_NAME})",
    ]
    found: list[str] = []
    for text in texts:
        # Drop markdown emphasis so bold inside or around a key does not split it.
        clean = re.sub(r"[*`_]{1,3}", "", text or "")
        for pattern in patterns:
            for match in re.findall(pattern, clean, flags=re.IGNORECASE | re.MULTILINE):
                value = re.sub(r"\s+", " ", match).strip(" -:,")
                if not value or value.rsplit(".", 1)[-1].lower() in _NOT_A_TOGGLE_SUFFIX:
                    continue
                if value not in found:
                    found.append(value)
    # Drop tails of longer keys: a mention of "tracking.page.enabled" alongside
    # "{shop}.myshopify.com.tracking.page.enabled" is the same toggle.
    return [
        value for value in found
        if not any(other != value and other.endswith("." + value) for other in found)
    ]


# Carriers that show up in Tracking App cards and tickets. The app tracks 900+
# carriers; this list only needs to cover the names cards actually name, so the
# document can say "Amazon Shipping" rather than "carrier-neutral".
_CARRIER_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Amazon Shipping",  ("amazon shipping", "amazon logistics", "amazon")),
    ("FedEx",            ("fedex", "fed ex")),
    ("UPS",              ("ups",)),
    ("DHL",              ("dhl",)),
    ("USPS",             ("usps",)),
    ("Australia Post",   ("australia post", "auspost", "eparcel", "mypost")),
    ("Canada Post",      ("canada post", "canadapost")),
    ("Royal Mail",       ("royal mail", "royalmail")),
    ("India Post",       ("india post", "indiapost")),
    ("Blue Dart",        ("blue dart", "bluedart")),
    ("Delhivery",        ("delhivery",)),
    ("DTDC",             ("dtdc",)),
    ("Estes",            ("estes",)),
    ("Chit Chats",       ("chit chats", "chitchats")),
    ("Veho",             ("veho",)),
    ("ShipBob",          ("shipbob",)),
    ("Sherpa",           ("sherpa",)),
    ("Landmark Global",  ("landmark global",)),
    ("Thailand Post",    ("thailand post", "thailandpost")),
    ("CoolRunner",       ("coolrunner", "cool runner")),
    ("Shippo",           ("shippo",)),
    ("Sendle",           ("sendle",)),
    ("Aramex",           ("aramex",)),
    ("GLS",              ("gls",)),
    ("Evri",             ("evri", "hermes")),
)

# Aliases short enough to collide with ordinary words need a word boundary.
_CARRIER_ALIAS_RE = {
    alias: re.compile(rf"\b{re.escape(alias)}\b", flags=re.IGNORECASE)
    for _, aliases in _CARRIER_ALIASES
    for alias in aliases
}


def detect_carriers(*texts: str) -> list[str]:
    """Canonical carrier names named anywhere in the card evidence."""
    combined = " ".join(text or "" for text in texts)
    found: list[str] = []
    for canonical, aliases in _CARRIER_ALIASES:
        if any(_CARRIER_ALIAS_RE[alias].search(combined) for alias in aliases):
            if canonical not in found:
                found.append(canonical)
    return found


def infer_navigation(*texts: str) -> list[str]:
    """Tracking App areas this card is most likely verified in."""
    combined = " ".join(text or "" for text in texts).lower()
    nav: list[str] = ["Shopify Admin > Apps > PluginHive Shipment Tracking & Notifications"]
    if any(token in combined for token in ("order", "import", "fulfil", "fulfill", "shipment")):
        nav.append("Orders -> order list / Import Orders")
    if any(token in combined for token in ("carrier", "credential", "exclusion", "tracking_company")):
        nav.append("Carriers -> carrier setup, credentials, and exclusions")
    if any(token in combined for token in ("setting", "preference", "filter", "orders to track")):
        nav.append("General Settings")
    if any(token in combined for token in ("notification", "email", "sms", "template", "trigger")):
        nav.append("Notifications -> templates and trigger rules")
    if any(token in combined for token in ("tracking page", "branded", "portal", "timeline", "banner", "logo")):
        nav.append("Tracking Page -> branded tracking portal settings")
    if any(token in combined for token in ("dashboard", "analytics", "on-time", "report", "delivery performance")):
        nav.append("Dashboard / Analytics")
    if any(token in combined for token in ("plan", "subscription", "billing", "allowance", "trial", "charge")):
        nav.append("Plans / subscription page")
    nav.append("Shopify Admin -> Orders for cross-checking fulfilment and tracking data at source")
    deduped: list[str] = []
    for item in nav:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _checklists_to_text(checklists: list[dict] | None) -> str:
    lines: list[str] = []
    for checklist in checklists or []:
        if not isinstance(checklist, dict):
            continue
        name = (checklist.get("name") or "").strip()
        if name:
            lines.append(name)
        for item in checklist.get("items", []) or []:
            item_name = (item.get("name") or "").strip()
            if item_name:
                state = (item.get("state") or "").strip()
                lines.append(f"- {item_name}" + (f" [{state}]" if state else ""))
    return "\n".join(lines)


def build_handoff_context(
    *,
    card,
    release_name: str = "",
    approved_at: str = "",
    acceptance_criteria: str = "",
    test_cases: str = "",
    ai_qa_summary: str = "",
    ai_qa_evidence: str = "",
    signoff_summary: str = "",
    members: list[dict] | None = None,
) -> HandoffDocContext:
    devs, testers = split_card_members(members or [])
    desc = getattr(card, "desc", "") or ""
    comments = [c for c in (getattr(card, "comments", []) or []) if c]
    checklists = getattr(card, "checklists", []) or []
    comments_text = "\n".join(comments)
    checklist_text = _checklists_to_text(checklists)
    evidence_text = "\n".join([comments_text, checklist_text]).strip()
    card_name = getattr(card, "name", "") or ""
    toggles = detect_toggles(desc, card_name, acceptance_criteria, test_cases, evidence_text)
    carriers = detect_carriers(
        desc, card_name, acceptance_criteria, test_cases, ai_qa_summary, ai_qa_evidence, evidence_text,
    )
    platforms = detect_platform_scope(
        desc, card_name, acceptance_criteria, test_cases, ai_qa_summary, ai_qa_evidence, evidence_text,
    )
    navigation = infer_navigation(
        desc, card_name, acceptance_criteria, test_cases, ai_qa_summary, ai_qa_evidence, evidence_text,
    )
    return HandoffDocContext(
        card_id=getattr(card, "id", ""),
        card_name=card_name,
        card_url=getattr(card, "url", "") or "",
        release_name=release_name,
        approved_at=approved_at,
        card_description=desc,
        card_comments=comments,
        card_checklists=checklists,
        acceptance_criteria=acceptance_criteria or desc,
        test_cases=test_cases,
        ai_qa_summary=ai_qa_summary,
        ai_qa_evidence=ai_qa_evidence,
        signoff_summary=signoff_summary,
        developer_names=devs,
        tester_names=testers,
        toggle_names=toggles,
        carrier_names=carriers,
        platform_names=platforms,
        likely_navigation=navigation,
    )


_SUPPORT_PROMPT = """You are writing a support-ready story-card guide for the PluginHive Shopify Shipment Tracking \
& Notifications App.

This document is used internally by support, QA, account managers, and escalation owners. \
It must be practical, support-facing, card-specific, and written so a support engineer can explain, \
verify, and escalate the change without reading code.

Use this EXACT section order — no other sections:

1. `# Support Guide: <Story ID or concise feature name>`
2. `<Original card title>` as a short subtitle line below the title when available.
3. `## Brief Description` — 2-4 sentences, one paragraph, explaining what changed, why it matters, \
   and the affected area of the app. Keep it support-facing, not marketing-heavy. No preamble.
4. `## Toggles & Prerequisites` — markdown table with columns `| Item | Detail |`. \
   At most 6 rows. Cover the platform, the toggle (write "None" when the card needs no toggle), the \
   release the merchant must be on, where in the app the change is visible, and any carrier/plan/store \
   prerequisite that actually matters for this card.
5. `## Step-by-Step Support Walkthrough` — numbered support steps to verify or explain the feature. \
   At most 10 numbered steps in total. Use scenarios (**Scenario A — ...**) only when a second scenario \
   covers genuinely different behaviour. One line per step — no sub-bullets restating the step. \
   Put the exact app area inside the relevant step: Orders, Import Orders, Carriers, General Settings, \
   Notifications, Tracking Page, Dashboard, Analytics, the plans page, or Shopify Admin Orders. \
   If the card requires payload, carrier field, or diagnostic log verification, add a highlighted \
   bullet immediately after the relevant step. The bullet must start with a plain dash and one of these \
   label texts, with no backticks or extra dash around the label: \
   Request node to verify:  /  Request nodes to verify:  /  Request/response nodes to verify:  /  \
   Request/log fields to verify:  \
   For example, exactly: - Request/log fields to verify: tracking_company, tracking_url
6. `## Expected Behaviour` — bullets, each a distinct signal support can observe: the UI result, the \
   tracking/notification result, the plan-usage result, or a known limitation. At most 8 bullets. \
   Do not repeat the walkthrough steps here. Call out any change merchants will notice without asking \
   for it, and any limitation that will generate tickets.

The document ends after `Expected Behaviour`.

Length: keep the whole document under 600 words. Support reads this during a call — every sentence must \
tell them something they would otherwise have to ask engineering. Cut anything else.

Accuracy rules:
- Name a specific field, value, or setting ONLY if the card evidence names it. Never round out a list \
  with plausible-sounding extras.
- When the card's developer or QA comments describe what actually shipped and the code-analysis section \
  describes a proposed fix, trust the comments — the analysis is often superseded.
- When the affected fields are not enumerable from the evidence, say "the fields the card names" rather \
  than guessing which ones.

Formatting rules:
- Use markdown tables with pipe syntax — header row, separator row (|---|---|), then data rows.
- Write for support, not engineering: no code, file, class, or method names, no API or schema jargon, \
  and no internal engineering terms. The only exemptions are the request/log callouts above and exact \
  toggle keys.
- Call out carrier names explicitly when the change is carrier-specific.
- No filler: no "this section describes", no restating the card title, no closing summary.
- DO NOT add: Merchant-Safe Explanation, Common Questions & Troubleshooting, Support Escalation Packet, \
  The Problem, The Solution, Key Benefits, User Story, Test Scenarios, Acceptance Criteria Checklist, \
  AI Code Analysis, Rollout Notes, or References.
- Use facts from the context only. If a detail is missing, phrase it as "Confirm from the live store or \
  card context" rather than inventing.

CONTEXT:
{context}
"""


_BUSINESS_PROMPT = """You are writing a customer-facing, marketing-style Business Brief for a new feature \
in the PluginHive Shopify Shipment Tracking & Notifications App.

Your audience is Shopify merchants, store owners, account managers, and product marketing — NOT engineers \
or QA teams. Write as if this will appear on a product update page or be shared with a customer success \
manager explaining the release to a merchant.

Tone: Friendly, benefit-first, real-world focused. No jargon, no API field names, no code identifiers.

Use this exact section order:
1. `# What's New: <feature name in plain English>`
2. `## Brief Description`
3. `## What You Can Do Now`
4. `## Real-World Scenarios`
5. `## Who Benefits`
6. `## How to Get Started`
7. `## What Stays the Same`

Section guidelines:
- **Brief Description**: Tell the story from a merchant's perspective. What frustration or blocker did \
  they hit? Use a realistic scenario (e.g. "If your customers ask where their parcel is..."). 2-4 sentences.
- **What You Can Do Now**: Plain-English bullets describing the new capability. No technical terms.
- **Real-World Scenarios**: 2-3 short named scenarios (### Scenario 1: ...) showing a real merchant \
  benefiting. Use concrete details: store type, carrier, what they do, what improves.
- **Who Benefits**: Short bullets — which merchant types, business sizes, or shipping patterns benefit most.
- **How to Get Started**: Simple numbered steps in plain English, using natural app navigation \
  (e.g. "Go to General Settings -> Orders to track"). No more than 5 steps.
- **What Stays the Same**: Reassure merchants what hasn't changed. Calm any migration concerns.

Rules:
- Never mention API fields, payloads, or code identifiers
- Never mention internal QA terms (acceptance criteria, test cases, regression)
- Use merchant-friendly language: "you can now", "your orders", "your customers"
- If the feature is carrier-specific, say the carrier name naturally ("Amazon Shipping", not a carrier code)
- Keep the whole document skimmable and upbeat, under about 400 words

Use facts from the context only. Do not invent features or scenarios not supported by the context.

CONTEXT:
{context}
"""


def _context_text(ctx: HandoffDocContext) -> str:
    checklist_text = _checklists_to_text(ctx.card_checklists)
    parts = [
        f"Card: {ctx.card_name}",
        f"Card URL: {ctx.card_url or '(none)'}",
        f"Release: {ctx.release_name or '(unknown)'}",
        f"Approved at: {ctx.approved_at or '(unknown)'}",
        f"Developed by: {', '.join(ctx.developer_names) if ctx.developer_names else 'Unknown'}",
        f"Tested by: {', '.join(ctx.tester_names) if ctx.tester_names else 'QA Team'}",
        f"Toggles: {', '.join(ctx.toggle_names) if ctx.toggle_names else 'None detected'}",
        f"Carriers: {', '.join(ctx.carrier_names) if ctx.carrier_names else 'Carrier-neutral or not explicitly stated'}",
        f"Platform: {', '.join(ctx.platform_names) if ctx.platform_names else PLATFORM_NAME}",
        "Likely Tracking App navigation:",
        "\n".join(f"- {item}" for item in (ctx.likely_navigation or [])),
        "",
        "CARD DESCRIPTION / CURRENT AC:",
        (ctx.acceptance_criteria or ctx.card_description or "").strip()[:7000],
        "",
        "LIVE TRELLO COMMENTS / QA NOTES:",
        ("\n\n".join(ctx.card_comments or []) or "None").strip()[:7000],
        "",
        "LIVE TRELLO CHECKLISTS:",
        (checklist_text or "None").strip()[:3000],
        "",
        "TEST CASES:",
        (ctx.test_cases or "").strip()[:6000],
        "",
        "AI QA SUMMARY:",
        (ctx.ai_qa_summary or "").strip()[:3000],
        "",
        "AI QA EVIDENCE:",
        (ctx.ai_qa_evidence or "").strip()[:5000],
        "",
        "SIGN-OFF / NOTES:",
        (ctx.signoff_summary or "").strip()[:2000],
    ]
    return "\n".join(parts).strip()


def _invoke_doc_prompt(prompt: str, ctx: HandoffDocContext) -> str:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage

    claude = ChatAnthropic(
        model=config.CLAUDE_SONNET_MODEL,
        api_key=config.ANTHROPIC_API_KEY,
        temperature=0.1,
        max_tokens=_LLM_MAX_TOKENS,
        default_request_timeout=_LLM_TIMEOUT_SECONDS,
    )
    resp = claude.invoke([HumanMessage(content=prompt.format(context=_context_text(ctx)))])
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    return content.strip()


def _standard_navigation_markdown(extra_navigation: list[str] | None = None) -> str:
    # The release support guide uses one fixed app-orientation block. Keep
    # card-specific paths in the walkthrough so this section stays predictable
    # across all cards.
    return "\n".join(f"- {item}" for item in STANDARD_TRACKING_NAVIGATION)


def _enforce_standard_navigation(markdown_text: str, ctx: HandoffDocContext) -> str:
    """Remove a generic app-location block from support guides.

    Card-specific navigation belongs in the walkthrough, where support sees it
    next to the action they need to perform.
    """
    heading = "## Where to Find This in the Tracking App"
    if not markdown_text or heading not in markdown_text:
        return markdown_text

    pattern = re.compile(
        rf"({re.escape(heading)}\s*\n)(.*?)(?=\n## (?:Step-by-Step Support Walkthrough|Expected Behaviour)\b|\Z)",
        flags=re.DOTALL,
    )
    sanitized, count = pattern.subn("", markdown_text, count=1)
    if not count:
        logger.warning("Could not remove generic navigation block for %s", ctx.card_name)
    return sanitized


def _enforce_toggle_scope(markdown_text: str, ctx: HandoffDocContext) -> str:
    if ctx.toggle_names:
        return markdown_text

    sanitized = markdown_text
    sanitized = re.sub(
        r"^\|\s*(?:None detected|No feature toggle(?: found in card notes)?\.?)\s*\|\s*Use live store/account state as the source of truth\.?\s*\|\s*$",
        "| Toggle | None — no feature toggle is required for this card; confirm the release, store, and card-specific prerequisites only. |",
        sanitized,
        flags=re.MULTILINE,
    )
    sanitized = re.sub(
        r"^\s*[-*]\s*Use live store/account state as the source of truth when a toggle is mentioned\.?\s*$\n?",
        "",
        sanitized,
        flags=re.MULTILINE,
    )
    sanitized = re.sub(
        r"^\s*[-*]\s*Exact toggle/config value if (?:the feature is )?gated\.?\s*$\n?",
        "",
        sanitized,
        flags=re.MULTILINE,
    )
    return sanitized


_CALLOUT_VARIANT_RE = re.compile(
    r"^[ \t]*[-*][ \t]*`?[ \t]*[-*]?[ \t]*"
    r"(Request(?: nodes?| /response nodes| /log fields|/response nodes|/log fields) to verify)"
    r"[ \t]*:?[ \t]*`?[ \t]*",
    flags=re.IGNORECASE | re.MULTILINE,
)


def _normalize_request_callouts(markdown_text: str) -> str:
    """Rewrite request/log callout bullets into the one shape the PDF highlights.

    Models copy the label out of the prompt with backticks or a doubled dash
    ("- `- Request nodes to verify:` ..."), which then renders as plain text
    instead of a highlighted box. Dropping the label's opening backtick can leave
    the rest of the line with an odd number of them, so unbalanced backticks are
    cleared from any line that was rewritten.
    """
    lines = []
    for line in (markdown_text or "").splitlines():
        fixed = _CALLOUT_VARIANT_RE.sub(lambda m: f"- {m.group(1)}: ", line)
        if fixed != line and fixed.count("`") % 2:
            fixed = fixed.replace("`", "")
        lines.append(fixed)
    return "\n".join(lines)


def _enforce_support_doc_guardrails(markdown_text: str, ctx: HandoffDocContext) -> str:
    sanitized = _normalize_request_callouts(markdown_text)
    return _enforce_toggle_scope(_enforce_standard_navigation(sanitized, ctx), ctx)


def _fallback_support_doc(ctx: HandoffDocContext) -> str:
    carriers = ", ".join(ctx.carrier_names) if ctx.carrier_names else "carrier-neutral"
    nav_steps = ctx.likely_navigation or ["Shopify Admin > Apps > PluginHive Shipment Tracking & Notifications"]
    story_label = _story_id(ctx) or ctx.card_name
    toggle_row = (
        "| Toggle | " + ", ".join(ctx.toggle_names) + " — confirm the exact value is enabled for the target store before testing. |"
        if ctx.toggle_names
        else "| Toggle | None — no feature toggle is required for this card; confirm the release, store, and card-specific prerequisites only. |"
    )
    return f"""## Support Guide: {story_label}

{ctx.card_name}

## Brief Description
This update covers the approved card scope for {carriers} behaviour in the Shopify Shipment Tracking & Notifications App. Use the card details, approved AC, test evidence, and live store state to explain or verify the visible merchant outcome.

## Toggles & Prerequisites

| Item | Detail |
|---|---|
| Platform | {PLATFORM_NAME} |
{toggle_row}
| Version | Confirm the merchant store is on {ctx.release_name or 'the target release'} before promising the behaviour. |
| Carrier / plan | Confirm the exact carrier setup, plan, and order data before testing carrier-specific behaviour. |

## Step-by-Step Support Walkthrough

1. Confirm the merchant store, release, carrier setup, and order context.
2. Open the relevant app area: {nav_steps[0]}.
3. Reproduce the workflow described in the approved card scope.
4. Check the visible result in the app, and the tracking, notification, or plan-usage evidence that goes with it.

## Expected Behaviour

- The feature behaves as described in the approved card scope — compare the live result with the approved AC and QA evidence.
- Existing order import, tracking, and notification flows continue to work.
"""


def _fallback_business_doc(ctx: HandoffDocContext) -> str:
    carriers = ", ".join(ctx.carrier_names) if ctx.carrier_names else "all supported carriers"
    return f"""# What's New: {ctx.card_name}

## Brief Description
Shopify merchants tracking shipments with {carriers} previously ran into a limitation in this area. \
This update removes that blocker so your orders and customer notifications keep flowing without workarounds.

## What You Can Do Now
- The improvement is available directly inside the Tracking App — no extra setup required
- Stores shipping with {carriers} benefit as soon as they are on this release
- Existing settings, tracking pages, and notification templates keep working exactly as before

## Real-World Scenarios

### Scenario 1: Day-to-day tracking
A merchant shipping with {carriers} now sees their orders tracked without hitting the previous limitation, \
so customers get accurate updates without anyone chasing it manually.

### Scenario 2: High-volume store
For stores handling many orders a day, this update removes a manual workaround, cutting the chance of a \
shipment going untracked.

## Who Benefits
- Shopify merchants shipping with {carriers}
- Stores that previously saw orders or tracking updates go missing in this flow
- Merchants who want fewer "where is my order?" tickets

## How to Get Started
1. Update to this release of the Tracking App
2. Open the app from Shopify Admin > Apps
3. Check the settings area relevant to your carriers
4. Confirm your recent orders are tracked as expected
5. Contact PluginHive support if you need help

## What Stays the Same
- Your existing carrier setup and notification templates are untouched
- Your branded tracking page keeps its current look and settings
- No migration or re-configuration is needed for current setups
"""


def generate_support_guide(ctx: HandoffDocContext) -> str:
    try:
        return _enforce_support_doc_guardrails(_invoke_doc_prompt(_SUPPORT_PROMPT, ctx), ctx)
    except Exception as exc:
        logger.warning("Support guide generation fell back to template: %s", exc)
        return _enforce_support_doc_guardrails(_fallback_support_doc(ctx), ctx)


def generate_business_brief(ctx: HandoffDocContext) -> str:
    try:
        return _invoke_doc_prompt(_BUSINESS_PROMPT, ctx)
    except Exception as exc:
        logger.warning("Business brief generation fell back to template: %s", exc)
        return _fallback_business_doc(ctx)


def _doc_title(ctx: HandoffDocContext) -> str:
    story_id_match = re.search(r"\b([A-Z]{1,4}-\d{1,5})\b", ctx.card_name or "")
    return story_id_match.group(1) if story_id_match else (ctx.card_name or ctx.card_id or "Card")


def _demote_markdown(markdown_text: str) -> str:
    """Nest a single-card document under a release-level package heading."""
    lines: list[str] = []
    for raw in (markdown_text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("### "):
            lines.append("#### " + line[4:].strip())
        elif line.startswith("## "):
            lines.append("### " + line[3:].strip())
        elif line.startswith("# "):
            lines.append("## " + line[2:].strip())
        else:
            lines.append(line)
    return "\n".join(lines).strip()


def _story_id(ctx: HandoffDocContext) -> str:
    """Story/card number only, for the index page `Story ID` column."""
    name = ctx.card_name or ""
    story_id_match = re.search(r"\b([A-Z]{1,4}-\d{1,5})\b", name)
    if story_id_match:
        return story_id_match.group(1)
    leading_number = re.match(r"\s*#?(\d{1,6})\b", name)
    if leading_number:
        return leading_number.group(1)
    return ""


def _story_title(ctx: HandoffDocContext) -> str:
    """Card title for the `Story Title` column.

    Strips the StoryLab card-name boilerplate ("From SL: TRX-025 — ") so the
    column holds the title only; the id already has its own column.
    """
    title = (ctx.card_name or "").strip()
    title = re.sub(r"^from\s+sl\s*:\s*", "", title, flags=re.IGNORECASE).strip()
    story_id = _story_id(ctx)
    if story_id and title.startswith(story_id):
        title = title[len(story_id):].lstrip(" -–—:#")
    return title or "(untitled card)"


def _strip_leading_h1(markdown_text: str) -> str:
    """Drop a per-card document's own H1 title.

    Inside a combined release package the wrapper already prints
    "<Story ID> - <Story Title>", so the card's own "# Support Guide: ..."
    line would render as a duplicate heading.
    """
    lines = (markdown_text or "").lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _table_cell(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ").strip()


def _trello_link_cell(ctx: HandoffDocContext) -> str:
    """Markdown link for the index page `Trello card link` column."""
    url = (ctx.card_url or "").strip()
    if not url:
        return "-"
    label = _story_id(ctx) or "Card"
    return f"[{label}]({url})"


def _release_summary_table(contexts: list[HandoffDocContext]) -> str:
    rows = [
        "| Story ID | Story Title | Toggle Name | Trello card link |",
        "|---|---|---|---|",
    ]
    for ctx in contexts:
        toggles = ", ".join(ctx.toggle_names) if ctx.toggle_names else "None"
        rows.append(
            f"| {_table_cell(_story_id(ctx)) or '-'} | {_table_cell(_story_title(ctx))} "
            f"| {_table_cell(toggles)} | {_trello_link_cell(ctx)} |"
        )
    return "\n".join(rows)


def _card_section_heading(ctx: HandoffDocContext) -> str:
    """`<Story ID> - <Story Title>` heading, without repeating the id inside the title."""
    story_id = _story_id(ctx)
    story_title = _story_title(ctx)
    return f"{story_id} - {story_title}" if story_id else story_title


def generate_combined_support_guide(contexts: list[HandoffDocContext], release_name: str = "") -> str:
    """Generate one release-level Support Guide containing all selected cards."""
    contexts = [ctx for ctx in contexts if ctx]
    release = release_name or (contexts[0].release_name if contexts else "") or "Tracking App Release"
    parts = [
        f"# {release} Support Guide",
        "",
        "## Included Story Cards",
        _release_summary_table(contexts),
    ]
    for ctx in contexts:
        parts.extend([
            "",
            f"## {_card_section_heading(ctx)}",
            _demote_markdown(_strip_leading_h1(generate_support_guide(ctx))),
        ])
    return "\n".join(part for part in parts if part is not None).strip()


def generate_combined_business_brief(contexts: list[HandoffDocContext], release_name: str = "") -> str:
    """Generate one release-level Business Brief containing all selected cards."""
    contexts = [ctx for ctx in contexts if ctx]
    release = release_name or (contexts[0].release_name if contexts else "") or "Tracking App Release"
    carriers = sorted({carrier for ctx in contexts for carrier in ctx.carrier_names})
    carrier_scope = ", ".join(carriers) if carriers else "supported tracking workflows"
    parts = [
        f"# What's New: {release}",
        "",
        "## Release Overview",
        f"This release includes {len(contexts)} approved update(s) for the PluginHive Shopify Shipment "
        f"Tracking & Notifications App, covering {carrier_scope}.",
        "",
        "## Included Updates",
        _release_summary_table(contexts),
    ]
    for ctx in contexts:
        parts.extend([
            "",
            f"## {_card_section_heading(ctx)}",
            _demote_markdown(_strip_leading_h1(generate_business_brief(ctx))),
        ])
    parts.extend([
        "",
        "## Availability",
        "These updates are available once the merchant store is on the target Tracking App release and any "
        "listed prerequisite or toggle is confirmed.",
    ])
    return "\n".join(part for part in parts if part is not None).strip()


def _register_fonts() -> tuple[str, str]:
    """Register Arial + Georgia TTF fonts. Returns (sans_family, serif_family)."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfbase.pdfmetrics import registerFontFamily

        _SF = "/System/Library/Fonts/Supplemental/"
        pdfmetrics.registerFont(TTFont("Arial",           _SF + "Arial.ttf"))
        pdfmetrics.registerFont(TTFont("Arial-Bold",      _SF + "Arial Bold.ttf"))
        pdfmetrics.registerFont(TTFont("Arial-Italic",    _SF + "Arial Italic.ttf"))
        pdfmetrics.registerFont(TTFont("Arial-BoldItalic",_SF + "Arial Bold Italic.ttf"))
        registerFontFamily("Arial", normal="Arial", bold="Arial-Bold",
                           italic="Arial-Italic", boldItalic="Arial-BoldItalic")

        pdfmetrics.registerFont(TTFont("Georgia",           _SF + "Georgia.ttf"))
        pdfmetrics.registerFont(TTFont("Georgia-Bold",      _SF + "Georgia Bold.ttf"))
        pdfmetrics.registerFont(TTFont("Georgia-Italic",    _SF + "Georgia Italic.ttf"))
        pdfmetrics.registerFont(TTFont("Georgia-BoldItalic",_SF + "Georgia Bold Italic.ttf"))
        registerFontFamily("Georgia", normal="Georgia", bold="Georgia-Bold",
                           italic="Georgia-Italic", boldItalic="Georgia-BoldItalic")
        return "Arial", "Georgia"
    except Exception:
        return "Helvetica", "Times-Roman"


_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FFFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """Drop emoji — the embedded PDF fonts render them as blank boxes."""
    return _EMOJI_RE.sub("", text or "").strip()


def _md_to_rl(text: str, sans: str = "Arial") -> str:
    """Convert basic markdown inline formatting to ReportLab XML tags."""
    text = _strip_emoji(text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Markdown links [label](url) → clickable ReportLab anchors. Do this first so the
    # label/url (which never contain markdown emphasis) survive the asterisk handling.
    def _link(m):
        label, url = m.group(1), m.group(2)
        return f'<a href="{url}" color="#1155CC">{label}</a>'
    text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', _link, text)
    # Stash `code` spans before emphasis handling. A literal asterisk inside a
    # code span (for example `*.enabled`) must not be read as an italic marker —
    # otherwise emphasis pairs across two code spans and emits interleaved tags
    # that ReportLab rejects.
    code_spans: list[str] = []

    def _stash(m):
        code_spans.append(m.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    text = re.sub(r'`([^`]+)`', _stash, text)
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', text)
    text = re.sub(r'\*\*(.+?)\*\*',     r'<b>\1</b>', text)
    text = re.sub(r'\*([^*\n]+?)\*',    r'<i>\1</i>', text)
    # Strip any unmatched asterisks left over (e.g. from BDD steps bleeding in)
    text = re.sub(r'\*+', '', text)
    for index, span in enumerate(code_spans):
        text = text.replace(f"\x00{index}\x00",
                            f'<font name="Courier" fontSize="9">{span}</font>')
    return text


# Brand line shown under the PDF title. Keep the handoff PDF styling identical
# across the MCSL / FedEx / AU Post / Tracking App repos — only this brand
# string differs.
PDF_BRAND = "PluginHive Tracking App"


def _pdf_subtitle(title: str, markdown_text: str) -> str:
    """Brand + platform/carrier scope line for the PDF header panel."""
    parts = [PDF_BRAND]
    platform_names = detect_platform_scope(title, markdown_text)
    if platform_names:
        parts.append(" / ".join(platform_names))
    carriers_found = detect_carriers(title)
    if carriers_found:
        parts.append("  /  ".join(carriers_found))
    return "  ·  ".join(parts)


def render_pdf_bytes(title: str, markdown_text: str) -> bytes:
    try:
        from reportlab.lib.colors import HexColor
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError("PDF rendering requires reportlab to be installed") from exc

    SANS, SERIF = _register_fonts()

    PAGE_W, PAGE_H = A4
    LM = RM = 0.7 * inch
    CW = PAGE_W - LM - RM

    # ── Professional navy / gold colour palette ──────────────────────────────
    C_NAVY      = HexColor("#0d1b3e")   # header background — deep navy
    C_NAVY_MID  = HexColor("#162447")   # header body rows
    C_NAVY_META = HexColor("#1a2f5e")   # metadata strip
    C_GOLD      = HexColor("#c9922a")   # badge label & subtitle — warm gold
    C_BLUE      = HexColor("#1d4ed8")   # section headings — royal blue
    C_ACCENT    = HexColor("#2563eb")   # left accent bar
    C_WHITE     = HexColor("#ffffff")
    C_META_TXT  = HexColor("#94a3b8")   # metadata strip text
    C_TEXT      = HexColor("#1e293b")   # body text — rich charcoal
    C_GRAY      = HexColor("#475569")   # secondary / quote text
    C_BORDER    = HexColor("#e2e8f0")   # dividers & table borders

    def _ps(name, **kw):
        return ParagraphStyle(name, **kw)

    # Fonts: Georgia for the big title impact, Arial everywhere else
    hdr_badge  = _ps("HBadge", fontName=f"{SANS}-Bold",   fontSize=8.5, leading=11, textColor=C_GOLD,
                               spaceAfter=2, tracking=60)
    hdr_title  = _ps("HTitle", fontName=f"{SERIF}-Bold",  fontSize=26,  leading=32, textColor=C_WHITE,
                               spaceAfter=4)
    hdr_sub    = _ps("HSub",   fontName=f"{SANS}-Bold",   fontSize=10.5,leading=14, textColor=C_GOLD,
                               spaceAfter=0)
    hdr_meta   = _ps("HMeta",  fontName=SANS,             fontSize=8.5, leading=12, textColor=C_META_TXT)
    h2_style   = _ps("H2",     fontName=f"{SANS}-Bold",   fontSize=12,  leading=16, textColor=C_BLUE,
                               spaceBefore=12, spaceAfter=2)
    h2_box_style = _ps("H2Box", fontName=f"{SANS}-Bold",  fontSize=12,  leading=16, textColor=C_BLUE,
                               spaceBefore=0, spaceAfter=0)
    h3_style   = _ps("H3",     fontName=f"{SANS}-BoldItalic", fontSize=11, leading=14, textColor=C_BLUE,
                               spaceBefore=8, spaceAfter=3)
    body_style = _ps("Body",   fontName=SANS,             fontSize=10.5, leading=16, textColor=C_TEXT,
                               spaceAfter=6)
    bullet_sty = _ps("Bullet", fontName=SANS,             fontSize=10.5, leading=16, textColor=C_TEXT,
                               spaceAfter=4, leftIndent=16)
    num_style  = _ps("Num",    fontName=SANS,             fontSize=10.5, leading=16, textColor=C_TEXT,
                               spaceAfter=4, leftIndent=18)
    quote_sty  = _ps("Quote",  fontName=f"{SERIF}-Italic",fontSize=10.5, leading=16, textColor=C_GRAY,
                               leftIndent=20, rightIndent=20, spaceAfter=8,
                               borderPadding=(6, 10, 6, 14),
                               borderColor=C_GOLD, borderWidth=0)

    # ── H2 rendered as a full-width heading box ─────────────────────────────
    C_BOX_BG     = HexColor("#f7f9ff")   # heading box fill — very light blue
    C_BOX_BORDER = HexColor("#dbe4f7")   # heading box border

    def _h2_row(text: str):
        p = Paragraph(_md_to_rl(text), h2_box_style)
        row = Table([[p]], colWidths=[CW])
        row.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), C_BOX_BG),
            ("BOX",           (0, 0), (-1, -1), 0.8, C_BOX_BORDER),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 11),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
            ("LEFTPADDING",   (0, 0), (-1, -1), 16),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 16),
        ]))
        return [Spacer(1, 0.10 * inch), row, Spacer(1, 0.09 * inch)]

    # ── Badge / subtitle detection ───────────────────────────────────────────
    tl = title.lower()
    if any(w in tl for w in ["delay", "fix", "bug", "error", "performance", "slow", "issue"]):
        badge_txt = "PERFORMANCE FIX"
    elif any(w in tl for w in ["new", "feature", "add", "introduc", "launch"]):
        badge_txt = "NEW FEATURE"
    else:
        badge_txt = "UPDATE"

    clean_title = re.sub(r'\[#\d+\]', '', title).strip()
    clean_title = re.sub(r'From SL:\s*[A-Z]+-\d+\s*[—–-]\s*', '', clean_title).strip()

    subtitle = _pdf_subtitle(title, markdown_text)

    # ── Parse markdown ───────────────────────────────────────────────────────
    lines = (markdown_text or "").splitlines()
    content_lines: list[str] = []
    skip_h1 = True
    for line in lines:
        if skip_h1 and line.startswith("# "):
            skip_h1 = False
            continue
        content_lines.append(line)

    # ── Canvas footer ────────────────────────────────────────────────────────
    buf = io.BytesIO()

    def _draw_footer(canvas_obj, doc):
        canvas_obj.saveState()
        # Thin gold rule above footer
        canvas_obj.setStrokeColor(C_GOLD)
        canvas_obj.setLineWidth(0.5)
        canvas_obj.line(LM, 26, PAGE_W - RM, 26)
        canvas_obj.setFillColor(C_GRAY)
        canvas_obj.setFont(SANS, 7.5)
        canvas_obj.drawString(LM, 12, f"Generated {_dt.datetime.now().strftime('%B %d, %Y')}  ·  Confidential — PluginHive")
        canvas_obj.drawCentredString(PAGE_W / 2, 12, f"Page {doc.page}")
        canvas_obj.drawRightString(PAGE_W - RM, 12, "pluginhive.com")
        canvas_obj.restoreState()

    doc_obj = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=LM, rightMargin=RM,
        topMargin=0.4 * inch, bottomMargin=0.5 * inch,
        title=title,
    )

    story: list = []

    # ── Header panel (deep navy) ─────────────────────────────────────────────
    badge_p = Paragraph(badge_txt, hdr_badge)
    title_p = Paragraph(clean_title, hdr_title)
    sub_p   = Paragraph(subtitle, hdr_sub)
    # No blurb row: the cover carries the badge, title and subtitle only.
    hdr_tbl = Table([[badge_p], [title_p], [sub_p]], colWidths=[CW])
    hdr_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_NAVY_MID),
        ("BACKGROUND",    (0, 0), (0, 0),   C_NAVY),
        ("TOPPADDING",    (0, 0), (0, 0), 18),
        ("BOTTOMPADDING", (0, 0), (0, 0),  4),
        ("TOPPADDING",    (0, 1), (0, 1),  4),
        ("BOTTOMPADDING", (0, 1), (0, 1),  6),
        ("TOPPADDING",    (0, 2), (0, 2),  2),
        ("BOTTOMPADDING", (0, 2), (0, 2), 18),
        ("LEFTPADDING",   (0, 0), (-1, -1), 22),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 22),
    ]))

    # Gold top-border accent line on header
    hdr_border = Table([[""]], colWidths=[CW], rowHeights=[3])
    hdr_border.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_GOLD),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))

    meta_txt = (
        f"Generated {_dt.datetime.now().strftime('%B %Y')}     ·     "
        f"PluginHive QA Team"
    )
    meta_tbl = Table([[Paragraph(meta_txt, hdr_meta)]], colWidths=[CW])
    meta_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_NAVY_META),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 22),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 22),
    ]))

    story += [hdr_border, hdr_tbl, meta_tbl, Spacer(1, 0.25 * inch)]

    # ── Styles for tables and checkboxes ────────────────────────────────────
    tbl_hdr  = _ps("TblHdr",  fontName=f"{SANS}-Bold", fontSize=9,   leading=12,
                               textColor=C_WHITE)
    tbl_cell = _ps("TblCell", fontName=SANS,            fontSize=9,   leading=13,
                               textColor=C_TEXT)
    tbl_cell_sm = _ps("TblSm", fontName=SANS,           fontSize=8.5, leading=12,
                               textColor=C_TEXT)
    chk_sty  = _ps("Chk",     fontName=SANS,            fontSize=10.5, leading=16,
                               textColor=C_TEXT, spaceAfter=3, leftIndent=16)
    node_sty = _ps("NodeCallout", fontName=f"{SANS}-Bold", fontSize=10, leading=14,
                               textColor=HexColor("#1e3a8a"), spaceAfter=0)
    code_sty = _ps("Code",    fontName="Courier",        fontSize=8.5, leading=12,
                               textColor=C_TEXT)
    note_sty = _ps("Note",    fontName=SANS,             fontSize=10,  leading=14,
                               textColor=C_TEXT)

    def _code_block(code_lines: list[str]):
        """Fenced ``` block → monospace box."""
        body = "<br/>".join(
            (ln.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
               .replace(" ", "&nbsp;")) or "&nbsp;"
            for ln in code_lines
        )
        tbl = Table([[Paragraph(body, code_sty)]], colWidths=[CW])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), HexColor("#f1f5f9")),
            ("BOX",           (0, 0), (-1, -1), 0.5, C_BORDER),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return tbl

    def _callout(quote_lines: list[str]):
        """Blockquote → note box; gold tint when it reads as a warning/QA note."""
        joined = " ".join(quote_lines).lower()
        warn = any(kw in joined for kw in ("warning", "caution", "qa note", "confirm"))
        body = "<br/>".join(_md_to_rl(ln) if ln.strip() else "&nbsp;" for ln in quote_lines)
        tbl = Table([[Paragraph(body, note_sty)]], colWidths=[CW])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), HexColor("#fefce8") if warn else HexColor("#f1f5f9")),
            ("LINEBEFORE",    (0, 0), (0, -1),  3, C_GOLD if warn else C_ACCENT),
            ("LEFTPADDING",   (0, 0), (-1, -1), 12),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
            ("TOPPADDING",    (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        return tbl

    def _flush_table(raw_rows: list[str]) -> None:
        """Parse buffered markdown table lines and append a styled ReportLab Table."""
        parsed: list[list[str]] = []
        for r in raw_rows:
            if re.match(r"^\|[-| :]+\|$", r.strip()):
                continue  # separator row
            cells = [c.strip() for c in r.strip().strip("|").split("|")]
            parsed.append(cells)
        if not parsed:
            return
        n_cols = max(len(r) for r in parsed)
        # Normalise column count
        parsed = [r + [""] * (n_cols - len(r)) for r in parsed]
        # Auto column widths: first col narrower, last col narrower for status cols
        header_cells = [c.lower() for c in parsed[0]]
        story_id_first = "story id" in header_cells[0]
        if n_cols == 4 and story_id_first:
            # Release index page: Story ID | Story Title | Toggle Name | Trello card link
            col_ws = [0.14 * CW, 0.39 * CW, 0.27 * CW, 0.20 * CW]
        elif n_cols == 3 and story_id_first:
            # Release index page without a toggle column
            col_ws = [0.14 * CW, 0.51 * CW, 0.35 * CW]
        elif n_cols == 3:
            col_ws = [0.06 * CW, 0.56 * CW, 0.38 * CW]
        elif n_cols == 2:
            col_ws = [0.32 * CW, 0.68 * CW]
        else:
            unit = CW / n_cols
            col_ws = [unit] * n_cols
        # Build cell paragraphs
        tbl_data: list[list] = []
        for ri, row in enumerate(parsed):
            style = tbl_hdr if ri == 0 else tbl_cell_sm
            tbl_data.append([Paragraph(_md_to_rl(cell), style) for cell in row])
        rl_tbl = Table(tbl_data, colWidths=col_ws, repeatRows=1)
        ts = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0),   C_NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0),   C_WHITE),
            ("FONTNAME",      (0, 0), (-1, 0),   f"{SANS}-Bold"),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1),  [C_WHITE, HexColor("#f1f5f9")]),
            ("GRID",          (0, 0), (-1, -1),  0.4, C_BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1),  5),
            ("BOTTOMPADDING", (0, 0), (-1, -1),  5),
            ("LEFTPADDING",   (0, 0), (-1, -1),  7),
            ("RIGHTPADDING",  (0, 0), (-1, -1),  7),
            ("VALIGN",        (0, 0), (-1, -1),  "TOP"),
        ])
        rl_tbl.setStyle(ts)
        story.append(rl_tbl)
        story.append(Spacer(1, 0.1 * inch))

    # ── Render content (with table buffering) ────────────────────────────────
    table_buf: list[str] = []
    seen_card_marker = False
    seen_page_h1 = False
    seen_card_section = False
    combined_package = is_combined_package(content_lines)

    def _maybe_flush():
        if table_buf:
            _flush_table(list(table_buf))
            table_buf.clear()

    idx = 0
    while idx < len(content_lines):
        line = content_lines[idx]
        idx += 1
        clean = line.strip()

        # Fenced code block
        if clean.startswith("```"):
            _maybe_flush()
            code_lines: list[str] = []
            while idx < len(content_lines) and not content_lines[idx].strip().startswith("```"):
                code_lines.append(content_lines[idx])
                idx += 1
            idx += 1  # closing fence
            if code_lines:
                story.append(_code_block(code_lines))
                story.append(Spacer(1, 0.08 * inch))
            continue

        # Blockquote / callout block
        if clean.startswith(">"):
            _maybe_flush()
            quote_lines = [re.sub(r"^>\s?", "", clean)]
            while idx < len(content_lines) and content_lines[idx].strip().startswith(">"):
                quote_lines.append(re.sub(r"^>\s?", "", content_lines[idx].strip()))
                idx += 1
            story.append(_callout(quote_lines))
            story.append(Spacer(1, 0.08 * inch))
            continue

        # Table row detection
        if clean.startswith("|"):
            table_buf.append(clean)
            continue
        else:
            _maybe_flush()

        if not clean:
            story.append(Spacer(1, 0.05 * inch))
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", clean):
            story.append(HRFlowable(width=CW, thickness=0.5, color=C_BORDER,
                                    spaceBefore=4, spaceAfter=6))
            continue
        if re.match(r"^CARD\s+\d+/\d+$", clean, flags=re.IGNORECASE):
            if seen_card_marker:
                story.append(PageBreak())
            seen_card_marker = True
            story.append(Paragraph(_md_to_rl(clean), body_style))
            continue
        if clean.startswith("# ") and not clean.startswith("## "):
            # A later H1 starts a new document section — give it its own page.
            if seen_page_h1:
                story.append(PageBreak())
            seen_page_h1 = True
            story.extend(_h2_row(clean[2:].strip()))
        elif clean.startswith("## "):
            heading = clean[3:].strip()
            # Every story card starts on its own page — including the first, so the
            # index page stands alone and no card begins halfway down another page.
            if is_card_section_heading(heading, combined_package):
                story.append(PageBreak())
                seen_card_section = True
            story.extend(_h2_row(heading))
        elif clean.startswith("### "):
            story.append(Paragraph(_md_to_rl(clean[4:].strip()), h3_style))
        elif re.match(r"^- \[[ xX]\]", clean):
            # Checkbox bullet: - [ ] or - [x]
            checked = bool(re.match(r"^- \[[xX]\]", clean))
            raw_text = re.sub(r"^- \[[ xX]\]\s*", "", clean)
            # Strip any leftover ** / * that _md_to_rl couldn't pair-match
            raw_text = re.sub(r"\*+", "", raw_text)
            text = _md_to_rl(raw_text)
            if checked:
                icon = f'<font color="#16a34a" fontName="{SANS}-Bold" fontSize="13">✓</font>'
            else:
                icon = f'<font color="#1d4ed8" fontName="{SANS}-Bold" fontSize="11">✦</font>'
            story.append(Paragraph(f"{icon}  {text}", chk_sty))
        elif is_request_log_callout(clean):
            text = re.sub(r"^[-*]\s*", "", clean).strip()
            node_tbl = Table([[Paragraph(_md_to_rl(text), node_sty)]], colWidths=[CW])
            node_tbl.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), HexColor("#dbeafe")),
                ("BOX",           (0, 0), (-1, -1), 0.7, HexColor("#2563eb")),
                ("LEFTPADDING",   (0, 0), (-1, -1), 10),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
                ("TOPPADDING",    (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(node_tbl)
            story.append(Spacer(1, 0.08 * inch))
        elif clean.startswith("- ") or clean.startswith("* "):
            text = _md_to_rl(clean[2:].strip())
            story.append(Paragraph(
                f'<font color="#c9922a" fontName="{SANS}-Bold">›</font>  {text}', bullet_sty,
            ))
        elif re.match(r"^\d+\.\s+", clean):
            m = re.match(r"^(\d+)\.\s+(.*)", clean)
            if m:
                n, cnt = m.group(1), _md_to_rl(m.group(2))
                story.append(Paragraph(
                    f'<font color="#1d4ed8" fontName="{SANS}-Bold">{n}.</font>  {cnt}', num_style,
                ))
        elif clean.startswith('"') or clean.startswith('\u201c'):
            q_tbl = Table([[Paragraph(_md_to_rl(clean), quote_sty)]], colWidths=[CW])
            q_tbl.setStyle(TableStyle([
                ("BACKGROUND",   (0, 0), (-1, -1), HexColor("#fefce8")),
                ("LINEAFTER",    (0, 0), (0, -1),  3, C_GOLD),
                ("LINEBEFORE",   (0, 0), (0, -1),  3, C_GOLD),
                ("TOPPADDING",   (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING",(0, 0), (-1, -1), 10),
                ("LEFTPADDING",  (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ]))
            story.append(q_tbl)
            story.append(Spacer(1, 0.06 * inch))
        else:
            story.append(Paragraph(_md_to_rl(clean), body_style))

    _maybe_flush()
    story.append(Spacer(1, 0.3 * inch))
    doc_obj.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return buf.getvalue()
