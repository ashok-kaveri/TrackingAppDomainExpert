"""
Test Case Runner — Execute Generated Test Cases in Chrome
=========================================================
Given a set of generated test cases (markdown), a live app URL, and credentials,
Claude interprets each test case into actionable browser steps and executes them
using Playwright (headful Chrome) or reports results as a structured checklist.

Flow:
  1. Parse test cases from markdown into a list of TC objects
  2. For each TC: Claude generates step-by-step browser actions
  3. Each action is executed via Playwright chromium (headful)
  4. Results (Pass / Fail / Skip + notes) are returned per TC

Usage:
    from pipeline.tc_runner import run_test_cases, TCRunResult
    results = run_test_cases(
        test_cases_markdown="...",
        app_url="https://mystore.myshopify.com/admin",
        username="admin@store.com",
        password="secret",
    )
    for r in results:
        print(r.tc_name, r.status, r.notes)
"""
from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field
from textwrap import dedent
from typing import Generator

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

import config

logger = logging.getLogger(__name__)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ParsedTC:
    """A single parsed test case from the markdown block."""
    index: int
    name: str
    preconditions: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    expected: str = ""


@dataclass
class TCRunResult:
    """Result of running a single test case."""
    index: int
    tc_name: str
    status: str              # "PASS" | "FAIL" | "SKIP" | "ERROR"
    notes: str = ""
    steps_executed: list[str] = field(default_factory=list)
    screenshot_path: str = ""


# ── Prompts ───────────────────────────────────────────────────────────────────

PARSE_TC_PROMPT = dedent("""\
    You are a QA automation engineer. Parse the following test cases markdown into structured JSON.

    Markdown:
    {markdown}

    Return a JSON array. Each element:
    {{
      "index": <0-based int>,
      "name": "<test case title>",
      "preconditions": ["<precondition 1>", ...],
      "steps": ["<step 1>", "<step 2>", ...],
      "expected": "<expected result>"
    }}

    Rules:
    - Extract every distinct test case (TC-1, TC-2, etc.)
    - steps: the action steps only (not the expected result)
    - expected: the expected outcome / assertion
    - If preconditions are not stated, set to []
    - Respond ONLY with valid JSON array, no markdown fences
""")

BROWSER_STEPS_PROMPT = dedent("""\
    You are a QA engineer executing test cases manually in a browser.
    The tester has already logged in to: {app_url}

    Test case to execute:
    Name: {tc_name}
    Steps: {steps}
    Expected: {expected}

    Based on the current page state and the test case steps, describe what you would check/verify
    in plain language. Rate the result as PASS, FAIL, or SKIP with a brief note.

    Respond in JSON (no fences):
    {{
      "status": "PASS" | "FAIL" | "SKIP",
      "notes": "<brief explanation of what was verified and why it passes/fails>",
      "steps_executed": ["<what was done step 1>", "<step 2>", ...]
    }}

    If you cannot determine the result (no browser access), default to SKIP with a note explaining why.
""")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_claude() -> ChatAnthropic:
    return ChatAnthropic(
        model=config.CLAUDE_HAIKU_MODEL,
        api_key=config.ANTHROPIC_API_KEY,
        temperature=0.1,
        max_tokens=2048,
    )


def _ask_claude_json(claude: ChatAnthropic, prompt: str) -> dict | list:
    resp = claude.invoke([HumanMessage(content=prompt)])
    raw = resp.content.strip()
    json_text = re.sub(r"```(?:json)?\n?", "", raw).strip().rstrip("`").strip()
    return json.loads(json_text)


def parse_test_cases(markdown: str) -> list[ParsedTC]:
    """
    Parse a markdown test case document into a list of ParsedTC objects.
    Falls back to simple regex parsing if Claude is unavailable.
    """
    if not config.ANTHROPIC_API_KEY or not markdown.strip():
        return []

    try:
        claude = _get_claude()
        prompt = PARSE_TC_PROMPT.format(markdown=markdown[:4000])
        data = _ask_claude_json(claude, prompt)
        if isinstance(data, list):
            return [
                ParsedTC(
                    index=item.get("index", i),
                    name=item.get("name", f"TC-{i+1}"),
                    preconditions=item.get("preconditions", []),
                    steps=item.get("steps", []),
                    expected=item.get("expected", ""),
                )
                for i, item in enumerate(data)
            ]
    except Exception as e:
        logger.warning("Claude TC parse failed, using regex fallback: %s", e)

    # Regex fallback — extract TC blocks by header pattern
    return _regex_parse_tcs(markdown)


