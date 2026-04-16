"""
Automation Writer  —  Pipeline Step 5
======================================
Browser-assisted Playwright TypeScript code generation.

Flow:
  ① Find existing POM via registry + keyword matching (never create duplicates)
  ② Navigate to the real page with stored auth session → capture live elements
  ③a EXISTING POM → add new locators + methods (append only, no overwrite)
      Always create a SEPARATE new spec file for this card
  ③b NEW PAGE     → create POM with real locators + new spec + update fixtures.ts
  ④ Commit to automation/<branch> and push (never main)

Key rules enforced:
  - Import test/expect from '../../src/setup/fixtures' (not @playwright/test)
  - All POMs extend BasePage, locators are readonly class properties
  - this.appFrame for app iframe locators, this.page for Shopify admin
  - test.describe.configure({ mode: 'serial' }) on every describe block
  - Every test has at least one expect()
  - No page.waitForTimeout() > 3000ms, no test.only()
"""
from __future__ import annotations
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

import config

logger = logging.getLogger(__name__)

CODEBASE  = Path(config.AUTOMATION_CODEBASE_PATH)
SKILL_MD  = CODEBASE / "trackingSkill.md"
AUTH_JSON = CODEBASE / "auth.json"
ENV_FILE  = CODEBASE / ".env"


# ---------------------------------------------------------------------------
# POM Registry — maps feature areas to existing page objects
# ---------------------------------------------------------------------------
# Add entries here as new POMs are created.
# keywords: if any keyword appears in card_name.lower(), this POM is used.
# nav:      app navigation path for browser capture.

POM_REGISTRY: list[dict] = [
    {
        "id": "dashboardPage",
        "file": "src/pages/app/Dashboard/DashboardPage.ts",
        "class": "DashboardPage",
        "fixture": "dashboardPage",
        "keywords": [
            "dashboard", "overview", "shipment overview", "stats",
            "delivery stats", "summary",
        ],
        "nav": "Dashboard",
        "app_path": "dashboard",
    },
    {
        "id": "trackingPage",
        "file": "src/pages/app/TrackingPage/TrackingPage.ts",
        "class": "TrackingPage",
        "fixture": "trackingPage",
        "keywords": [
            "tracking page", "branded tracking", "tracking portal",
            "track shipment", "tracking link", "tracking widget",
        ],
        "nav": "Tracking Page",
        "app_path": "tracking-page",
    },
    {
        "id": "notificationsPage",
        "file": "src/pages/app/Notifications/NotificationsPage.ts",
        "class": "NotificationsPage",
        "fixture": "notificationsPage",
        "keywords": [
            "notification", "email template", "sms template", "email alert",
            "sms alert", "delivery notification", "shipment notification",
            "notify customer", "email notification", "sms notification",
        ],
        "nav": "Notifications",
        "app_path": "notifications",
    },
    {
        "id": "carriersPage",
        "file": "src/pages/app/Carriers/CarriersPage.ts",
        "class": "CarriersPage",
        "fixture": "carriersPage",
        "keywords": [
            "carrier", "carrier integration", "add carrier", "remove carrier",
            "carrier setting", "carrier config", "ups", "fedex", "dhl",
            "usps", "carrier-specific",
        ],
        "nav": "Carriers",
        "app_path": "carriers",
    },
    {
        "id": "settingsPage",
        "file": "src/pages/app/Settings/SettingsPage.ts",
        "class": "SettingsPage",
        "fixture": "settingsPage",
        "keywords": [
            "settings", "store config", "branding", "notification preference",
            "app setting", "general setting", "store setting",
        ],
        "nav": "Settings",
        "app_path": "settings",
    },
    {
        "id": "analyticsPage",
        "file": "src/pages/app/Analytics/AnalyticsPage.ts",
        "class": "AnalyticsPage",
        "fixture": "analyticsPage",
        "keywords": [
            "analytics", "delivery performance", "on-time rate", "exception tracking",
            "delivery report", "analytics dashboard", "shipment analytics",
        ],
        "nav": "Analytics",
        "app_path": "analytics",
    },
]

