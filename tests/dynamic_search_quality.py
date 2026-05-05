#!/usr/bin/env python3
"""Dynamic search-quality test suite.

Walks the local vectorstore, samples random WA + transcript chunks,
generates one specific factual question per chunk via Claude, then
hits prod /api/ask with each generated question and checks whether
the bot's answer cites the same source chunk and contains language
distinctive to that chunk.

This complements the static 17-query regression suite at
/tmp/search_test_prod.py — it surfaces NEW failure modes as the
corpus grows, rather than re-checking the same hand-picked cases.

Run locally:
    cd mds-ai-bot && source venv/bin/activate
    python3 tests/dynamic_search_quality.py
    python3 tests/dynamic_search_quality.py --n-wa 5 --n-tr 5
    python3 tests/dynamic_search_quality.py --recent-days 30 --json results.json

Environment requirements:
    ANTHROPIC_API_KEY in .env  (for question generation)
    Local vectorstore present  (for sampling)

The reviewer Bearer token is hard-coded — same one used by the static
suite. Update REVIEWER_EMAIL/REVIEWER_CODE if Andy rotates them.

Designed in CU Page 09 (Search-Quality Test Plan).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Project root on sys.path so we can import config + query
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from query import format_display_name, get_vectorstore  # noqa: E402

# ── Prod target ────────────────────────────────────────────────────────────
BASE_URL = os.getenv("BOT_BASE_URL", "https://mds-ai-bot.onrender.com")
REVIEWER_EMAIL = os.getenv("REVIEWER_EMAIL", "appstore-reviewer@mds.co")
REVIEWER_CODE = os.getenv("REVIEWER_FIXED_CODE", "837363")

# ── Generation prompt ──────────────────────────────────────────────────────
QUESTION_PROMPT = """You are a test-question generator for a RAG system.

Read the chunk below. Write ONE specific factual question that this chunk uniquely answers — meaning the question can only be answered with information distinctive to THIS chunk, not generic background.

