"""
Test Runner  —  Stage 6 of the Delivery Pipeline
==================================================
Runs `npx playwright test` scoped to the spec files generated for a
release, captures results, and returns a structured TestRunResult.

Strategy:
  • If spec files are given   → run only those files (scoped run)
  • If a release tag is given → run tests tagged with that release
  • Fallback                  → run full suite

Output is parsed from Playwright's JSON reporter.

Usage:
    from pipeline.test_runner import run_tests, RunConfig
    result = run_tests(RunConfig(
        release="TrackingApp 2.3.115",
        spec_files=["tests/notifications/emailTemplate.spec.ts"],
    ))
    # result.passed, result.failed, result.failed_tests …
"""
from __future__ import annotations
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
from pipeline.slack_client import TestRunResult

logger = logging.getLogger(__name__)

CODEBASE = Path(config.AUTOMATION_CODEBASE_PATH)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    release: str = ""
    spec_files: list[str] = field(default_factory=list)   # relative to codebase root
    grep: str = ""         # --grep pattern (e.g. "@smoke")
    headed: bool = False
    timeout_ms: int = 120_000   # per-test timeout
    workers: int = 1            # always serial for Tracking App tests
    project: str = ""           # playwright project name if using multiple projects


# ---------------------------------------------------------------------------
# Parser — reads Playwright JSON reporter output
# ---------------------------------------------------------------------------

def _parse_json_report(report_path: str) -> dict:
    """
    Parse Playwright JSON reporter output into a structured summary.
    Returns {"passed": N, "failed": N, "skipped": N, "duration": float,
             "failed_tests": [...], "failed_specs": [...]}
    """
    try:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    except Exception as e:
        logger.warning("Could not read JSON report: %s", e)
        return {"passed": 0, "failed": 0, "skipped": 0, "duration": 0.0,
                "failed_tests": [], "failed_specs": []}

    passed = failed = skipped = 0
    failed_tests: list[str] = []
    failed_specs: list[str] = []
    duration = report.get("stats", {}).get("duration", 0) / 1000  # ms → secs

    for suite in report.get("suites", []):
        _walk_suite(suite, failed_tests, failed_specs)

    # Count from stats block if available
    stats = report.get("stats", {})
    passed  = stats.get("expected", 0)
    failed  = stats.get("unexpected", 0)
    skipped = stats.get("skipped", 0)

    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "duration": duration,
        "failed_tests": list(dict.fromkeys(failed_tests)),
        "failed_specs": list(dict.fromkeys(failed_specs)),
    }


def _walk_suite(suite: dict, failed_tests: list, failed_specs: list) -> None:
    """Recursively walk Playwright JSON report suites to find failures."""
    spec_file = suite.get("file", "")
    for spec in suite.get("specs", []):
        for test in spec.get("tests", []):
            results = test.get("results", [])
            status = test.get("status", "")
            # Playwright marks unexpected results (failures) vs expected (passes)
            has_failure = any(r.get("status") not in ("passed", "skipped", "expected") for r in results)
            if has_failure or status == "unexpected":
                title = spec.get("title", test.get("title", "Unknown test"))
                failed_tests.append(f"{spec_file} › {title}")
                if spec_file and spec_file not in failed_specs:
                    failed_specs.append(spec_file)
    for child in suite.get("suites", []):
        _walk_suite(child, failed_tests, failed_specs)


