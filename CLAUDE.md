# TrackingAppDomainExpert — Claude Session Context

> **Read this first in every session.** Captures all design decisions, current state, and architecture of every component.

---

## Project Overview

**TrackingAppDomainExpert** is an AI-powered knowledge base and QA platform for the PluginHive Shopify Shipment Tracking & Notifications App.

Main capability:

1. **Domain Expert Chat** — RAG-backed chatbot. Answers questions about the Tracking Shopify app from real docs, setup guides, tracking status explanations, codebase, and past approved cards.

---

## Key File Map

| File | Purpose |
|------|---------|
| `config.py` | All env-driven config: models, paths, ChromaDB, seed URLs |
| `ingest/run_ingest.py` | Master ingestion pipeline |
| `ingest/web_scraper.py` | Scrapes PluginHive tracking pages |
| `ingest/codebase_loader.py` | Indexes automation codebase into ChromaDB |
| `ingest/pdf_loader.py` | Loads PDF test cases |
| `ingest/wiki_loader.py` | Loads internal markdown wiki |
| `rag/vectorstore.py` | ChromaDB operations (tracking_knowledge collection) |
| `rag/chain.py` | Conversational RAG chain (Claude) |
| `rag/prompts.py` | Domain expert system prompt |
| `ui/chat_app.py` | Streamlit Domain Expert chat UI |
| `api/server.py` | FastAPI REST API server |
| `pipeline/handoff_docs.py` | Release Support Guide / Business Brief generation + handoff PDF renderer |
| `scripts/send_handoff_pdf_to_slack.py` | Sends a rendered handoff PDF to Slack (dry-run unless `--yes`) |
| `skills/trackingapp-handoff-docs/` | Skill: release handoff doc format, guardrails, PDF render script |

---

## Release Handoff Docs (Support Guide / Business Brief)

Generated from a Trello release lane. The PDF styling is shared with the MCSL / FedEx / AU Post
repos — `pipeline/handoff_docs.py` holds a copy of the same renderer, and only `PDF_BRAND`
("PluginHive Tracking App") plus the domain parts (app navigation, carrier detection, prompts)
differ here. Read `skills/trackingapp-handoff-docs/SKILL.md` before generating one.

```bash
# 1. Render markdown -> styled PDF
PYTHONPATH=. .venv/bin/python skills/trackingapp-handoff-docs/scripts/render_handoff_pdf.py \
  --markdown data/handoff_docs/<name>.md --title "<doc title>" --out data/handoff_docs/<name>.pdf

# 2. Deliver to Slack — dry run first, then --yes. Bare --channel = qa_members_internal
PYTHONPATH=. .venv/bin/python scripts/send_handoff_pdf_to_slack.py \
  --pdf data/handoff_docs/<name>.pdf --title "<doc title>" --channel --yes
```

Markdown and PDF both live in `data/handoff_docs/`.

---

## Knowledge Base — Seed URLs

The 3 primary knowledge sources for the Tracking App:

| URL | What it covers |
|-----|----------------|
| `https://www.pluginhive.com/product/shopify-shipment-tracking-notifications-app/` | Product overview, features, pricing, carrier list |
| `https://www.pluginhive.com/knowledge-base/set-up-shopify-shipment-tracking-notify-app/` | Full setup guide (install, configure, notifications) |
| `https://www.pluginhive.com/shopify-order-tracking-statuses-explained/` | All tracking statuses explained with definitions |

These are ingested via `ingest/web_scraper.py → scrape_pluginhive_seeds_only()`.

---

## RAG / Knowledge Base

### Collections
- `tracking_knowledge` — domain docs (PluginHive pages, wiki, test cases, codebase)
- `tracking_code_knowledge` — source code (automation + backend + frontend) — future use

### Default Ingest Sources (`ingest/run_ingest.py`)
```
pluginhive_seeds   — 3 seed URLs above (fast, always run first)
codebase           — Playwright TypeScript automation codebase
pdf                — TrackingApp Master sheet test cases PDF
wiki               — Internal markdown wiki (if WIKI_PATH is set)
```

