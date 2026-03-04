import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent

load_dotenv(str(PROJECT_ROOT / ".env"), override=True)
DATA_DIR = PROJECT_ROOT / "data"
VECTORSTORE_DIR = PROJECT_ROOT / "vectorstore"

# API Keys
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Embedding — uses ChromaDB's built-in model (free, local, no API key)
# ChromaDB uses all-MiniLM-L6-v2 by default

# LLM (Claude)
LLM_MODEL = "claude-sonnet-4-20250514"
LLM_TEMPERATURE = 0.1

# Chunking
CHUNK_SIZE = 2000  # characters (larger chunks preserve conversational context better)
CHUNK_OVERLAP = 300

# Retrieval
TOP_K = 5  # number of chunks to retrieve
CONFIDENCE_THRESHOLD = 0.15  # minimum similarity score (0-1, lower = more permissive)

# Collection name for ChromaDB
COLLECTION_NAME = "mds_knowledge_base"
