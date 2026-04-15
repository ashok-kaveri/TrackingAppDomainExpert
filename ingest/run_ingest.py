from __future__ import annotations
#!/usr/bin/env python3
"""
Master ingestion pipeline for TrackingAppDomainExpert.
Clears the knowledge base and rebuilds it from all configured sources.

Usage:
    python ingest/run_ingest.py                         # Ingest all sources
    python ingest/run_ingest.py --sources pluginhive_seeds
    python ingest/run_ingest.py --sources codebase wiki
"""
import argparse
import logging
import sys
import time

import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_SOURCES = ["pluginhive_seeds", "codebase", "pdf", "wiki"]
# pluginhive_seeds — 3 seed URLs: product page, setup guide, tracking statuses page
# codebase         — Playwright TypeScript automation codebase
# pdf              — TrackingApp Master sheet test cases PDF
# wiki             — Internal tracking wiki markdown knowledge base
#
# Optional sources (not in default — too slow or require extra setup):
# pluginhive       — Full BFS crawl of PluginHive tracking pages (slow)
# shopify          — Shopify App Store listing
# sheets           — Google Sheets test cases (requires credentials.json)


def run_ingest(sources: list[str] | None = None) -> None:
    from rag.vectorstore import clear_collection, add_documents
    from ingest.web_scraper import (
        scrape_pluginhive_docs,
        scrape_pluginhive_seeds_only,
        scrape_shopify_app_store,
    )
    from ingest.codebase_loader import load_codebase
    from ingest.pdf_loader import load_pdf_test_cases
    from ingest.wiki_loader import load_wiki_docs

    active_sources = sources if sources is not None else _DEFAULT_SOURCES
    start = time.time()

    print("=" * 60)
    print("TrackingApp Domain Expert — Knowledge Base Ingestion")
    print(f"Sources: {', '.join(active_sources)}")
    print("=" * 60)
    logger.info("Clearing existing knowledge base…")
    clear_collection()

    all_documents = []

    if "pluginhive" in active_sources:
        logger.info("Scraping PluginHive Tracking App docs (full BFS crawl)…")
        all_documents.extend(scrape_pluginhive_docs())

    if "pluginhive_seeds" in active_sources:
        logger.info("Scraping PluginHive seed URLs (product page, setup guide, statuses)…")
        all_documents.extend(scrape_pluginhive_seeds_only())

    if "shopify" in active_sources:
        logger.info("Scraping Shopify App Store listing…")
        all_documents.extend(scrape_shopify_app_store())

    if "codebase" in active_sources:
        logger.info("Loading automation codebase…")
        all_documents.extend(load_codebase())

    if "pdf" in active_sources:
        logger.info("Loading PDF test cases…")
        all_documents.extend(load_pdf_test_cases())

    if "wiki" in active_sources:
        logger.info("Loading internal wiki documentation…")
        all_documents.extend(load_wiki_docs())

    if "shopify_actions" in active_sources:
        logger.info("Loading Shopify Actions codebase (bulk order creation)…")
        sa_docs = load_codebase(
            path=config.SHOPIFY_ACTIONS_PATH,
            source_type="shopify_actions",
            extensions=[".js", ".json"],
            exclude_dirs=[".playground"],
        )
        all_documents.extend(sa_docs)
        logger.info("Shopify Actions: %d chunks loaded", len(sa_docs))

    if not all_documents:
        logger.error("No documents loaded. Check your sources and try again.")
        sys.exit(1)

    logger.info("Embedding and storing %d chunks in ChromaDB…", len(all_documents))
    add_documents(all_documents)

    elapsed = time.time() - start
    print(f"\n✅ Done: {len(all_documents)} chunks indexed in {elapsed:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rebuild TrackingApp Domain Expert knowledge base"
    )
    parser.add_argument(
        "--sources",
        nargs="*",
        choices=[
            "pluginhive", "pluginhive_seeds", "shopify",
            "codebase", "pdf", "wiki", "shopify_actions", "sheets",
        ],
        help="Which sources to ingest (default: pluginhive_seeds codebase pdf wiki)",
    )
    args = parser.parse_args()
    run_ingest(args.sources)
