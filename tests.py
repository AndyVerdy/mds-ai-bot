"""
MDS AI Bot — Test Suite.
Tests ingestion pipeline, query logic, and web API.

Usage:
    python tests.py              Run all tests
    python tests.py -v           Verbose output
"""

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

import config
from ingest import (
    extract_speaker_name,
    make_context_header,
    is_otter_transcript,
    load_document,
    chunk_documents,
)


class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def ok(self, name):
        self.passed += 1
        print(f"  PASS  {name}")

    def fail(self, name, detail=""):
        self.failed += 1
        self.errors.append((name, detail))
        print(f"  FAIL  {name}")
        if detail:
            print(f"        {detail}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*50}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print("\nFailures:")
            for name, detail in self.errors:
                print(f"  - {name}: {detail}")
        print(f"{'='*50}")
        return self.failed == 0


result = TestResult()


# ============================================================
# 1. Speaker name extraction
# ============================================================
print("\n--- Speaker Name Extraction ---")

speaker_cases = [
    ("1. Josh Hadley_otter_ai.txt", "Josh Hadley"),
    ("11. Ozlem Sengul_otter_ai.txt", "Ozlem Sengul"),
    ("Alar Huul_otter_ai.txt", "Alar Huul"),
    ("Matt Dreier_otter_ai.txt", "Matt Dreier"),
    ("3. Brian Kelsey_otter_ai.txt", "Brian Kelsey"),
    ("Călin Domuța_otter_ai.txt", "Călin Domuța"),
    ("report.pdf", "report"),
    ("Q1 Sales Deck.pptx", "Q1 Sales Deck"),
]

for filename, expected in speaker_cases:
    actual = extract_speaker_name(filename)
    if actual == expected:
        result.ok(f"extract_speaker_name('{filename}') == '{expected}'")
    else:
        result.fail(
            f"extract_speaker_name('{filename}')",
            f"Expected '{expected}', got '{actual}'",
        )


# ============================================================
# 2. Context header generation
# ============================================================
print("\n--- Context Header ---")

meta1 = {"speaker": "Josh Hadley", "source": "/data/1. Josh Hadley_otter_ai.txt", "type": "transcript", "timestamp_start": "0:05"}
header1 = make_context_header(meta1)
if "Josh Hadley" in header1 and "transcript" in header1 and "0:05" in header1:
    result.ok("Context header includes speaker, type, timestamp")
else:
    result.fail("Context header content", f"Got: {header1}")

meta2 = {"source": "slides.pptx", "type": "presentation", "slide": 3}
header2 = make_context_header(meta2)
if "Slide: 3" in header2 and "presentation" in header2:
    result.ok("Context header includes slide number")
else:
    result.fail("Context header slide", f"Got: {header2}")


# ============================================================
# 3. Otter.ai transcript detection
# ============================================================
print("\n--- Otter Transcript Detection ---")

# Create temp files for testing
with tempfile.NamedTemporaryFile(mode="w", suffix="_otter_ai.txt", delete=False) as f:
    f.write("Unknown Speaker  0:05\nHello everyone\n\nUnknown Speaker  0:10\nWelcome")
    otter_file = f.name

with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
    f.write("This is a regular text file with no speaker timestamps.")
    plain_file = f.name

try:
    if is_otter_transcript(otter_file):
        result.ok("Detects Otter.ai transcript by filename")
    else:
        result.fail("Otter detection by filename")

    if not is_otter_transcript(plain_file):
        result.ok("Plain text not detected as Otter transcript")
    else:
        result.fail("False positive on plain text")
finally:
    os.unlink(otter_file)
    os.unlink(plain_file)


# ============================================================
# 4. Document loading and chunking
# ============================================================
print("\n--- Document Loading ---")

# Create a fake Otter transcript
with tempfile.NamedTemporaryFile(
    mode="w", suffix="_otter_ai.txt", prefix="Test Speaker_", delete=False, dir="/tmp"
) as f:
    # Write enough content to generate multiple chunks
    lines = []
    for i in range(50):
        ts = f"{i // 60}:{i % 60:02d}"
        lines.append(f"Unknown Speaker  {ts}")
        lines.append(f"This is segment number {i} with some content about Amazon selling strategies and business growth. " * 3)
        lines.append("")
    f.write("\n".join(lines))
    test_transcript = f.name

try:
    docs = load_document(test_transcript)
    if len(docs) > 0:
        result.ok(f"Loaded Otter transcript: {len(docs)} chunks")
    else:
        result.fail("No chunks from Otter transcript")

    # Check metadata
    if docs and docs[0].metadata.get("speaker"):
        result.ok(f"Speaker metadata present: '{docs[0].metadata['speaker']}'")
    else:
        result.fail("Missing speaker metadata")

    # Check context header in content
    if docs and docs[0].page_content.startswith("["):
        result.ok("Context header prepended to chunk content")
    else:
        result.fail("No context header in chunk content")

    # Check type
    if docs and docs[0].metadata.get("type") == "transcript":
        result.ok("Document type is 'transcript'")
    else:
        result.fail("Wrong document type")

finally:
    os.unlink(test_transcript)


# ============================================================
# 5. Chunk size validation
# ============================================================
print("\n--- Chunk Size ---")

if config.CHUNK_SIZE >= 1500:
    result.ok(f"Chunk size is {config.CHUNK_SIZE} (>= 1500 for conversational context)")
else:
    result.fail(f"Chunk size {config.CHUNK_SIZE} may be too small for transcripts")

if config.CHUNK_OVERLAP >= 200:
    result.ok(f"Chunk overlap is {config.CHUNK_OVERLAP}")
else:
    result.fail(f"Chunk overlap {config.CHUNK_OVERLAP} may be too small")


# ============================================================
# 6. Knowledge base integration (if vectorstore exists)
# ============================================================
print("\n--- Knowledge Base ---")

if config.VECTORSTORE_DIR.exists():
    from langchain_community.vectorstores import Chroma

    vectorstore = Chroma(
        collection_name=config.COLLECTION_NAME,
        persist_directory=str(config.VECTORSTORE_DIR),
    )
    collection = vectorstore._collection
    count = collection.count()

    if count > 0:
        result.ok(f"Vectorstore has {count} chunks")
    else:
        result.fail("Vectorstore is empty")

    # Check that chunks have speaker metadata
    sample = collection.get(limit=5, include=["metadatas"])
    speakers_found = sum(1 for m in sample["metadatas"] if m.get("speaker"))
    if speakers_found > 0:
        result.ok(f"Speaker metadata present in {speakers_found}/5 sampled chunks")
    else:
        result.fail("No speaker metadata in vectorstore chunks")

    # Check unique speakers
    all_meta = collection.get(include=["metadatas"])
    speakers = set(m.get("speaker", "") for m in all_meta["metadatas"])
    speakers.discard("")
    if len(speakers) >= 10:
        result.ok(f"Found {len(speakers)} unique speakers: {', '.join(sorted(speakers)[:5])}...")
    else:
        result.fail(f"Only {len(speakers)} speakers found")

else:
    result.fail("Vectorstore not found — run 'python bot.py ingest data/' first")


# ============================================================
# 7. Query accuracy tests (requires vectorstore + API key)
# ============================================================
print("\n--- Query Accuracy ---")

if config.VECTORSTORE_DIR.exists() and config.ANTHROPIC_API_KEY:
    from query import ask

    # Test 1: Speaker-specific query
    r = ask("Who is Scott Deetz?")
    if r["confidence"] > 0 and r["chunks_used"] > 0:
        has_scott = any("scott" in str(s.get("source", "")).lower() or "scott" in str(s.get("speaker", "")).lower() for s in r["sources"])
        if has_scott:
            result.ok("Speaker query retrieves correct source (Scott Deetz)")
        else:
            result.fail("Speaker query didn't find Scott Deetz's transcript")
    else:
        result.fail(f"Speaker query returned no results (confidence={r['confidence']})")

    # Test 2: Broad topic query
    r2 = ask("What strategies were discussed for Amazon sellers?")
    if r2["confidence"] > 0 and r2["chunks_used"] >= 3:
        result.ok(f"Broad query returned {r2['chunks_used']} chunks, confidence={r2['confidence']:.2f}")
    else:
        result.fail(f"Broad query underperformed: chunks={r2['chunks_used']}, confidence={r2['confidence']:.2f}")

    # Test 3: No-data query should be handled gracefully
    r3 = ask("What is the capital of France?")
    if r3["confidence"] < 0.3:
        result.ok("Irrelevant query has low confidence")
    else:
        result.fail(f"Irrelevant query had high confidence: {r3['confidence']:.2f}")

else:
    print("  SKIP  Query tests (need vectorstore + ANTHROPIC_API_KEY)")


# ============================================================
# 8. Web API tests
# ============================================================
print("\n--- Web API ---")

try:
    from web import app

    client = app.test_client()

    # Health check
    resp = client.get("/api/health")
    if resp.status_code == 200:
        data = resp.get_json()
        if data.get("status") == "ok":
            result.ok("Health endpoint returns ok")
        else:
            result.fail("Health endpoint wrong body")
    else:
        result.fail(f"Health endpoint returned {resp.status_code}")

    # Index page
    resp = client.get("/")
    if resp.status_code == 200 and b"MDS Knowledge Assistant" in resp.data:
        result.ok("Index page loads")
    else:
        result.fail("Index page failed")

    # Ask endpoint - empty question
    resp = client.post("/api/ask", json={"question": ""})
    if resp.status_code == 400:
        result.ok("Empty question returns 400")
    else:
        result.fail(f"Empty question returned {resp.status_code}")

    # Ask endpoint - valid question (only if vectorstore exists)
    if config.VECTORSTORE_DIR.exists() and config.ANTHROPIC_API_KEY:
        resp = client.post("/api/ask", json={"question": "What topics were discussed?"})
        if resp.status_code == 200:
            data = resp.get_json()
            if "answer" in data and "confidence" in data:
                result.ok("Ask API returns answer and confidence")
            else:
                result.fail(f"Ask API missing fields: {list(data.keys())}")
        else:
            result.fail(f"Ask API returned {resp.status_code}")

except Exception as e:
    result.fail(f"Web API tests: {e}")


# ============================================================
# Summary
# ============================================================
success = result.summary()
sys.exit(0 if success else 1)
