"""
Query interface for MDS AI Bot.
Retrieves relevant chunks from the vector store and generates answers with citations.
Uses Claude (Anthropic) for LLM, OpenAI for embeddings.
"""

import json
import re
import sys
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_community.vectorstores import Chroma
from langchain_core.prompts import ChatPromptTemplate
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

import config

console = Console()

# Month name lookup for date formatting
_MONTH_NAMES = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}


def format_date_display(date_str: str) -> str:
    """Format a date string into readable 'Month Year' format.

    Handles:
        '2025-06-13' → 'June 2025'
        '2025-06'    → 'June 2025'
        '2025-01'    → 'January 2025'
        '2025'       → '2025'
        ''           → ''
    """
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) >= 2:
        year = parts[0]
        month = _MONTH_NAMES.get(parts[1], parts[1])
        return f"{month} {year}"
    return date_str


def clean_source_name(name: str) -> str:
    """Strip file extensions and clean up source display names.

    '1. Josh Hadley_otter_ai.txt' → '1. Josh Hadley_otter_ai'
    'Mogul Call with Adam Weiler.txt' → 'Mogul Call with Adam Weiler'
    """
    if not name:
        return name
    # Strip common extensions
    for ext in (".txt", ".md", ".vtt", ".srt", ".pdf", ".pptx"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    return name


def format_display_name(raw: str) -> str:
    """Clean a raw speaker/source name into a user-friendly display name.

    '2025-06-06_5. Prue Millsap'  → 'Prue Millsap'
    '2025-12-08_MDS Large Catalog Sellers Monthly Call with Nick Amos' → 'Large Catalog Sellers Monthly Call with Nick Amos'
    '2025-10-21_22. FC Chats_ Perfecting Media Buying in 2024' → 'FC Chats: Perfecting Media Buying in 2024'
    '2025-04-21_Josh Hadley_1' → 'Josh Hadley'
    '2025-01-10_Mogul Call with hasan & Dave' → 'Mogul Call with Hasan & Dave'
    '2025-03-07_Coaching Call with Steve Taylor March 2025' → 'Coaching Call with Steve Taylor'
    '2026-02-13_GMT20260210-210732_Recording' → 'Recording (Feb 2026)'
    """
    if not raw:
        return raw
    name = raw

    # Strip date prefix (YYYY-MM-DD_)
    name = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", name)
    # Strip leading number prefix (1. or 22.)
    name = re.sub(r"^\d+\.\s*", "", name)
    # Strip trailing _N or _NN (numbering suffix)
    name = re.sub(r"_\d+$", "", name)
    # Strip _otter_ai suffix
    name = re.sub(r"_otter_ai$", "", name, flags=re.IGNORECASE)
    # Clean "FC Chats_ " → "FC Chats: "
    name = name.replace("FC Chats_ ", "FC Chats: ")
    # Strip "MDS " prefix — it's always MDS content
    name = re.sub(r"^MDS\s+", "", name)
    # Strip trailing month + year (e.g. "Jan 2025", "March 2025", "Feb 2026")
    name = re.sub(
        r"\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+20\d{2}$",
        "", name, flags=re.IGNORECASE,
    )
    # Handle GMT recordings
    if re.match(r"^GMT\d+", name):
        name = "Recording"
    # Strip inline date codes like "03192025" (MMDDYYYY)
    name = re.sub(r"\s+\d{8}$", "", name)
    # Fix lowercase person names: "hasan & Dave" → "Hasan & Dave"
    # Capitalize any word that starts lowercase after "with" or "&" or at the start
    def _fix_caps(m):
        return m.group(0)[0] + m.group(0)[1].upper() + m.group(0)[2:]
    # Capitalize first word after "with "
    name = re.sub(r"(with\s)([a-z])", lambda m: m.group(1) + m.group(2).upper(), name)
    # Capitalize words after " & "
    name = re.sub(r"(&\s)([a-z])", lambda m: m.group(1) + m.group(2).upper(), name)

    return name.strip()


def extract_date_from_speaker(raw: str) -> str:
    """Extract a formatted date from a speaker/filename string.

    '2025-06-06_5. Prue Millsap' → 'June 2025'
    '2025-01-10_Mogul Call' → 'January 2025'
    'Some Name Without Date' → ''
    """
    match = re.match(r"^(\d{4})-(\d{2})", raw)
    if match:
        return format_date_display(f"{match.group(1)}-{match.group(2)}")
    return ""


def format_video_url(url: str) -> str:
    """Convert admin video URL to user-facing format.

    'https://app.mds.co/admin/contentlibrary/detail/69a8e1ff.../0' → 'https://app.mds.co/videos/69a8e1ff...'
    """
    if not url:
        return url
    match = re.match(r".*/admin/contentlibrary/detail/([a-f0-9]+)/\d+", url)
    if match:
        return f"https://app.mds.co/videos/{match.group(1)}"
    return url

SEARCH_LOG = Path(config.DATA_DIR) / "search_log.json"
TOPICS_CACHE = Path(config.DATA_DIR) / "topics_cache.json"

SYSTEM_PROMPT = """You are the MDS Knowledge Assistant, an AI that answers questions based on Million Dollar Sellers (MDS) content. There are TWO kinds of source material in the knowledge base:

  A. TRANSCRIPTS — recorded MDS sessions, presentations, mastermind calls. Each chunk is attributed to a named SPEAKER.
  B. WHATSAPP CONVERSATIONS — daily chat logs from MDS WhatsApp groups (e.g. "MDS Trading", "MDS AI & Automations"). Multiple participants per conversation. Lines look like `[date time UTC] @Name: message`.

RULES:
1. ONLY answer based on the provided context. Do not use outside knowledge.
2. If the context does not contain enough information to answer confidently, say: "I don't have enough information in the knowledge base to answer this confidently."
3. ATTRIBUTION:
   - For TRANSCRIPT sources, reference the speaker by name (e.g. "According to Ian Sells..." or "Yuri Dimitrov discussed...").
   - For WHATSAPP sources, attribute as a CONVERSATION, not a person. Examples:
       "In the MDS Trading WhatsApp group on May 2, members discussed..."
       "Ramon and Khalid debated this in the MDS AI & Automations chat..."
       "From the MDS Supplements group conversation: ..."
     NEVER treat the WhatsApp group name as a person's name.
   - When mixing both kinds, make the source type clear in the prose so the reader knows whether it came from a recorded session or a chat conversation.
4. Be concise and direct.
5. If the question is ambiguous, state your interpretation before answering.
6. Do NOT include a "Sources:" section at the end of your answer. Source citations are handled separately by the UI.

FORMAT:
- Give a clear, direct answer with proper attribution per the rules above.
- Do NOT list sources at the end — the system displays them automatically.
"""

USER_PROMPT_TEMPLATE = """Context from the MDS knowledge base:
---
{context}
---

Question: {question}

Answer based ONLY on the context above. Reference speakers by name when relevant."""


def get_vectorstore():
    """Load the existing vector store with ChromaDB's built-in embeddings."""
    return Chroma(
        collection_name=config.COLLECTION_NAME,
        persist_directory=str(config.VECTORSTORE_DIR),
    )


_WA_INTENT_SIGNALS = (
    "whatsapp",
    "whats app",
    "wa chat",
    "wa group",
    "wa channel",
    "chat group",
    "group chat",
    "in the chat",
    "in the group",
    "in the channel",
    "channel ai",
    "channel automations",
    "channel tiktok",
    "channel real estate",
    "channel resellers",
    "channel supplements",
    "channel retail",
    "channel trading",
    "channel logistics",
    "channel seo",
    "channel large sku",
    "ai and automations",
    "ai & automations",
)


def _is_wa_explicit_query(question: str) -> bool:
    """True when the user's question explicitly targets WhatsApp content.
    When True, ask() bypasses the per-source-type split and pulls TOP_K
    chunks from WA only — avoids the source list showing transcript
    speakers when the question was clearly about chat conversations."""
    lower = (question or "").lower()
    return any(signal in lower for signal in _WA_INTENT_SIGNALS)


# Words that look name-like but are actually meeting/recording labels.
# A 2-3 word display name containing any of these is NOT a person name.
_SPEAKER_STOP_WORDS = {
    "mds", "mogul", "call", "channel", "monthly", "weekly", "meeting",
    "advisory", "council", "chats", "fc", "with", "and", "the", "of",
    "from", "for", "real", "estate", "ai", "seo", "tiktok", "post",
    "event", "team", "chapter", "rockies", "inspire", "recording",
    "notes", "sku", "large", "logistics", "trading", "retail",
    "supplements", "resellers", "automations", "automation", "part",
    "panel",
}

# Lazily-built map of name-phrase (lowercase) → set of raw speaker
# metadata values that match. Built once per process from
# vectorstore metadata; cleared if the collection is re-ingested
# (process restart picks up the new state).
_SPEAKER_NAME_INDEX: dict | None = None


def _extract_name_candidates(display_name: str) -> list[str]:
    """Pull person-name candidates from a cleaned transcript display name.

    Yields up to three kinds of candidates:
      1. Multi-word person names after "with" / "w_":
         "Mogul Call with Josh Hadley"            → ["Josh Hadley"]
         "Call with Andrei Ureche and Alex Chiru" → ["Andrei Ureche", "Alex Chiru"]
         "FC Chats: ... w_Brett Eaton"            → ["Brett Eaton"]
      2. The post-"with" / post-"w_" tail as a whole, when it contains
         "&" / "and" co-host separators that would otherwise split the
         pair into single-word fragments:
         "Mogul Call with Hasan & Dave" → adds "Hasan & Dave"
      3. Stand-alone short names without scheduling stop words:
         "Scott Deetz" → ["Scott Deetz"]

    Returns [] for pure meeting labels with no person attribution
    ("Rockies Chapter Monthly Call", "AI Channel monthly Call").
    """
    if not display_name:
        return []
    out: list[str] = []
    m = re.search(
        r"(?:\bwith\s+|w_)([A-Z][\w.\-\']+(?:\s+(?:[A-Z][\w.\-\']+|&|and))*)",
        display_name,
    )
    if m:
        tail = m.group(1).strip().rstrip(".,;:")
        # Multi-person source — keep the full tail too so a question
        # quoting "Hasan & Dave" still matches.
        if re.search(r"\s+(?:and|&)\s+", tail):
            out.append(tail)
        for part in re.split(r"\s+(?:and|&)\s+", tail):
            part = part.strip().rstrip(".,;:")
            words = part.split()
            if 2 <= len(words) <= 4 and all(w and w[0].isupper() for w in words):
                out.append(part)
        return out
    words = display_name.split()
    if 2 <= len(words) <= 3 and all(w and (w[0].isupper() or w[0].isdigit()) for w in words):
        if not any(w.lower().rstrip(".,:") in _SPEAKER_STOP_WORDS for w in words):
            out.append(display_name)
    return out


def _get_speaker_name_index() -> dict[str, list[str]]:
    """Build (and cache) the name-phrase → raw-speaker-list index.

    Iterates all transcript chunks once, runs format_display_name +
    _extract_name_candidates, and inverts into a phrase-keyed map.
    Cost: one collection.get(metadatas) at first call (~1s for 10k chunks).
    """
    global _SPEAKER_NAME_INDEX
    if _SPEAKER_NAME_INDEX is not None:
        return _SPEAKER_NAME_INDEX
    vectorstore = get_vectorstore()
    all_meta = vectorstore._collection.get(include=["metadatas"])
    index: dict[str, set[str]] = {}
    for meta in all_meta["metadatas"]:
        if not meta:
            continue
        if meta.get("type") == "whatsapp":
            continue
        raw = meta.get("speaker", "")
        if not raw:
            continue
        for name in _extract_name_candidates(format_display_name(raw)):
            index.setdefault(name.lower(), set()).add(raw)
    _SPEAKER_NAME_INDEX = {k: sorted(v) for k, v in index.items()}
    return _SPEAKER_NAME_INDEX


def _detect_speakers_in_query(question: str) -> list[str]:
    """Return raw-speaker metadata values whose person-name appears in the query.

    Returns [] if no known speaker name (≥2 words) appears in the question.
    Multiple matches are merged so a query mentioning two speakers pulls
    chunks from both.
    """
    if not question:
        return []
    q_lower = question.lower()
    matches: set[str] = set()
    for phrase, raws in _get_speaker_name_index().items():
        if phrase in q_lower:
            matches.update(raws)
    return sorted(matches)


def format_context(docs_with_scores: list) -> str:
    """Format retrieved documents into context string with metadata.

    Distinguishes WhatsApp conversation chunks from transcript chunks so the
    LLM applies the correct attribution style.
    """
    parts = []
    for i, (doc, score) in enumerate(docs_with_scores, 1):
        meta = doc.metadata
        source_type = meta.get("type", "")

        if source_type == "whatsapp":
            chat_name = meta.get("chat_name", "Unknown group")
            date_str = format_date_display(meta.get("date", ""))
            period = meta.get("period_type", "daily")
            source_info = (
                f"Source type: WHATSAPP CONVERSATION | "
                f"Group: {chat_name} | Date: {date_str} | Period: {period}"
            )
        else:
            source_name = clean_source_name(meta.get("source", "Unknown"))
            source_info = f"Source type: TRANSCRIPT | Source: {source_name}"
            speaker = meta.get("speaker", "")
            if speaker:
                source_info += f" | Speaker: {clean_source_name(speaker)}"
            if meta.get("event"):
                source_info += f" | Event: {meta['event']}"
            if meta.get("date"):
                source_info += f" | Date: {format_date_display(meta['date'])}"
            if meta.get("timestamp_start"):
                source_info += f" | Timestamp: {meta['timestamp_start']}"
            if meta.get("page"):
                source_info += f" | Page: {meta['page']}"
            if meta.get("slide"):
                source_info += f" | Slide: {meta['slide']}"

        parts.append(f"[Chunk {i}] (relevance: {1 - score:.2f})\n{source_info}\n{doc.page_content}")

    return "\n\n---\n\n".join(parts)


def check_api_keys() -> str | None:
    """Check required API keys are set. Returns error message or None."""
    if not config.ANTHROPIC_API_KEY:
        return "Error: ANTHROPIC_API_KEY not set. Add it to .env file."
    return None


def ask(question: str, verbose: bool = False) -> dict:
    """
    Ask a question against the knowledge base.

    Returns dict with:
      - answer: str
      - sources: list of source metadata
      - confidence: float (0-1, higher = more confident)
      - chunks_used: int
    """
    key_error = check_api_keys()
    if key_error:
        return {"answer": key_error, "sources": [], "confidence": 0, "chunks_used": 0}

    vectorstore = get_vectorstore()

    # Check if vectorstore has any documents
    collection = vectorstore._collection
    if collection.count() == 0:
        return {
            "answer": "The knowledge base is empty. Please ingest some documents first using: python bot.py ingest <files>",
            "sources": [],
            "confidence": 0,
            "chunks_used": 0,
        }

    # Retrieval strategy depends on query intent:
    #
    # 1. If the user explicitly mentions WhatsApp / chat / group → pull
    #    TOP_K from WA chunks only. Mixing in transcripts pollutes the
    #    answer and makes the source list misleading.
    # 2. If the query mentions a known transcript speaker (e.g.
    #    "Josh Hadley") → pre-filter the transcript half to ONLY that
    #    speaker's chunks. Generic chunks were outranking speaker-
    #    specific ones in pure similarity ranking.
    # 3. Otherwise → split the pool: TOP_K/2 WA + TOP_K/2 transcripts so
    #    WA chunks (~160) aren't crowded out by transcripts (~9879).
    if _is_wa_explicit_query(question):
        docs_with_scores = vectorstore.similarity_search_with_score(
            question, k=config.TOP_K, filter={"type": "whatsapp"}
        )
    else:
        half = max(1, config.TOP_K // 2)
        wa_docs = vectorstore.similarity_search_with_score(
            question, k=half, filter={"type": "whatsapp"}
        )
        speaker_matches = _detect_speakers_in_query(question)
        if speaker_matches:
            tr_filter = {"speaker": {"$in": speaker_matches}}
        else:
            tr_filter = {"type": {"$ne": "whatsapp"}}
        tr_docs = vectorstore.similarity_search_with_score(
            question, k=config.TOP_K - half, filter=tr_filter
        )
        docs_with_scores = sorted(wa_docs + tr_docs, key=lambda x: x[1])

    if not docs_with_scores:
        return {
            "answer": "I don't have enough information in the knowledge base to answer this.",
            "sources": [],
            "confidence": 0,
            "chunks_used": 0,
        }

    # Calculate confidence from similarity scores
    # ChromaDB default embeddings use squared L2 distance: 0 = identical, ~2 = unrelated
    # Convert to 0-1 scale: 0 distance -> 1.0 confidence, 2+ distance -> 0.0
    similarities = [max(0, 1 - score / 2) for _, score in docs_with_scores]
    avg_confidence = sum(similarities) / len(similarities)

    if verbose:
        console.print(f"\n[dim]Retrieved {len(docs_with_scores)} chunks, avg confidence: {avg_confidence:.2f}[/dim]")
        for i, (doc, score) in enumerate(docs_with_scores):
            sim = max(0, 1 - score / 2)
            console.print(f"[dim]  Chunk {i+1}: similarity={sim:.2f} distance={score:.2f} from {doc.metadata.get('source', '?')}[/dim]")

    # Check confidence threshold — don't show sources for irrelevant queries
    if avg_confidence < config.CONFIDENCE_THRESHOLD:
        return {
            "answer": "I don't have enough information in the knowledge base to answer this confidently. The most relevant content I found doesn't closely match your question.",
            "sources": [],
            "confidence": avg_confidence,
            "chunks_used": len(docs_with_scores),
        }

    # Build context and query Claude
    context = format_context(docs_with_scores)

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", USER_PROMPT_TEMPLATE),
    ])

    llm = ChatAnthropic(
        model=config.LLM_MODEL,
        temperature=config.LLM_TEMPERATURE,
        anthropic_api_key=config.ANTHROPIC_API_KEY,
    )

    chain = prompt | llm
    response = chain.invoke({"context": context, "question": question})

    # Detect Claude's hedging. Two distinct cases:
    #
    #   GENUINE DECLINE — hedge phrase appears in the first 200 chars AND
    #     the whole answer is short (<250 chars). Claude is saying "no
    #     relevant info" and that's all there is. Strip sources, cap conf
    #     at 0.18. Showing 5 sources next to "I don't know" is misleading.
    #
    #   SUBSTANTIVE WITH HEDGE — hedge phrase present somewhere, but the
    #     answer is long enough to have real content (>= 250 chars OR the
    #     hedge is buried past char 200). Claude found relevant chunks and
    #     summarized them while caveating that some specific detail was
    #     missing. Keep sources but cap the boost at 0.45 instead of 0.65
    #     so the UI doesn't oversell a hedged answer.
    answer_text = response.content
    _no_info_phrases = [
        "i don't have enough information",
        "not enough information",
        "doesn't contain specific information",
        "doesn't contain enough information",
        "does not contain specific information",
        "does not contain enough information",
        "no relevant information",
    ]
    answer_lower = answer_text.lower()
    has_no_info = any(phrase in answer_lower for phrase in _no_info_phrases)
    hedge_at_start = any(phrase in answer_lower[:200] for phrase in _no_info_phrases)
    is_short_answer = len(answer_text) < 250
    is_genuine_decline = hedge_at_start and is_short_answer

    if is_genuine_decline:
        avg_confidence = min(avg_confidence, 0.18)
        return {
            "answer": answer_text,
            "sources": [],
            "confidence": avg_confidence,
            "chunks_used": len(docs_with_scores),
        }

    # Build deduplicated, enriched source list (clean names, format dates).
    # WhatsApp digests dedup by source_id (Airtable record); transcripts by raw speaker.
    seen_keys = set()
    enriched_sources = []
    for doc, _ in docs_with_scores:
        meta = doc.metadata
        source_type = meta.get("type", "")

        if source_type == "whatsapp":
            digest_id = meta.get("source_id", "") or meta.get("source", "")
            key = ("wa", digest_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            chat_name = meta.get("chat_name", "Unknown group")
            display_date = format_date_display(meta.get("date", ""))
            # `source_id` is the Airtable record ID; expose it directly so
            # iOS can deep-link to that digest's detail view from the source card.
            airtable_record_id = meta.get("source_id", "") or digest_id.replace("airtable://Summaries/", "")
            source_entry = {
                # `speaker` carries the display name for backwards compat with
                # existing iOS clients; the explicit fields below let new clients
                # render WA sources properly.
                "speaker": chat_name,
                "date": display_date,
                "event": "",
                "topic": "",
                "type": "whatsapp",
                "source_type": "whatsapp",
                "chat_name": chat_name,
                "period_type": meta.get("period_type", "daily"),
                "msg_count": int(meta.get("msg_count", 0) or 0),
                "source": meta.get("source", ""),
                "digest_id": airtable_record_id,
            }
            enriched_sources.append(source_entry)
        else:
            raw_speaker = meta.get("speaker", "Unknown")
            key = ("transcript", raw_speaker)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            display_date = format_date_display(meta.get("date", ""))
            if not display_date:
                display_date = extract_date_from_speaker(raw_speaker)

            source_entry = {
                "speaker": format_display_name(raw_speaker),
                "date": display_date,
                "event": meta.get("event", ""),
                "topic": meta.get("topic", ""),
                "type": meta.get("type", "") or "transcript",
                "source_type": "transcript",
                "source": clean_source_name(meta.get("source", "")),
            }
            video_url = meta.get("video_url", "")
            if video_url:
                source_entry["video_url"] = format_video_url(video_url)
            enriched_sources.append(source_entry)

    # Boost confidence when Claude gave a substantive answer with sources.
    # Raw embedding similarity often underestimates relevance (0.3-0.5 range
    # even for great matches), so we correct based on answer quality signals.
    # Hedged-but-substantive answers get a smaller boost so the UI doesn't
    # oversell them.
    if enriched_sources and len(answer_text) > 200:
        floor = 0.45 if has_no_info else 0.65
        avg_confidence = max(avg_confidence, floor)

    return {
        "answer": answer_text,
        "sources": enriched_sources,
        "confidence": avg_confidence,
        "chunks_used": len(docs_with_scores),
    }


def summarize_source(display_name: str) -> dict:
    """Summarize all content from a specific source/speaker.

    Instead of doing a semantic search on the speaker name (which fails because
    transcript content doesn't mention the speaker's name), this function:
    1. Finds the raw speaker name from metadata that matches the display name
    2. Fetches ALL chunks from that speaker using exact metadata filter
    3. Sends them to Claude for a thorough summary
    """
    key_error = check_api_keys()
    if key_error:
        return {"answer": key_error, "sources": [], "confidence": 0, "chunks_used": 0}

    vectorstore = get_vectorstore()
    collection = vectorstore._collection

    if collection.count() == 0:
        return {"answer": "Knowledge base is empty.", "sources": [], "confidence": 0, "chunks_used": 0}

    # Step 1: Find the raw speaker that matches the display name.
    # We do a small semantic search first to narrow candidates, then check metadata.
    initial_results = vectorstore.similarity_search_with_score(display_name, k=20)

    raw_speaker = None
    for doc, _score in initial_results:
        raw = doc.metadata.get("speaker", "")
        if raw and display_name.lower() in format_display_name(raw).lower():
            raw_speaker = raw
            break

    # If semantic search didn't find a metadata match, scan broader
    if not raw_speaker:
        all_meta = collection.get(include=["metadatas"])
        for meta in all_meta["metadatas"]:
            raw = meta.get("speaker", "")
            if raw and display_name.lower() in format_display_name(raw).lower():
                raw_speaker = raw
                break

    if not raw_speaker:
        # Last resort: fall back to regular ask with a better query
        return ask(f"What did {display_name} discuss in their MDS session?")

    # Step 2: Get ALL chunks from this exact speaker
    results = collection.get(
        where={"speaker": {"$eq": raw_speaker}},
        include=["documents", "metadatas"],
    )

    if not results["documents"]:
        return ask(f"What did {display_name} discuss in their MDS session?")

    # Step 3: Build context from chunks (up to 15 for token budget)
    chunks = list(zip(results["documents"], results["metadatas"]))[:15]
    total_chunks = len(results["documents"])

    context_parts = []
    for i, (content, meta) in enumerate(chunks, 1):
        source_info = ""
        if meta.get("date"):
            source_info += f"Date: {format_date_display(meta['date'])} | "
        if meta.get("event"):
            source_info += f"Event: {meta['event']} | "
        context_parts.append(f"[Chunk {i}]\n{source_info}\n{content}")

    context = "\n\n---\n\n".join(context_parts)

    # Step 4: Ask Claude to summarize
    prompt = ChatPromptTemplate.from_messages([
        ("system", (
            "You are the MDS Knowledge Assistant. Your task is to provide a comprehensive "
            "summary of an MDS session. Include the key topics discussed, specific insights, "
            "strategies, and actionable takeaways. Reference specific points and speakers by name. "
            "Be thorough but concise — organize into clear sections."
        )),
        ("human", (
            "Here is the full transcript content from the MDS session: {display_name}\n"
            "---\n{context}\n---\n\n"
            "Provide a comprehensive summary of this session including:\n"
            "1. Main topics discussed\n"
            "2. Key insights and strategies shared\n"
            "3. Actionable takeaways for Amazon/e-commerce sellers"
        )),
    ])

    llm = ChatAnthropic(
        model=config.LLM_MODEL,
        temperature=config.LLM_TEMPERATURE,
        anthropic_api_key=config.ANTHROPIC_API_KEY,
    )

    chain = prompt | llm
    response = chain.invoke({"display_name": display_name, "context": context})

    # Build source entry from metadata
    meta = results["metadatas"][0]
    display_date = format_date_display(meta.get("date", ""))
    if not display_date:
        display_date = extract_date_from_speaker(raw_speaker)

    source_entry = {
        "speaker": format_display_name(raw_speaker),
        "date": display_date,
        "event": meta.get("event", ""),
        "source": clean_source_name(meta.get("source", "")),
    }
    video_url = meta.get("video_url", "")
    if video_url:
        source_entry["video_url"] = format_video_url(video_url)

    return {
        "answer": response.content,
        "sources": [source_entry],
        "confidence": 0.85,
        "chunks_used": total_chunks,
    }


def track_search(query: str):
    """Track a search query for popular searches."""
    log = {}
    if SEARCH_LOG.exists():
        try:
            log = json.loads(SEARCH_LOG.read_text(encoding="utf-8"))
        except Exception:
            pass
    key = query.strip().lower()
    if len(key) < 3:
        return
    log[key] = log.get(key, 0) + 1
    SEARCH_LOG.write_text(json.dumps(log), encoding="utf-8")


def get_popular_searches(limit: int = 6) -> list[str]:
    """Return most searched queries."""
    if not SEARCH_LOG.exists():
        return []
    try:
        log = json.loads(SEARCH_LOG.read_text(encoding="utf-8"))
        sorted_q = sorted(log.items(), key=lambda x: x[1], reverse=True)
        return [q for q, count in sorted_q[:limit]]
    except Exception:
        return []


def extract_topics() -> list[str]:
    """Extract main topics from the knowledge base using Claude. Cached to file."""
    if TOPICS_CACHE.exists():
        try:
            topics = json.loads(TOPICS_CACHE.read_text(encoding="utf-8"))
            if topics:
                return topics
        except Exception:
            pass

    key_error = check_api_keys()
    if key_error:
        return []

    vectorstore = get_vectorstore()
    collection = vectorstore._collection
    if collection.count() == 0:
        return []

    # Get a diverse sample of chunks
    results = collection.get(limit=30, include=["documents"])
    sample = "\n---\n".join(results["documents"][:15])[:6000]

    llm = ChatAnthropic(
        model=config.LLM_MODEL,
        temperature=0,
        anthropic_api_key=config.ANTHROPIC_API_KEY,
    )

    try:
        response = llm.invoke(
            "Analyze these business mastermind session excerpts and extract 8-10 main TOPICS discussed. "
            "Return ONLY a JSON array of short topic phrases (2-5 words each). "
            "Focus on actionable business topics and strategies — NOT speaker names.\n\n"
            f"Example: [\"Exit Planning\", \"Amazon PPC\", \"Supply Chain Strategy\"]\n\n{sample}"
        )
        # Extract JSON from response (handle markdown code blocks)
        content = response.content.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        topics = json.loads(content)
        TOPICS_CACHE.write_text(json.dumps(topics, indent=2), encoding="utf-8")
        return topics
    except Exception as e:
        console.print(f"[dim]Topic extraction failed: {e}[/dim]")
        return []


def interactive():
    """Run interactive Q&A loop."""
    console.print(Panel(
        "[bold]MDS Knowledge Assistant[/bold] (powered by Claude)\n"
        "Ask questions about MDS content. Type 'quit' to exit.\n"
        "Type 'verbose' to toggle debug info.",
        style="blue",
    ))

    verbose = False

    while True:
        try:
            question = console.input("\n[bold cyan]Question:[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nGoodbye!")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            console.print("Goodbye!")
            break
        if question.lower() == "verbose":
            verbose = not verbose
            console.print(f"[dim]Verbose mode: {'on' if verbose else 'off'}[/dim]")
            continue

        result = ask(question, verbose=verbose)

        # Display answer
        console.print()
        console.print(Markdown(result["answer"]))

        # Display confidence
        conf = result["confidence"]
        conf_color = "green" if conf > 0.6 else "yellow" if conf > 0.3 else "red"
        console.print(f"\n[{conf_color}]Confidence: {conf:.0%}[/{conf_color}] | Chunks used: {result['chunks_used']}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        result = ask(question, verbose=True)
        console.print()
        console.print(Markdown(result["answer"]))
        conf = result["confidence"]
        conf_color = "green" if conf > 0.6 else "yellow" if conf > 0.3 else "red"
        console.print(f"\n[{conf_color}]Confidence: {conf:.0%}[/{conf_color}]")
    else:
        interactive()