# Area → test folder mapping
AREA_FOLDER: dict[str, str] = {
    "dashboardPage":      "tests/dashboard",
    "trackingPage":       "tests/trackingPage",
    "notificationsPage":  "tests/notifications",
    "carriersPage":       "tests/carriers",
    "settingsPage":       "tests/settings",
    "analyticsPage":      "tests/analytics",
    "_new":               "tests/misc",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AutomationResult:
    kind: str                       # "existing_pom" | "new_pom"
    pom_file: str                   # relative path to POM file
    pom_class: str
    spec_file: str                  # new spec file path
    fixture_property: str           # pages.xxx name
    files_written: list[str] = field(default_factory=list)
    branch: str = ""
    pushed: bool = False
    push_error: str = ""
    error: str = ""
    skipped: bool = False
    browser_elements: str = ""      # raw captured elements from browser
    detection_reason: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _camel(text: str) -> str:
    words = re.sub(r"[^a-zA-Z0-9 ]", "", text).split()
    return (words[0].lower() + "".join(w.title() for w in words[1:])) if words else "feature"


def _pascal(text: str) -> str:
    return "".join(w.title() for w in re.sub(r"[^a-zA-Z0-9 ]", "", text).split()) or "Feature"


def _load_conventions() -> str:
    if SKILL_MD.exists():
        content = SKILL_MD.read_text(encoding="utf-8", errors="ignore")
        if content.startswith("---"):
            parts = content.split("---", 2)
            return parts[2].strip() if len(parts) >= 3 else content
        return content
    return ""


def _read_file(rel_path: str) -> str:
    for p in [CODEBASE / rel_path, Path(rel_path)]:
        if p.exists():
            return p.read_text(encoding="utf-8", errors="ignore")
    return ""


def _write_file(rel_path: str, content: str) -> str:
    abs_path = CODEBASE / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    return rel_path


def _get_store_url() -> str:
    """Read STORE from automation repo .env"""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("STORE="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val and not val.startswith("your-"):
                    return val
    return os.getenv("STORE", "")


# ---------------------------------------------------------------------------
# Step ①: Find existing POM
# ---------------------------------------------------------------------------

def find_pom(card_name: str) -> dict | None:
    """
    Match card name to an existing POM via keyword matching.
    Returns the registry entry or None if this is a new page.
    """
    lower = card_name.lower()
    for entry in POM_REGISTRY:
        if any(kw in lower for kw in entry["keywords"]):
            # Verify the file actually exists
            if (CODEBASE / entry["file"]).exists():
                logger.info("Matched existing POM: %s → %s", card_name, entry["file"])
                return entry
    logger.info("No existing POM matched for: %s → will create new", card_name)
    return None


# ---------------------------------------------------------------------------
# Step ②: Browser element capture
# ---------------------------------------------------------------------------

def capture_browser_elements(
    nav_description: str,
    app_path: str = "",
) -> str:
    """
    Use Python Playwright with the stored auth session to navigate to the
    relevant section and capture the accessibility tree.

    Returns a structured string describing real UI elements (buttons, inputs,
    headings, labels, checkboxes) for Claude to generate locators from.
    """
    if not AUTH_JSON.exists():
        return "auth.json not found — locators generated from test cases only."

    store_url = _get_store_url()
    if not store_url:
        return "STORE not set in .env — locators generated from test cases only."

    # Build the app URL
    app_base = f"https://{store_url}/admin/apps"
    if app_path:
        target_url = f"{app_base}/tracking-app/{app_path}"
    else:
        target_url = f"{app_base}/tracking-app"

    logger.info("Capturing browser elements from: %s", target_url)

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                storage_state=str(AUTH_JSON),
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()

            # Navigate to the section
            page.goto(target_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)  # let iframe load

            # Try to get accessibility tree from the app iframe
            iframe_locator = page.frame_locator('iframe[name="app-iframe"]')
            iframe_element = page.query_selector('iframe[name="app-iframe"]')

            elements: list[str] = []

            if iframe_element:
                frame = iframe_element.content_frame()
                if frame:
                    # Capture key elements from the iframe
                    ax_tree = frame.accessibility.snapshot(interesting_only=True)
                    if ax_tree:
                        elements.append(_format_ax_tree(ax_tree))

                    # Also capture visible text and roles for context
                    headings = frame.query_selector_all("h1, h2, h3, h4")
                    for h in headings[:10]:
                        txt = h.inner_text().strip()
                        if txt:
                            elements.append(f"heading: '{txt}'")

                    buttons = frame.query_selector_all("button:visible")
                    for b in buttons[:15]:
                        txt = (b.get_attribute("aria-label") or b.inner_text()).strip()
                        if txt:
                            elements.append(f"button: '{txt}'")

                    inputs = frame.query_selector_all("input:visible, select:visible, textarea:visible")
                    for inp in inputs[:20]:
                        name = inp.get_attribute("name") or ""
                        label = inp.get_attribute("aria-label") or inp.get_attribute("placeholder") or ""
                        input_type = inp.get_attribute("type") or "text"
                        elements.append(f"input[name='{name}'] type={input_type} label='{label}'")

                    checkboxes = frame.query_selector_all("input[type='checkbox']:visible")
                    for cb in checkboxes[:10]:
                        name = cb.get_attribute("name") or ""
                        elements.append(f"checkbox[name='{name}']")

            context.close()
            browser.close()

            if elements:
                result = f"=== Live UI elements from: {nav_description} ===\n"
                result += "\n".join(elements[:50])
                logger.info("Captured %d elements from browser", len(elements))
                return result
            return f"Page loaded but no elements captured for: {nav_description}"

    except Exception as e:
        logger.warning("Browser capture failed: %s", e)
        return f"Browser capture unavailable ({e}) — locators generated from test cases."


def _format_ax_tree(node: dict, depth: int = 0, lines: list | None = None) -> str:
    """Recursively flatten accessibility tree to readable lines."""
    if lines is None:
        lines = []
    if depth > 4 or len(lines) > 60:
        return "\n".join(lines)
    role = node.get("role", "")
    name = node.get("name", "")
    if role and name and role not in ("generic", "none", "presentation"):
        lines.append(f"{'  ' * depth}{role}: '{name}'")
    for child in node.get("children", []):
        _format_ax_tree(child, depth + 1, lines)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ADD_TO_EXISTING_POM_PROMPT = dedent("""\
    You are a senior Playwright TypeScript automation engineer for the Tracking App (PluginHive Shopify Shipment Tracking & Notifications App).

    ## Project Conventions
    {conventions}

    ## Task: ADD new locators and methods to an EXISTING page object.
    DO NOT rewrite or remove existing code. ONLY append new readonly locators
    in the constructor and new action methods after the existing ones.

    Feature Card: {card_name}
    Test Cases (positive scenarios to automate):
    {test_cases}

    ## Live UI Elements Captured from Browser
    (Use these to generate accurate locators. Match names/roles exactly.)
    {browser_elements}

    ## Domain Expert: What Already Exists in the Codebase
    (From RAG — use these existing methods/locators directly instead of re-creating them)
    {rag_context}

    ## Existing POM file ({pom_file}):
    {existing_pom}

    Return the COMPLETE updated file — existing code intact + new additions appended.
    Start with: === UPDATED POM: {pom_file} ===
    Output ONLY TypeScript — no markdown fences, no explanation text, no tables.

    Rules for new locators:
    - Add as readonly properties at the END of the existing property list
    - Initialize in constructor AFTER existing initializations
    - Use this.appFrame.getByRole(...) or this.appFrame.getByLabel(...) where possible
    - Use this.appFrame.locator('[name="..."]') for inputs with known names
    - Group new locators with a comment: // --- {card_name} ---
    - Add new action methods AFTER existing methods, also with the comment group
""")

NEW_SPEC_PROMPT = dedent("""\
    You are a senior Playwright TypeScript automation engineer for the Tracking App (PluginHive Shopify Shipment Tracking & Notifications App).

    ## Project Conventions
    {conventions}

    ## Task: Create a NEW spec file for a specific feature card.
    The page object is shown in full below — only call methods that ACTUALLY exist in it.

    Feature Card: {card_name}
    Test Cases (positive scenarios to automate):
    {test_cases}

    Page Object class: {pom_class}
    Fixture property:  pages.{fixture}  (already registered — do NOT touch fixtures.ts)

    Spec file path: {spec_path}

    ## COMPLETE PAGE OBJECT FILE (the ONLY methods you may call)
    ⚠️ DO NOT invent methods. If a test case needs a method that is not in the POM below,
    either use the closest real method OR skip that test case with test.skip().
    {pom_content}

    ## Live UI Elements (for context)
    {browser_elements}

    ## Domain Expert Context
    {rag_context}

    Generate the complete spec file.
    Start with: === SPEC FILE: {spec_path} ===
    Output ONLY TypeScript — NO markdown tables, NO explanation text, NO "Design Decisions".

    Rules:
    - import {{ test, expect }} from '{fixtures_import}'
    - import ShopifyOrderUploader from '../../src/helpers/createOrder'  (adjust depth)
    - test.describe.configure({{ mode: 'serial' }})
    - Use pages.{fixture} to call methods from the page object
    - ONLY call methods that exist in the POM above — never invent method names
    - If a test case requires backend mocking or API simulation, use test.skip()
    - Every test must have at least one expect()
    - No test.only(), no waitForTimeout() > 3000
    - Use descriptive test names matching the test case scenarios
    - Add tag: {{ tag: '@smoke' }} to the describe block

    ## Critical API contracts — NEVER deviate from these:

    ### STORE declaration — always use this exact pattern at the top of the file:
    ```
    const store = process.env.STORE;
    if (!store) {{
      throw new Error('STORE environment variable is required');
    }}
    ```
    ⚠️ Do NOT use `process.env.STORE!` (non-null assertion) — use the null-check pattern above.

    ### Order creation — ALWAYS a dedicated test() block, NEVER test.beforeAll():
    ```
    test('Create an order from API', async () => {{
      orderUploader = new ShopifyOrderUploader();
      const orderID = await orderUploader.uploadOrder();
      if (!orderID) {{
        throw new Error('Failed to create Shopify order');
      }}
      sharedOrderID = orderID;
      console.log(`Order created: ${{sharedOrderID}}`);
      expect(orderID).toBeTruthy();
    }});
    ```
    ⚠️ NEVER put order creation or expect() inside test.beforeAll(). beforeAll() must only
       contain setup that does NOT create orders (e.g., page navigation, configuration).
       If no beforeAll is needed, omit it entirely.

    ### Other contracts:
    - ShopifyOrderUploader constructor takes NO arguments: new ShopifyOrderUploader()
    - uploadOrder() → Promise<string | undefined>: check with if (!orderID) throw
    - uploadOrder('CA') for Canada/international orders
    - ShopifyOrderUploader reads STORE from process.env automatically — do NOT pass store
    - pages.shopifyAdmin.navigateToStore(store) to navigate
    - pages.shopifyAdmin.searchAndOpenOrder(orderID) to open an order
""")

NEW_POM_PROMPT = dedent("""\
    You are a senior Playwright TypeScript automation engineer for the Tracking App (PluginHive Shopify Shipment Tracking & Notifications App).

    ## Project Conventions
    {conventions}

    ## Task: Create a BRAND NEW page object for a page that doesn't exist yet.

    Feature Card: {card_name}
    POM file path: {pom_path}
    Class name: {class_name}

    ## Live UI Elements Captured from Browser
    (Use EXACTLY these element names/roles for locators — don't invent.)
    {browser_elements}

    ## Domain Expert Context (existing POM methods and patterns to follow)
    {rag_context}

    ## Existing POM for style reference:
    {pom_sample}

    Generate the complete POM file.
    Start with: === NEW POM: {pom_path} ===

    Rules:
    - import {{ Page, Locator }} from '@playwright/test'
    - import {{ BasePage }} from '../../basePage' (adjust relative path as needed)
    - export class {class_name} extends BasePage
    - All locators as readonly properties, initialized in constructor
    - this.appFrame.getByRole / getByLabel / getByText / locator for iframe elements
    - Add action methods for each interaction the tests will need
""")

FIXTURES_UPDATE_PROMPT = dedent("""\
    Add a new page object to the fixtures.ts file.

    New class: {class_name}
    Import from: {import_path}
    Pages type property: {property_name}: {class_name}
    Instantiated as: {property_name}: new {class_name}(page)

    Current fixtures.ts:
    {fixtures_content}

    Return the COMPLETE updated file.
    Start with: === UPDATED FILE: src/setup/fixtures.ts ===
    Then full TypeScript. No markdown fences.
""")

REVIEW_PROMPT = dedent("""\
    Review this Playwright TypeScript file for the Tracking App.
    Check:
    1. Imports test/expect from fixtures path (not @playwright/test) — for spec files
    2. All locators are readonly class properties (not inside methods)
    3. Uses this.appFrame for app iframe elements (not this.page for app content)
    4. test.describe.configure({{ mode: 'serial' }}) present — for spec files
    5. Every test has at least one expect() — for spec files
    6. No waitForTimeout > 3000
    7. No test.only()

    File: {file_path}
    {content}

    Respond JSON:
    {{"passed": true/false, "issues": [], "fixed_content": "corrected content or empty"}}
""")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args: list[str]) -> tuple[bool, str]:
    r = subprocess.run(
        ["git", "-C", str(CODEBASE)] + args,
        capture_output=True, text=True, timeout=30,
    )
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _current_branch() -> str:
    ok, out = _git(["branch", "--show-current"])
    return out.strip() if ok else "main"


def _create_branch(branch: str) -> bool:
    ok, _ = _git(["checkout", "-b", branch])
    if not ok:
        ok, _ = _git(["checkout", branch])
    return ok


def _commit(files: list[str], message: str) -> bool:
    _git(["add"] + [str(CODEBASE / f) for f in files])
    ok, out = _git(["commit", "-m", message])
    if not ok:
        logger.warning("Commit issue: %s", out)
    return ok


def _push(branch: str) -> tuple[bool, str]:
    return _git(["push", "-u", "origin", branch])


# ---------------------------------------------------------------------------
# Code review
# ---------------------------------------------------------------------------

def _review(file_path: str, content: str, claude: ChatAnthropic) -> str:
    try:
        resp = claude.invoke([HumanMessage(content=REVIEW_PROMPT.format(
            file_path=file_path, content=content[:3500]
        ))])
        raw = re.sub(r"```(?:json)?", "", resp.content).strip().rstrip("`")
        data = json.loads(raw)
        if not data.get("passed") and data.get("fixed_content"):
            logger.info("Auto-fixed review issues in %s: %s", file_path, data.get("issues"))
            return data["fixed_content"]
    except Exception as e:
        logger.debug("Review step skipped: %s", e)
    return content


# ---------------------------------------------------------------------------
# Test-case filtering — only automate what makes sense to automate
# ---------------------------------------------------------------------------

def filter_automatable_cases(test_cases_markdown: str) -> tuple[str, dict]:
    """
    Split test cases markdown by type and return only the automatable subset.

    Rules:
      - Positive  → always included  (happy path, fully automatable)
      - Edge      → included         (boundary values, still UI-driven)
      - Negative  → EXCLUDED         (require error mocking / invalid API
                                      responses — belong in manual / contract tests)

    Returns:
        filtered_markdown:  Only the Positive + Edge TC blocks joined back together
        summary:            {'total': n, 'positive': n, 'edge': n, 'negative': n, 'kept': n}
    """
    import re as _re

    blocks = _re.split(r"(?=###\s+TC-\d+)", test_cases_markdown)
    kept: list[str] = []
    counts = {"total": 0, "positive": 0, "edge": 0, "negative": 0}

    for block in blocks:
        block = block.strip()
        if not block or not _re.match(r"###\s+TC-\d+", block):
            continue
        counts["total"] += 1
        type_match = _re.search(r"\*\*Type:\*\*\s*(Positive|Negative|Edge)", block, _re.IGNORECASE)
        tc_type = type_match.group(1).capitalize() if type_match else "Positive"

        if tc_type == "Negative":
            counts["negative"] += 1
            # Skip — negative cases require error mocking, not suitable for E2E automation
        elif tc_type == "Edge":
            counts["edge"] += 1
            kept.append(block)
        else:
            counts["positive"] += 1
            kept.append(block)

    counts["kept"] = len(kept)
    filtered = "\n\n".join(kept)
    logger.info(
        "TC filter: total=%d positive=%d edge=%d negative=%d(skipped) → %d automatable",
        counts["total"], counts["positive"], counts["edge"], counts["negative"], counts["kept"],
    )
    return filtered, counts


# ---------------------------------------------------------------------------
# Self-healing: Run → Fix → Re-run loop
# ---------------------------------------------------------------------------

FIX_PROMPT = dedent("""\
    You are a senior Playwright TypeScript automation engineer.
    A generated test spec is FAILING. Fix ONLY the spec (and optionally the POM) so all tests pass.

    ## Playwright Error Output
    {error_output}

    ## Current Spec ({spec_path})
    {spec_content}

    ## Current POM ({pom_path})
    {pom_content}

    ## Project Conventions
    {conventions}

    Rules for the fix:
    - Only call methods that ACTUALLY EXIST in the POM above — never invent method names
    - If a method is missing from the POM, add it to the POM (return UPDATED POM too)
    - If the error is a selector/timeout, update the locator in the POM
    - If the error is a wrong assertion value, correct the expected value
    - Keep all passing tests intact — do NOT remove or skip tests that are passing
    - Do not add test.only()

    ## Critical API contracts — NEVER deviate:
    - ShopifyOrderUploader takes NO constructor arguments: new ShopifyOrderUploader()
    - uploadOrder() returns Promise<string | undefined> — always check: if (!orderID) throw new Error(...)
    - uploadOrder('CA') for international/Canada orders
    - NEVER pass store or any argument to new ShopifyOrderUploader()
    - STORE declaration — ALWAYS this exact pattern (no non-null assertion):
        const store = process.env.STORE;
        if (!store) {{ throw new Error('STORE environment variable is required'); }}
    - Order creation MUST be in a dedicated test() block — NEVER in test.beforeAll()
    - NEVER put expect() inside test.beforeAll()

    Return ONLY the fixed TypeScript files — NO explanations, NO markdown tables,
    NO comments outside the code, NO "Design Decisions" sections.
    Output ONLY these blocks, nothing else:

    === FIXED SPEC: {spec_path} ===
    <full fixed spec TypeScript — TypeScript only, no markdown>

    If you also need to fix the POM:
    === FIXED POM: {pom_path} ===
    <full fixed POM TypeScript — TypeScript only, no markdown>
""")


def _find_node() -> str:
    """Find node executable — tries PATH first, then common locations."""
    import shutil
    node = shutil.which("node")
    if node:
        return node
    candidates = [
        "/usr/local/bin/node",
        "/opt/homebrew/bin/node",
        str(Path.home() / ".nvm" / "versions" / "node"),
    ]
    for c in candidates:
        p = Path(c)
        if p.is_file():
            return str(p)
        # nvm version directory — find the latest
        if p.is_dir():
            versions = sorted(p.glob("*/bin/node"), reverse=True)
            if versions:
                return str(versions[0])
    return "node"  # let subprocess fail with a useful message


def _run_playwright(spec_path: str, timeout: int = 180) -> tuple[bool, str]:
    """
    Run playwright test for *spec_path* inside CODEBASE.
    Returns (all_passed, combined_stdout+stderr output).
    """
    node      = _find_node()
    pw_bin    = CODEBASE / "node_modules" / ".bin" / "playwright"
    if not pw_bin.exists():
        return False, f"playwright binary not found at {pw_bin}"

    cmd = [
        node, str(pw_bin), "test",
        spec_path,
        "--project=Google Chrome",
        "--reporter=line",
    ]
    logger.info("Running playwright: %s", " ".join(cmd))
    try:
        r = subprocess.run(
            cmd,
            cwd=str(CODEBASE),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = r.stdout + r.stderr
        passed = r.returncode == 0
        logger.info("Playwright exit=%d passed=%s", r.returncode, passed)
        return passed, output
    except subprocess.TimeoutExpired:
        return False, f"Playwright timed out after {timeout}s"
    except Exception as exc:
        return False, f"Playwright run error: {exc}"


def _extract_errors(output: str, max_chars: int = 4000) -> str:
    """Pull the most useful error lines out of playwright output."""
    lines = output.splitlines()
    error_lines: list[str] = []
    capturing = False
    for line in lines:
        if any(kw in line for kw in ("Error:", "×", "✘", "FAILED", "TimeoutError", "expect(", "at ")):
            capturing = True
        if capturing:
            error_lines.append(line)
        # Stop capturing after a blank line following errors
        if capturing and line.strip() == "":
            capturing = False
    summary = "\n".join(error_lines) if error_lines else output
    return summary[:max_chars]


def run_and_fix_loop(
    spec_path: str,
    pom_path: str,
    claude: ChatAnthropic,
    card_name: str = "",
    max_iterations: int = 3,
    on_progress=None,          # optional callback(iteration, status_str, output)
) -> dict:
    """
    Run the Playwright spec, and if it fails, use Claude to fix the code and
    re-run — up to *max_iterations* times.

    Args:
        spec_path:      Relative path to the spec file inside CODEBASE
        pom_path:       Relative path to the POM file inside CODEBASE
        claude:         ChatAnthropic instance
        card_name:      Human-readable feature name (for logging / commits)
        max_iterations: Maximum fix-and-retry cycles (default 3)
        on_progress:    Optional callback(iteration: int, status: str, output: str)

    Returns dict with keys:
        passed          bool
        iterations      int    — how many run attempts were made
        history         list[dict]  — per-iteration {iteration, passed, output, fixed_files}
        final_output    str
    """
    conventions = _load_conventions()[:2000]
    history: list[dict] = []

    for attempt in range(1, max_iterations + 1):
        status_msg = f"Run {attempt}/{max_iterations}…"
        logger.info("[fix-loop] %s for %s", status_msg, spec_path)
        if on_progress:
            on_progress(attempt, status_msg, "")

        passed, output = _run_playwright(spec_path)
        history.append({"iteration": attempt, "passed": passed,
                         "output": output[-3000:], "fixed_files": []})

        if passed:
            logger.info("[fix-loop] ✅ All tests passed on attempt %d", attempt)
            if on_progress:
                on_progress(attempt, f"✅ All tests passed (attempt {attempt})", output)
            return {"passed": True, "iterations": attempt,
                    "history": history, "final_output": output}

        if attempt == max_iterations:
            logger.warning("[fix-loop] ❌ Still failing after %d attempts", max_iterations)
            break

        # ── Ask Claude to fix the failing code ──────────────────────────────
        error_snippet = _extract_errors(output)
        spec_content  = _read_file(spec_path)
        pom_content   = _read_file(pom_path)

        if on_progress:
            on_progress(attempt, f"🔧 Fixing errors (attempt {attempt})…", error_snippet)

        fix_resp = claude.invoke([HumanMessage(content=FIX_PROMPT.format(
            error_output=error_snippet,
            spec_path=spec_path,
            spec_content=spec_content[:4000],
            pom_path=pom_path,
            pom_content=pom_content[:4000],
            conventions=conventions,
        ))])
        raw_fix = fix_resp.content.strip()

        fixed_files: list[str] = []

        # Parse fixed spec
        fixed_spec = _parse_block(raw_fix, "FIXED SPEC")
        if fixed_spec and fixed_spec != spec_content:
            fixed_spec = _review(spec_path, fixed_spec, claude)
            _write_file(spec_path, fixed_spec)
            fixed_files.append(spec_path)
            logger.info("[fix-loop] Wrote fixed spec: %s", spec_path)

        # Parse fixed POM (optional — only if Claude returned one)
        # Use non-greedy lookahead so it stops at the next === block or end-of-string
        m = re.search(r"=== FIXED POM:.+?===\n([\s\S]+?)(?=\n===\s|\Z)", raw_fix)
        if m:
            fixed_pom = m.group(1).strip()
            fixed_pom = re.sub(r"^```(?:typescript|ts)?\n?", "", fixed_pom)
            fixed_pom = re.sub(r"\n?```\s*$", "", fixed_pom)
            if fixed_pom and fixed_pom != pom_content:
                fixed_pom = _review(pom_path, fixed_pom, claude)
                _write_file(pom_path, fixed_pom)
                fixed_files.append(pom_path)
                logger.info("[fix-loop] Wrote fixed POM: %s", pom_path)

        history[-1]["fixed_files"] = fixed_files

        if fixed_files:
            _commit(
                fixed_files,
                f"test(fix): auto-fix attempt {attempt} for '{card_name or spec_path}'",
            )

    return {"passed": False, "iterations": max_iterations,
            "history": history, "final_output": history[-1]["output"]}


# ---------------------------------------------------------------------------
# Parse output blocks
# ---------------------------------------------------------------------------

def _parse_block(raw: str, marker: str) -> str:
    """
    Parse content after === MARKER: path === line.
    Stops at the next === ... === boundary so multiple blocks in one response
    don't bleed into each other (e.g. FIXED SPEC + FIXED POM in same reply).
    Also strips any markdown explanation text Claude appends after the TypeScript.
    """
    # Non-greedy capture that stops at the next === section header or end-of-string
    m = re.search(rf"=== {marker}:.+?===\n([\s\S]+?)(?=\n===\s|\Z)", raw)
    if m:
        body = m.group(1).strip()
        # Strip leading ```typescript / ```ts fence if present
        body = re.sub(r"^```(?:typescript|ts)?\n?", "", body)
        # Strip trailing ``` fence if present
        body = re.sub(r"\n?```\s*$", "", body)
        # Strip any markdown explanation Claude appends after the TypeScript code.
        # TypeScript files end with a closing brace or }); — everything after is noise.
        body = _strip_post_code_markdown(body)
        return body
    # Marker not found — return empty string so callers can guard before writing
    logger.warning("_parse_block: marker '%s' not found in Claude response — skipping write", marker)
    return ""


def _strip_post_code_markdown(code: str) -> str:
    """
    Remove markdown explanation text that Claude sometimes appends after
    valid TypeScript code (e.g. ## Design Decisions, | table |, plain prose).
    Keeps everything up to and including the last line that looks like TypeScript.
    """
    lines = code.splitlines()
    last_ts_line = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        # A line is "TypeScript" if it's not a markdown heading/table/bullet
        # and not blank after the code ends
        if stripped and not stripped.startswith(("#", "|", "---", "*", ">")):
            last_ts_line = i
    return "\n".join(lines[: last_ts_line + 1])


# ---------------------------------------------------------------------------
# Spec path helper
# ---------------------------------------------------------------------------

def _spec_path(card_name: str, pom_id: str) -> str:
    folder = AREA_FOLDER.get(pom_id, AREA_FOLDER["_new"])
    return f"{folder}/{_camel(card_name)}.spec.ts"


def _fixtures_import(spec_path: str) -> str:
    """Calculate relative import path from spec to fixtures.ts."""
    depth = spec_path.count("/")
    return "../" * depth + "src/setup/fixtures"


def _query_domain_expert(card_name: str, test_cases: str) -> tuple[str, list[str]]:
    """
    Query automation code RAG (spec files, POMs, helpers) + QA knowledge RAG
    to get existing patterns, known UI element texts, and navigation paths.

    Priority:
      1. Automation code RAG  — real spec files, POM classes, helper imports
         (source_type="automation" in tracking_code_knowledge collection)
      2. QA knowledge RAG     — fallback if automation not yet indexed
         (tracking_knowledge collection)

    Returns:
        rag_context:      Relevant existing code to guide generation
        known_ui_texts:   UI element texts already in the codebase
    """
    context_parts: list[str] = []
    full_text = ""

    # ── 1. Automation code RAG (highest value) ────────────────────────────
    try:
        from rag.code_indexer import search_code, get_index_stats
        _stats = get_index_stats()
        if _stats.get("automation", 0) > 0:
            query = f"TypeScript Playwright spec POM {card_name} test describe"
            auto_docs = search_code(query, k=5, source_type="automation")

            # Also query for helper/setup patterns
            helper_docs = search_code(
                f"fixture import helper createOrder ShopifyOrderUploader {card_name}", k=3,
                source_type="automation",
            )

            for doc in auto_docs + helper_docs:
                fpath = doc.metadata.get("file_path", "")
                context_parts.append(f"\n--- automation/{fpath} ---\n{doc.page_content}")
                full_text += doc.page_content + "\n"

            logger.info(
                "Automation code RAG: %d docs for '%s'",
                len(auto_docs) + len(helper_docs), card_name,
            )
    except Exception as e:
        logger.debug("Automation code RAG query failed (non-fatal): %s", e)

    # ── 2. QA knowledge RAG fallback ──────────────────────────────────────
    try:
        from rag.vectorstore import search
        pom_docs = search(f"page object TypeScript locators {card_name}", k=3)
        nav_docs = search(f"navigation test spec {card_name} selectAppMenu appPath", k=2)
        for doc in pom_docs + nav_docs:
            src = doc.metadata.get("source", doc.metadata.get("source_url", ""))
            context_parts.append(f"\n--- {src} ---\n{doc.page_content}")
            full_text += doc.page_content + "\n"
    except Exception as e:
        logger.debug("QA knowledge RAG query failed (non-fatal): %s", e)

    if not context_parts:
        return "", []

    header = "=== Existing Automation Code (follow these exact patterns) ==="
    rag_context = header + "\n".join(context_parts)

    # Extract actual UI text strings already used as locators in the codebase
    ui_texts: list[str] = []
    ui_texts += re.findall(r"getByRole\([^,)]+,\s*\{\s*name:\s*['\"]([^'\"]+)['\"]", full_text)
    ui_texts += re.findall(r"getByLabel\(['\"]([^'\"]+)['\"]", full_text)
    ui_texts += re.findall(r"getByText\(['\"]([^'\"]+)['\"]", full_text)
    ui_texts += re.findall(r"getByPlaceholder\(['\"]([^'\"]+)['\"]", full_text)
    known_ui_texts = list({t.lower().strip() for t in ui_texts if t.strip()})

    logger.info(
        "Domain Expert total: %d context blocks, %d known UI texts for '%s'",
        len(context_parts), len(known_ui_texts), card_name,
    )
    return rag_context, known_ui_texts


# ---------------------------------------------------------------------------
# Main flows
# ---------------------------------------------------------------------------

def _handle_existing_pom(
    card_name: str,
    test_cases: str,
    pom_entry: dict,
    browser_elements: str,
    claude: ChatAnthropic,
    dry_run: bool,
    rag_context: str = "",
    qa_context: str = "",
) -> AutomationResult:
    """
    Feature uses an existing POM → add new locators/methods + create new spec.
    """
    pom_file     = pom_entry["file"]
    pom_class    = pom_entry["class"]
    fixture_prop = pom_entry["fixture"]
    spec_path    = _spec_path(card_name, pom_entry["id"])
    conventions  = _load_conventions()[:3000]

    existing_pom = _read_file(pom_file)
    if not existing_pom:
        return AutomationResult(
            kind="existing_pom", pom_file=pom_file, pom_class=pom_class,
            spec_file=spec_path, fixture_property=fixture_prop,
            error=f"Could not read existing POM: {pom_file}",
        )

    files_written = []

    # ── Update POM: add new locators + methods ───────────────────────────
    pom_prompt = ADD_TO_EXISTING_POM_PROMPT.format(
        conventions=conventions,
        card_name=card_name,
        test_cases=test_cases,
        browser_elements=browser_elements,
        pom_file=pom_file,
        existing_pom=existing_pom[:4000],
        rag_context=rag_context[:2000],
    )
    pom_resp = claude.invoke([HumanMessage(content=pom_prompt)])
    updated_pom = _parse_block(pom_resp.content.strip(), "UPDATED POM")
    updated_pom = _review(pom_file, updated_pom, claude)

    if not dry_run and updated_pom and updated_pom != existing_pom:
        _write_file(pom_file, updated_pom)
        files_written.append(pom_file)
        logger.info("Updated POM: %s", pom_file)

    # ── Generate new spec ────────────────────────────────────────────────
    # Use the updated POM (with new methods) so spec only calls real methods
    final_pom = updated_pom if (updated_pom and updated_pom != existing_pom) else existing_pom

    qa_note = f"\n\n## QA Test Context (use these exact values in tests)\n{qa_context}" if qa_context else ""
    spec_prompt = NEW_SPEC_PROMPT.format(
        conventions=conventions,
        card_name=card_name,
        test_cases=test_cases + qa_note,
        pom_class=pom_class,
        fixture=fixture_prop,
        spec_path=spec_path,
        pom_content=final_pom[:5000],
        browser_elements=browser_elements[:500],
        fixtures_import=_fixtures_import(spec_path),
        rag_context=rag_context[:800],
    )
    spec_resp = claude.invoke([HumanMessage(content=spec_prompt)])
    spec_content = _parse_block(spec_resp.content.strip(), "SPEC FILE")
    spec_content = _review(spec_path, spec_content, claude)

    if not dry_run and spec_content:
        _write_file(spec_path, spec_content)
        files_written.append(spec_path)
        logger.info("Created spec: %s", spec_path)

    return AutomationResult(
        kind="existing_pom",
        pom_file=pom_file,
        pom_class=pom_class,
        spec_file=spec_path,
        fixture_property=fixture_prop,
        files_written=files_written,
        browser_elements=browser_elements[:300],
        detection_reason=f"Matched existing POM via keywords → {pom_file}",
        skipped=dry_run,
    )


def _handle_new_pom(
    card_name: str,
    test_cases: str,
    browser_elements: str,
    claude: ChatAnthropic,
    dry_run: bool,
    rag_context: str = "",
    qa_context: str = "",
) -> AutomationResult:
    """
    Brand-new page → generate POM + spec + update fixtures.ts.
    """
    class_name   = _pascal(card_name) + "Page"
    fixture_prop = _camel(card_name) + "Page"
    pom_file     = f"src/pages/app/{_pascal(card_name)}/{_pascal(card_name)}.ts"
    spec_path    = _spec_path(card_name, "_new")
    conventions  = _load_conventions()[:3000]
    pom_sample   = ""

    # Load one existing POM for style reference
    for entry in POM_REGISTRY:
        sample = _read_file(entry["file"])
        if sample:
            pom_sample = f"// {entry['file']}\n{sample[:800]}"
            break

    files_written = []

    # ── Generate new POM ─────────────────────────────────────────────────
    pom_prompt = NEW_POM_PROMPT.format(
        conventions=conventions,
        card_name=card_name,
        pom_path=pom_file,
        class_name=class_name,
        browser_elements=browser_elements,
        pom_sample=pom_sample,
        rag_context=rag_context[:2000],
    )
    pom_resp = claude.invoke([HumanMessage(content=pom_prompt)])
    pom_content = _parse_block(pom_resp.content.strip(), "NEW POM")
    pom_content = _review(pom_file, pom_content, claude)

    if not dry_run and pom_content:
        _write_file(pom_file, pom_content)
        files_written.append(pom_file)
        logger.info("Created POM: %s", pom_file)

    # ── Generate spec ────────────────────────────────────────────────────
    qa_note = f"\n\n## QA Test Context (use these exact values in tests)\n{qa_context}" if qa_context else ""
    spec_prompt = NEW_SPEC_PROMPT.format(
        conventions=conventions,
        card_name=card_name,
        test_cases=test_cases + qa_note,
        pom_class=class_name,
        fixture=fixture_prop,
        spec_path=spec_path,
        pom_content=pom_content[:5000],
        browser_elements=browser_elements[:500],
        fixtures_import=_fixtures_import(spec_path),
        rag_context=rag_context[:800],
    )
    spec_resp = claude.invoke([HumanMessage(content=spec_prompt)])
    spec_content = _parse_block(spec_resp.content.strip(), "SPEC FILE")
    spec_content = _review(spec_path, spec_content, claude)

    if not dry_run and spec_content:
        _write_file(spec_path, spec_content)
        files_written.append(spec_path)
        logger.info("Created spec: %s", spec_path)

    # ── Update fixtures.ts ───────────────────────────────────────────────
    if not dry_run:
        fixtures_content = _read_file("src/setup/fixtures.ts")
        if fixtures_content:
            # relative import from fixtures.ts → new POM
            import_path = f"../pages/app/{_pascal(card_name)}/{_pascal(card_name)}"
            fix_prompt = FIXTURES_UPDATE_PROMPT.format(
                class_name=class_name,
                import_path=import_path,
                property_name=fixture_prop,
                fixtures_content=fixtures_content[:4000],
            )
            fix_resp = claude.invoke([HumanMessage(content=fix_prompt)])
            updated_fix = _parse_block(fix_resp.content, "UPDATED FILE")
            if updated_fix:
                _write_file("src/setup/fixtures.ts", updated_fix)
                files_written.append("src/setup/fixtures.ts")
                logger.info("Updated fixtures.ts")

    return AutomationResult(
        kind="new_pom",
        pom_file=pom_file,
        pom_class=class_name,
        spec_file=spec_path,
        fixture_property=fixture_prop,
        files_written=files_written,
        browser_elements=browser_elements[:300],
        detection_reason="No existing POM matched — creating new page object",
        skipped=dry_run,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_automation(
    card_name: str,
    test_cases_markdown: str,
    acceptance_criteria: str = "",
    branch_name: str = "",
    dry_run: bool = False,
    push: bool = False,
    chrome_trace_context: str = "",
    qa_context: str = "",
    auto_fix: bool = False,
    fix_iterations: int = 3,
    on_fix_progress=None,
) -> dict:
    """
    Generate or update Playwright automation code for a Trello card.

    Args:
        card_name:             Feature card title
        test_cases_markdown:   Approved test cases (all types)
        acceptance_criteria:   Additional AC context
        branch_name:           Git branch (auto-generated as automation/<slug> if empty)
        dry_run:               Generate code preview without writing to disk
        push:                  Push branch to origin after commit
        chrome_trace_context:  Pre-captured UITrace context string from chrome_agent.
                               When provided, skips the internal capture_browser_elements()
                               call and uses this richer, multi-step agent trace instead.
        qa_context:            Free-text QA instructions with specific test data, e.g.
                               "Use tracking number 1234567890 for carrier UPS" — injected
                               directly into the code-gen prompt so tests use real values.
        auto_fix:              After generating code, automatically run tests and fix
                               failures using Claude (up to fix_iterations attempts).
        fix_iterations:        Maximum run→fix cycles when auto_fix=True (default 3).
        on_fix_progress:       Optional callback(iteration, status_str, output) for
                               streaming progress to the dashboard.

    Returns dict suitable for display in the Streamlit dashboard.
    """
    if not config.ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not set", "skipped": True}
    if not CODEBASE.exists():
        return {"error": f"Codebase not found: {CODEBASE}", "skipped": True}

    claude = ChatAnthropic(
        model=config.CLAUDE_SONNET_MODEL,
        api_key=config.ANTHROPIC_API_KEY,
        temperature=0.15,
        max_tokens=8192,
    )

    # ── ① Find existing POM ───────────────────────────────────────────────
    pom_entry = find_pom(card_name)

    # ── ①a Filter: keep only Positive + Edge cases for automation ────────
    filtered_cases, tc_counts = filter_automatable_cases(test_cases_markdown)
    if not filtered_cases:
        # Nothing automatable — return early with a clear message
        return {
            "kind": "skipped",
            "pom_file": "", "spec_file": "", "fixture_property": "",
            "files_written": [], "branch": "", "pushed": False, "push_error": "",
            "skipped": True,
            "error": (
                f"No automatable test cases found. "
                f"All {tc_counts['total']} cases were Negative type "
                f"(require error mocking — handled manually)."
            ),
            "tc_filter_summary": tc_counts,
            "browser_elements": "", "detection_reason": "",
            "fix_passed": None, "fix_iterations": 0,
            "fix_history": [], "fix_final_output": "",
        }

    # ── ①b Query Domain Expert RAG for existing code context ─────────────
    rag_context, _known_ui_texts = _query_domain_expert(card_name, filtered_cases)

    # ── ② Browser elements: prefer Chrome Agent trace, fall back to snapshot ─
    if chrome_trace_context:
        # Rich multi-step trace from the agentic explorer — grounded in real UI
        browser_elements = chrome_trace_context
        logger.info("Using Chrome Agent trace context for '%s' (%d chars)", card_name, len(chrome_trace_context))
    else:
        # One-shot accessibility snapshot (original behaviour)
        app_path = pom_entry["app_path"] if pom_entry else ""
        nav_desc = pom_entry["nav"] if pom_entry else card_name
        browser_elements = capture_browser_elements(nav_desc, app_path)

    # ── ③ Checkout branch ────────────────────────────────────────────────
    target_branch = branch_name or f"automation/{_slugify(card_name)[:40]}"
    if not dry_run:
        _create_branch(target_branch)

    # ── ④ Generate code (using filtered Positive+Edge cases only) ─────────
    if pom_entry:
        result = _handle_existing_pom(
            card_name, filtered_cases, pom_entry, browser_elements, claude, dry_run,
            rag_context=rag_context,
            qa_context=qa_context,
        )
    else:
        result = _handle_new_pom(
            card_name, filtered_cases, browser_elements, claude, dry_run,
            rag_context=rag_context,
            qa_context=qa_context,
        )

    result.branch = target_branch if not dry_run else ""

    # ── ⑤ Commit ─────────────────────────────────────────────────────────
    if not dry_run and result.files_written:
        verb = "update" if result.kind == "existing_pom" else "add"
        _commit(
            result.files_written,
            f"test(automation): {verb} Playwright tests for '{card_name}'\n\n"
            f"Kind: {result.kind} | Files: {len(result.files_written)}\n"
            f"Branch: {target_branch} — review before merging to main.",
        )

    # ── ⑥ Auto-fix loop: run → fix → re-run until green ─────────────────
    fix_result: dict = {}
    if not dry_run and auto_fix and result.files_written and not result.error:
        fix_result = run_and_fix_loop(
            spec_path=result.spec_file,
            pom_path=result.pom_file,
            claude=claude,
            card_name=card_name,
            max_iterations=fix_iterations,
            on_progress=on_fix_progress,
        )
        logger.info(
            "[auto-fix] %s — %d iterations, passed=%s",
            card_name, fix_result.get("iterations", 0), fix_result.get("passed"),
        )

    # ── ⑦ Push (only if requested AND tests are green or auto_fix was off) ──
    # Never push a branch where auto_fix ran but all iterations failed
    auto_fix_failed = auto_fix and fix_result and fix_result.get("passed") is False
    if not dry_run and push and result.files_written and not auto_fix_failed:
        ok, out = _push(target_branch)
        result.pushed = ok
        result.push_error = "" if ok else out
        if not ok:
            logger.warning("Push failed: %s", out)
    elif auto_fix_failed:
        result.push_error = (
            f"Push skipped — tests still failing after {fix_result.get('iterations', 0)} "
            f"auto-fix attempt(s). Fix locally and push manually."
        )
        logger.warning("[auto-fix] Push blocked — tests did not pass after fix loop")

    return {
        "kind": result.kind,
        "pom_file": result.pom_file,
        "spec_file": result.spec_file,
        "fixture_property": result.fixture_property,
        "files_written": result.files_written,
        "branch": result.branch,
        "pushed": result.pushed,
        "push_error": result.push_error,
        "error": result.error,
        "skipped": result.skipped,
        "browser_elements": result.browser_elements,
        "detection_reason": result.detection_reason,
        # TC filter breakdown
        "tc_filter_summary": tc_counts,
        # Auto-fix results (empty dict when auto_fix=False)
        "fix_passed": fix_result.get("passed"),
        "fix_iterations": fix_result.get("iterations", 0),
        "fix_history": fix_result.get("history", []),
        "fix_final_output": fix_result.get("final_output", ""),
    }