def _parse_stdout(stdout: str) -> dict:
    """
    Fallback parser: extract counts from Playwright's terminal output.
    Used when JSON report is not available.

    Example line: "5 passed, 2 failed, 1 skipped (8.3s)"
    """
    result = {"passed": 0, "failed": 0, "skipped": 0, "duration": 0.0,
              "failed_tests": [], "failed_specs": []}

    # e.g. "  5 passed (8s)" or "2 failed" or "1 skipped"
    m = re.search(r"(\d+)\s+passed", stdout)
    if m:
        result["passed"] = int(m.group(1))

    m = re.search(r"(\d+)\s+failed", stdout)
    if m:
        result["failed"] = int(m.group(1))

    m = re.search(r"(\d+)\s+skipped", stdout)
    if m:
        result["skipped"] = int(m.group(1))

    m = re.search(r"\(([\d.]+)s\)", stdout)
    if m:
        result["duration"] = float(m.group(1))

    # Extract failed test titles from ✘ lines
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("✘") or "× " in stripped or "FAILED" in stripped:
            # Extract spec file and test name
            m_spec = re.search(r"(tests/\S+\.spec\.ts)", stripped)
            if m_spec:
                result["failed_specs"].append(m_spec.group(1))
            result["failed_tests"].append(stripped[:120])

    return result


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_tests(cfg: RunConfig) -> TestRunResult:
    """
    Run Playwright tests for the given config and return a TestRunResult.

    Always uses serial execution (--workers=1) — Tracking App tests share
    browser state and must not run in parallel.
    """
    if not CODEBASE.exists():
        logger.error("Automation codebase not found at: %s", CODEBASE)
        return TestRunResult(
            release=cfg.release,
            total=0, passed=0, failed=1, skipped=0,
            duration_secs=0,
            failed_tests=[f"Codebase not found: {CODEBASE}"],
        )

    start = time.time()

    with tempfile.TemporaryDirectory() as tmpdir:
        json_report = os.path.join(tmpdir, "results.json")

        # ── Build command ─────────────────────────────────────────────────
        cmd = ["npx", "playwright", "test"]

        # Scope: specific spec files take priority
        if cfg.spec_files:
            cmd += cfg.spec_files
        elif cfg.grep:
            cmd += ["--grep", cfg.grep]

        # Reporter: JSON for structured parsing + list for readable stdout
        cmd += [
            "--reporter", f"json:{json_report}",
            "--reporter", "list",
            f"--workers={cfg.workers}",
            f"--timeout={cfg.timeout_ms}",
        ]

        if cfg.headed:
            cmd.append("--headed")

        if cfg.project:
            cmd += ["--project", cfg.project]

        logger.info("Running: %s", " ".join(cmd))
        logger.info("Working dir: %s", CODEBASE)

        # ── Execute ───────────────────────────────────────────────────────
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(CODEBASE),
                capture_output=True,
                text=True,
                timeout=600,   # 10 minute hard limit
            )
            stdout = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            logger.error("Playwright test run timed out after 10 minutes")
            return TestRunResult(
                release=cfg.release,
                total=0, passed=0, failed=1, skipped=0,
                duration_secs=600,
                failed_tests=["Test run timed out after 10 minutes"],
            )
        except FileNotFoundError:
            logger.error("npx not found — ensure Node.js is installed")
            return TestRunResult(
                release=cfg.release,
                total=0, passed=0, failed=1, skipped=0,
                duration_secs=0,
                failed_tests=["npx not found — ensure Node.js is installed and `npm install` has been run"],
            )

        elapsed = time.time() - start
        logger.info("Playwright exit code: %d  (%.1fs)", proc.returncode, elapsed)

        # ── Parse results ─────────────────────────────────────────────────
        if os.path.exists(json_report):
            parsed = _parse_json_report(json_report)
        else:
            logger.warning("JSON report not written — falling back to stdout parser")
            parsed = _parse_stdout(stdout)

        total = parsed["passed"] + parsed["failed"] + parsed["skipped"]

        result = TestRunResult(
            release=cfg.release,
            total=total,
            passed=parsed["passed"],
            failed=parsed["failed"],
            skipped=parsed["skipped"],
            duration_secs=parsed.get("duration", elapsed),
            failed_tests=parsed["failed_tests"],
            failed_specs=parsed["failed_specs"],
        )

        logger.info(
            "Results: %d passed, %d failed, %d skipped",
            result.passed, result.failed, result.skipped,
        )
        return result


# ---------------------------------------------------------------------------
# Scoped run helper — used by dashboard to run only a release's specs
# ---------------------------------------------------------------------------

def run_release_tests(
    release: str,
    spec_files: list[str],
    card_results_map: dict[str, str] | None = None,
) -> TestRunResult:
    """
    Run tests for a specific Tracking App release.

    Args:
        release:          Release label e.g. "TrackingApp 2.3.115"
        spec_files:       List of spec file paths (relative to codebase)
        card_results_map: {card_name → spec_file} for per-card result mapping

    Returns TestRunResult with per-card breakdown populated.
    """
    cfg = RunConfig(release=release, spec_files=spec_files)
    result = run_tests(cfg)

    # ── Per-card breakdown ─────────────────────────────────────────────────
    if card_results_map:
        for card_name, spec in card_results_map.items():
            spec_failed = [t for t in result.failed_tests if spec in t]
            result.card_results.append({
                "card_name": card_name,
                "spec": spec,
                "passed": 0 if spec_failed else 1,   # simplistic — 0 = all failed
                "failed": len(spec_failed),
            })

    return result


# ---------------------------------------------------------------------------
# CLI — quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    specs = sys.argv[1:] if len(sys.argv) > 1 else []
    print(f"Running: {specs or 'full suite'}")
    r = run_tests(RunConfig(release="manual-run", spec_files=specs))
    print(f"\n{'✅ PASSED' if r.failed == 0 else '❌ FAILED'}")
    print(f"  {r.passed} passed  {r.failed} failed  {r.skipped} skipped  ({r.duration_secs:.1f}s)")
    if r.failed_tests:
        print("\nFailed:")
        for t in r.failed_tests:
            print(f"  • {t}")
