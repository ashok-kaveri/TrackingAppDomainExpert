import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

BASE_DIR = Path(__file__).parent

# Anthropic / Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Primary model — deep reasoning, code gen, visual exploration
CLAUDE_SONNET_MODEL = os.getenv("CLAUDE_SONNET_MODEL", "claude-sonnet-4-6")
# Fast/cheap model — card processing, feature detection, lightweight tasks
CLAUDE_HAIKU_MODEL = os.getenv("CLAUDE_HAIKU_MODEL", "claude-haiku-4-5-20251001")
# Default model used by the domain expert chat
DOMAIN_EXPERT_MODEL = os.getenv("DOMAIN_EXPERT_MODEL", CLAUDE_SONNET_MODEL)

# Ollama — kept ONLY for embeddings (Anthropic has no embedding model)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")

# ChromaDB
CHROMA_PATH = str(BASE_DIR / "data" / "chroma_db")
CHROMA_COLLECTION = "tracking_knowledge"
# Separate collection for source code (backend + frontend)
CHROMA_CODE_COLLECTION = "tracking_code_knowledge"

# Source code paths (set via .env or indexed via the dashboard)
BACKEND_CODE_PATH  = os.getenv("BACKEND_CODE_PATH", "")
FRONTEND_CODE_PATH = os.getenv("FRONTEND_CODE_PATH", "")

# File extensions to index from source code directories
CODE_FILE_EXTENSIONS = [".ts", ".tsx", ".js", ".jsx", ".php", ".java", ".py", ".go", ".rb", ".cs"]

# Knowledge sources — Tracking App
PLUGINHIVE_BASE_URL = "https://www.pluginhive.com/product/shopify-shipment-tracking-notifications-app/"

# Guaranteed seed URLs — always scraped first.
# These are the primary knowledge base pages for the Tracking App.
PLUGINHIVE_SEED_URLS: list[str] = [
    # Product page
    "https://www.pluginhive.com/product/shopify-shipment-tracking-notifications-app/",
    # Official setup guide
    "https://www.pluginhive.com/knowledge-base/set-up-shopify-shipment-tracking-notify-app/",
    # Order tracking statuses explained
    "https://www.pluginhive.com/shopify-order-tracking-statuses-explained/",
]

SHOPIFY_APP_STORE_URL = "https://apps.shopify.com/shipment-tracking-notify"

# Automation codebase path
AUTOMATION_CODEBASE_PATH = os.getenv(
    "AUTOMATION_CODEBASE_PATH",
    str(BASE_DIR.parent / "tracking-test-automation"),
)

# Shopify Actions — CLI tool for bulk order creation via Shopify Admin API
SHOPIFY_ACTIONS_PATH = os.getenv(
    "SHOPIFY_ACTIONS_PATH",
    str(Path.home() / "Documents" / "shopify-actions"),
)

# Internal Tracking app wiki (markdown knowledge base)
WIKI_PATH = os.getenv("WIKI_PATH", "")

# PDF test cases
PDF_TEST_CASES_PATH = os.getenv(
    "PDF_TEST_CASES_PATH",
    str(Path.home() / "Downloads" / "TrackingApp Master sheet.pdf"),
)

# Google Sheets
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")
GOOGLE_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH", str(BASE_DIR / "credentials.json")
)

# RAG settings
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
PLUGINHIVE_MAX_PAGES = int(os.getenv("PLUGINHIVE_MAX_PAGES", "100"))
TOP_K_RESULTS = 8
MEMORY_WINDOW = 10