The question must:
- Reference a specific person BY NAME (use the metadata's speaker / chat group), or a specific brand, number, or claim from the chunk
- Be answerable in 1-2 sentences from the chunk's content
- Sound how a real MDS member would actually ask (natural, not robotic)

The question must NOT:
- Use the phrase "this chunk", "the chunk", "the speaker", "the second speaker", "the participant", or any other anonymous reference — name the person or group explicitly
- Be answerable from generic background knowledge alone

If the chunk is too generic, anonymous, repetitive, or lacks specific facts to anchor a unique question, output exactly:
TESTER_BROKEN

Chunk metadata: {meta_summary}

Chunk content:
---
{chunk_content}
---

Return ONLY the question (or TESTER_BROKEN). No preamble, no quotes."""

STOP_WORDS = {
    "the", "and", "that", "this", "with", "from", "for", "have", "has",
    "are", "was", "were", "you", "your", "about", "what", "when", "where",
    "why", "how", "who", "which", "they", "them", "their", "there",
    "into", "more", "than", "but", "not", "all", "any", "can", "will",
    "would", "could", "should", "been", "being", "had", "his", "her",
    "she", "him", "them", "we", "our", "us", "i", "me", "my", "mine",
    "or", "if", "is", "it", "in", "on", "at", "to", "of", "as", "by",
    "an", "be", "do", "did", "does", "so", "say", "said", "says",
    "get", "got", "out", "up", "down", "over", "just", "really", "very",
    "well", "good", "great", "much", "many", "some", "one", "two", "now",
    "then", "make", "made", "like", "see", "saw", "use", "used",
    "also", "only", "other", "these", "those", "first", "last",
}


# ── Sampling ───────────────────────────────────────────────────────────────
def parse_chunk_date(meta: dict) -> datetime | None:
    """Best-effort YYYY-MM-DD parse out of metadata. Returns None on failure."""
    raw = meta.get("date") or ""
    if not raw:
        return None
    try:
        # Accept "2026-04-30", "2026-04", "2026"
        if len(raw) >= 10:
            return datetime.strptime(raw[:10], "%Y-%m-%d")
        if len(raw) >= 7:
            return datetime.strptime(raw[:7], "%Y-%m")
        if len(raw) >= 4:
            return datetime.strptime(raw[:4], "%Y")
    except ValueError:
        return None
    return None


def sample_chunks(n_wa: int, n_tr: int, recent_days: int | None) -> list[dict]:
    """Pull a random sample of {n_wa} WA + {n_tr} transcript chunks.

    Each entry: {"id": chroma_id, "content": str, "metadata": dict, "kind": "wa"|"tr"}.
    `recent_days` filters to chunks whose metadata `date` is within the window.
    """
    vs = get_vectorstore()
    coll = vs._collection
    all_data = coll.get(include=["documents", "metadatas"])
    ids = all_data["ids"]
    docs = all_data["documents"]
    metas = all_data["metadatas"]

    cutoff = None
    if recent_days:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=recent_days)

    wa, tr = [], []
    for i, meta in enumerate(metas):
        if meta is None:
            continue
        if cutoff:
            d = parse_chunk_date(meta)
            if d is None or d < cutoff:
                continue
        # Skip very short chunks (under 200 chars) — too sparse to derive a question
        if len(docs[i]) < 200:
            continue
        kind = "wa" if meta.get("type") == "whatsapp" else "tr"
        bucket = wa if kind == "wa" else tr
        bucket.append({"id": ids[i], "content": docs[i], "metadata": meta, "kind": kind})

    rng = random.Random()
    rng.shuffle(wa)
    rng.shuffle(tr)
    return wa[:n_wa] + tr[:n_tr]


# ── Question generation ────────────────────────────────────────────────────
def meta_summary(meta: dict) -> str:
    if meta.get("type") == "whatsapp":
        return (
            f"WhatsApp digest | group={meta.get('chat_name', '?')} | "
            f"date={meta.get('date', '?')} | period={meta.get('period_type', '?')}"
        )
    return (
        f"Transcript | speaker={format_display_name(meta.get('speaker', ''))} | "
        f"source={meta.get('source', '?')[:60]} | date={meta.get('date', '?')}"
    )


def generate_question(chunk: dict) -> str | None:
    """Ask Claude for a chunk-specific question. Returns None for tester-broken."""
    from langchain_anthropic import ChatAnthropic
    llm = ChatAnthropic(
        model=config.LLM_MODEL,
        temperature=0.3,
        anthropic_api_key=config.ANTHROPIC_API_KEY,
    )
    # Cap chunk content to keep prompt small — first 1800 chars is plenty
    body = chunk["content"][:1800]
    resp = llm.invoke(QUESTION_PROMPT.format(
        meta_summary=meta_summary(chunk["metadata"]),
        chunk_content=body,
    ))
    text = (resp.content or "").strip().strip('"').strip("'")
    if "TESTER_BROKEN" in text or len(text) < 8:
        return None
    # Strip leading "Q:" / "Question:" if Claude added one
    text = re.sub(r"^(?:q|question)\s*[:.]\s*", "", text, flags=re.IGNORECASE)
    return text


# ── Prod request ───────────────────────────────────────────────────────────
class TokenStore:
    token: str | None = None

    @classmethod
    def get(cls) -> str:
        if cls.token:
            return cls.token
        post(f"/api/auth/request-code", {"email": REVIEWER_EMAIL})
        verify = post(f"/api/auth/verify", {"email": REVIEWER_EMAIL, "code": REVIEWER_CODE})
        cls.token = verify["token"]
        return cls.token


def _ssl_context() -> ssl.SSLContext:
    """Build an SSL context using certifi's CA bundle.

    macOS Python builds often miss system trust anchors and choke on
    Render's cert without this. Falls back to default context on failure.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def post(path: str, body: dict, headers: dict | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers=h,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ask_prod(question: str) -> dict:
    return post("/api/ask", {"question": question}, headers={"Authorization": f"Bearer {TokenStore.get()}"})


# ── Pass criteria ──────────────────────────────────────────────────────────
def source_match(chunk: dict, returned_sources: list[dict]) -> bool:
    """True if any returned source plausibly equals the sampled chunk's origin.

    For WA chunks: match by `digest_id` (=== source_id).
    For transcripts: match by raw `speaker` value (the bot returns
    `format_display_name(raw)` so we compare display-cleaned).
    """
    if not returned_sources:
        return False
    meta = chunk["metadata"]
    if chunk["kind"] == "wa":
        target = meta.get("source_id") or ""
        for s in returned_sources:
            if s.get("digest_id") == target:
                return True
        return False
    target_disp = format_display_name(meta.get("speaker", "") or "")
    if not target_disp:
        return False
    for s in returned_sources:
        if (s.get("speaker") or "").strip().lower() == target_disp.strip().lower():
            return True
    return False


def significant_words(text: str, n: int = 50) -> list[str]:
    """Return up to N distinctive lowercased word stems from the text.

    Strips punctuation, lowercases, drops STOP_WORDS, drops <4-char tokens.
    Output is order-preserved unique tokens (so a chunk's most-mentioned
    distinctive nouns appear first).
    """
    raw = re.findall(r"[A-Za-z][A-Za-z0-9'\-]{3,}", text)
    seen, out = set(), []
    for w in raw:
        wl = w.lower()
        if wl in STOP_WORDS or wl in seen:
            continue
        seen.add(wl)
        out.append(wl)
        if len(out) >= n:
            break
    return out


def overlap_count(chunk_text: str, answer: str, min_overlap: int = 2) -> int:
    """How many distinctive chunk-words appear in the answer."""
    chunk_words = set(significant_words(chunk_text, n=80))
    answer_words = set(significant_words(answer, n=200))
    return len(chunk_words & answer_words)


CONF_THRESHOLD = 0.30  # same floor used in static suite for "should find something"


def evaluate(chunk: dict, question: str, response: dict) -> tuple[bool, list[str]]:
    """Return (pass, list-of-failure-reasons)."""
    fails = []
    conf = response.get("confidence", 0)
    sources = response.get("sources") or []
    answer = response.get("answer") or ""

    if conf < CONF_THRESHOLD:
        fails.append(f"conf {conf:.2f} < {CONF_THRESHOLD}")
    if not source_match(chunk, sources):
        fails.append("source mismatch (no returned source matches sampled chunk)")
    overlap = overlap_count(chunk["content"], answer)
    if overlap < 2:
        fails.append(f"low word overlap ({overlap} distinctive words shared with chunk)")
    return len(fails) == 0, fails


# ── Main loop ──────────────────────────────────────────────────────────────
def run(args) -> dict:
    print(f"BASE_URL={BASE_URL}")
    print(f"Sampling {args.n_wa} WA + {args.n_tr} transcript chunks"
          + (f" from last {args.recent_days} days" if args.recent_days else ""))
    chunks = sample_chunks(args.n_wa, args.n_tr, args.recent_days)
    print(f"Got {len(chunks)} chunks")
    if not chunks:
        print("No chunks matched — try widening --recent-days or removing the filter")
        return {"sampled": 0, "results": []}

    results = []
    tester_broken = 0
    passes = 0
    for i, chunk in enumerate(chunks, 1):
        kind = chunk["kind"].upper()
        meta_str = meta_summary(chunk["metadata"])
        print(f"\n[{i:2d}/{len(chunks)}] {kind} | {meta_str}")
        try:
            q = generate_question(chunk)
        except Exception as e:
            print(f"        ✗ question-gen exception: {e}")
            results.append({"chunk_id": chunk["id"], "kind": kind, "tester_broken": True, "reason": str(e)})
            tester_broken += 1
            continue
        if q is None:
            print(f"        ⚠  TESTER_BROKEN (chunk too generic for unique question)")
            results.append({"chunk_id": chunk["id"], "kind": kind, "tester_broken": True})
            tester_broken += 1
            continue
        print(f"        Q: {q!r}")
        t0 = time.time()
        try:
            r = ask_prod(q)
        except urllib.error.HTTPError as e:
            print(f"        ✗ prod HTTP error: {e}")
            results.append({"chunk_id": chunk["id"], "kind": kind, "question": q, "error": str(e)})
            continue
        elapsed = time.time() - t0
        ok, fails = evaluate(chunk, q, r)
        passes += int(ok)
        src_kinds = [s.get("source_type", s.get("type", "?"))[:2] for s in (r.get("sources") or [])][:5]
        print(f"        conf={r.get('confidence', 0):.2f} src=[{','.join(src_kinds)}]({len(r.get('sources') or [])}) {elapsed:.1f}s")
        print(f"        A: {(r.get('answer') or '')[:160].replace(chr(10), ' ')}{'...' if len(r.get('answer') or '') > 160 else ''}")
        if ok:
            print(f"        ✓ PASS")
        else:
            print(f"        ✗ FAIL — {'; '.join(fails)}")
        results.append({
            "chunk_id": chunk["id"], "kind": kind, "question": q,
            "passed": ok, "fails": fails,
            "confidence": r.get("confidence", 0),
            "n_sources": len(r.get("sources") or []),
        })

    eligible = len(chunks) - tester_broken
    print(f"\n{'=' * 70}")
    print(f"DYNAMIC SUITE: {passes}/{eligible} passed "
          f"({tester_broken} tester-broken, skipped from denominator)")
    print(f"{'=' * 70}")

    # Per-kind summary
    by_kind = Counter()
    pass_by_kind = Counter()
    for r in results:
        if r.get("tester_broken"):
            continue
        by_kind[r["kind"]] += 1
        if r.get("passed"):
            pass_by_kind[r["kind"]] += 1
    for k in sorted(by_kind):
        print(f"  {k}: {pass_by_kind[k]}/{by_kind[k]}")

    return {
        "base_url": BASE_URL,
        "sampled": len(chunks),
        "tester_broken": tester_broken,
        "passes": passes,
        "eligible": eligible,
        "results": results,
        "ran_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--n-wa", type=int, default=5, help="WA chunks to sample (default 5)")
    p.add_argument("--n-tr", type=int, default=5, help="Transcript chunks to sample (default 5)")
    p.add_argument("--recent-days", type=int, default=None,
                   help="Restrict sampling to chunks dated within N days (default: no limit)")
    p.add_argument("--json", type=str, default=None, help="Write full results to this JSON file")
    p.add_argument("--seed", type=int, default=None,
                   help="Seed RNG for reproducible sampling (default: random)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    if not config.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY missing from .env", file=sys.stderr)
        sys.exit(2)
    summary = run(args)
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nWrote full results → {args.json}")
    sys.exit(0 if summary.get("passes", 0) == summary.get("eligible", 1) else 1)