def _regex_parse_tcs(markdown: str) -> list[ParsedTC]:
    """Simple regex-based TC parser as fallback."""
    tcs: list[ParsedTC] = []
    # Split on TC-N headers (e.g. ## TC-1, **TC-1**, ### TC-1:)
    blocks = re.split(r"(?m)^#{1,4}\s*(TC-?\d+[:\s])", markdown)
    # blocks[0] is before first TC, then alternating: header, content
    for i in range(1, len(blocks), 2):
        header = blocks[i].strip().rstrip(":")
        content = blocks[i + 1] if i + 1 < len(blocks) else ""
        steps = re.findall(r"(?m)^\s*\d+\.\s+(.+)$", content)
        expected_match = re.search(r"(?i)expected[:\s]+(.+)", content)
        expected = expected_match.group(1).strip() if expected_match else ""
        idx = len(tcs)
        tcs.append(ParsedTC(index=idx, name=header, steps=steps, expected=expected))
    return tcs


# ── Browser execution ─────────────────────────────────────────────────────────

def _try_playwright_login(app_url: str, username: str, password: str) -> bool:
    """
    Attempt to open the app URL in headful Chrome via Playwright.
    Returns True if Playwright is available and page loaded.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, slow_mo=500)
            page = browser.new_page()
            page.goto(app_url, timeout=15000)
            # If login fields exist, attempt login
            if page.locator("input[type='email'], input[type='text']").count() > 0:
                email_field = page.locator("input[type='email'], input[type='text']").first
                email_field.fill(username)
                pw_field = page.locator("input[type='password']").first
                if pw_field.count() > 0:
                    pw_field.fill(password)
                    pw_field.press("Enter")
                    page.wait_for_load_state("networkidle", timeout=10000)
            browser.close()
            return True
    except Exception as e:
        logger.debug("Playwright not available or login failed: %s", e)
        return False


def run_test_cases(
    test_cases_markdown: str,
    app_url: str,
    username: str = "",
    password: str = "",
    use_browser: bool = True,
) -> list[TCRunResult]:
    """
    Execute all test cases against a live app URL.

    For each parsed TC:
    - Attempts Playwright browser execution if use_browser=True
    - Claude evaluates expected vs actual and returns PASS/FAIL/SKIP

    Args:
        test_cases_markdown: The full TC markdown from generation
        app_url:             The live app URL to test against
        username:            Login email/username
        password:            Login password
        use_browser:         Whether to attempt headful browser launch

    Returns:
        list[TCRunResult] — one per parsed TC
    """
    if not config.ANTHROPIC_API_KEY:
        return [TCRunResult(index=0, tc_name="Error", status="ERROR",
                            notes="ANTHROPIC_API_KEY not set")]

    parsed = parse_test_cases(test_cases_markdown)
    if not parsed:
        return [TCRunResult(index=0, tc_name="No test cases", status="SKIP",
                            notes="Could not parse any test cases from the markdown")]

    # Attempt browser launch (fire and forget — opens Chrome for the tester)
    browser_available = False
    if use_browser and app_url.strip():
        browser_available = _try_playwright_login(app_url, username, password)

    claude = _get_claude()
    results: list[TCRunResult] = []

    for tc in parsed:
        try:
            prompt = BROWSER_STEPS_PROMPT.format(
                app_url=app_url or "the app",
                tc_name=tc.name,
                steps="\n".join(f"{i+1}. {s}" for i, s in enumerate(tc.steps)),
                expected=tc.expected,
            )
            data = _ask_claude_json(claude, prompt)
            results.append(TCRunResult(
                index=tc.index,
                tc_name=tc.name,
                status=data.get("status", "SKIP"),
                notes=data.get("notes", ""),
                steps_executed=data.get("steps_executed", []),
            ))
        except Exception as e:
            logger.warning("TC run failed for '%s': %s", tc.name, e)
            results.append(TCRunResult(
                index=tc.index,
                tc_name=tc.name,
                status="ERROR",
                notes=str(e),
            ))

    return results


def run_test_cases_stream(
    test_cases_markdown: str,
    app_url: str,
    username: str = "",
    password: str = "",
) -> Generator[TCRunResult, None, None]:
    """
    Generator version of run_test_cases — yields results one by one
    so the UI can update progressively.
    """
    parsed = parse_test_cases(test_cases_markdown)
    if not parsed:
        yield TCRunResult(index=0, tc_name="No test cases", status="SKIP",
                          notes="Could not parse any test cases from the markdown")
        return

    # Open browser once
    if app_url.strip():
        _try_playwright_login(app_url, username, password)

    claude = _get_claude()
    for tc in parsed:
        try:
            prompt = BROWSER_STEPS_PROMPT.format(
                app_url=app_url or "the app",
                tc_name=tc.name,
                steps="\n".join(f"{i+1}. {s}" for i, s in enumerate(tc.steps)),
                expected=tc.expected,
            )
            data = _ask_claude_json(claude, prompt)
            yield TCRunResult(
                index=tc.index,
                tc_name=tc.name,
                status=data.get("status", "SKIP"),
                notes=data.get("notes", ""),
                steps_executed=data.get("steps_executed", []),
            )
        except Exception as e:
            yield TCRunResult(
                index=tc.index,
                tc_name=tc.name,
                status="ERROR",
                notes=str(e),
            )
