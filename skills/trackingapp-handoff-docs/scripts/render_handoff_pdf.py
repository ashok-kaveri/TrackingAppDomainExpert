#!/usr/bin/env python3
"""Render handoff markdown to the shared PluginHive handoff PDF style."""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "handoff_doc"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a Tracking App handoff markdown file to PDF.",
    )
    parser.add_argument("--markdown", required=True, help="Markdown input path.")
    parser.add_argument("--title", default="", help="PDF title.")
    parser.add_argument("--out", default="", help="Output PDF path.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    import sys

    sys.path.insert(0, str(repo_root))
    md_path = Path(args.markdown)
    markdown = md_path.read_text(encoding="utf-8")
    title = args.title or md_path.stem.replace("_", " ")

    from pipeline.handoff_docs import render_pdf_bytes

    pdf_bytes = render_pdf_bytes(title, markdown)

    if args.out:
        out_path = Path(args.out)
    else:
        out_dir = repo_root / "data" / "handoff_docs"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{_safe_name(md_path.stem)}.pdf"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(pdf_bytes)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