### Partial re-ingest (fast)
```bash
PYTHONPATH=. .venv/bin/python ingest/run_ingest.py --sources pluginhive_seeds
```

### Full re-ingest
```bash
PYTHONPATH=. .venv/bin/python ingest/run_ingest.py
```

---

## Tracking App — Domain Knowledge

### App Sections
1. **Dashboard** — overview of shipments, statuses, notification stats
2. **Tracking Page** — branded tracking portal (embeddable in Shopify store)
3. **Notifications** — email & SMS templates, trigger rules, carrier-specific messages
4. **Carriers** — 900+ supported carriers; add/remove/configure tracking integrations
5. **Settings** — store config, branding, notification preferences
6. **Analytics** — delivery performance, on-time rates, exception tracking

### Tracking Statuses
| Status | Meaning |
|--------|---------|
| `Info Received` | Shipment created, not yet picked up |
| `In Transit` | Package is moving through the network |
| `Out for Delivery` | On the delivery vehicle, arriving today |
| `Delivered` | Successfully delivered |
| `Attempted Delivery` | Delivery tried but failed |
| `Exception` | Unexpected delay or issue (damage, weather, etc.) |
| `Expired` | Tracking number no longer active |
| `Pending` | Awaiting carrier scan / pickup |

### Notification Triggers
- Order shipped → notify customer with tracking link
- Out for delivery → pre-delivery alert
- Delivered → delivery confirmation
- Exception / delay → proactive delay notification
- Custom rules by carrier, status, or tag

### Branded Tracking Page
- Hosted at a custom subdomain or embedded as Shopify page
- Customisable: logo, colours, banner, estimated delivery display
- Shows full shipment timeline with carrier events

---

## Claude Models

| Purpose | Model | Config key |
|---------|-------|-----------|
| Domain Expert Chat | `claude-sonnet-4-6` | `CLAUDE_SONNET_MODEL` |
| Lightweight tasks | `claude-haiku-4-5-20251001` | `CLAUDE_HAIKU_MODEL` |

---

## Config Notes (critical — do not revert)

`config.py` uses `load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)`
NOT plain `load_dotenv()` — the explicit path is required because the app can be launched
from different working directories and `load_dotenv()` without a path fails silently.

---

## Running the Project

```bash
cd ~/Documents/Pluginhive/AILearning/TrackingAppDomainExpert

# 1. Create virtual environment (first time only)
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Install Playwright browsers (first time only)
.venv/bin/playwright install chromium

# 3. Copy and fill in .env
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY at minimum

# 4. Ingest knowledge base (first time, and when docs change)
PYTHONPATH=. .venv/bin/python ingest/run_ingest.py

# 5. Run Domain Expert Chat
PYTHONPATH=. .venv/bin/streamlit run ui/chat_app.py
# → http://localhost:8503

# 6. (Optional) Run REST API server
PYTHONPATH=. .venv/bin/uvicorn api.server:app --reload --port 8000
```

---

## ChromaDB Collection Names

| Collection | Purpose |
|-----------|---------|
| `tracking_knowledge` | Main domain docs collection |
| `tracking_code_knowledge` | Source code (future use) |

These differ from FedExDomainExpert (`fedex_knowledge`) — both projects can coexist
on the same machine without collection conflicts.

---

## Known Patterns (inherited from FedExDomainExpert)

1. **ChromaDB HNSW config** — `hnsw:space=cosine`, `M=16`, `construction_ef=100` set on collection creation. Required to prevent Python 3.14 + chromadb huge `link_lists.bin` bug.
2. **Deduplication** — `_deduplicate()` in `vectorstore.py` removes chunks with identical first 200 chars before insert.
3. **Batch size 500** — `_CHROMA_BATCH_SIZE = 500` prevents HNSW pre-allocation overflow.
4. **Labeled context** — `_build_labeled_context()` in `chain.py` buckets docs by `source_type` and adds section headers so Claude can accurately cite sources.
5. **Condense + QA** — two-step retrieval: condense follow-up → standalone question → retrieve → answer.
