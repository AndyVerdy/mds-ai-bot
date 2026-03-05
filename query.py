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

SYSTEM_PROMPT = """You are the MDS Knowledge Assistant, an AI that answers questions based on Million Dollar Sellers (MDS) content including video transcripts and presentation decks.

RULES:
1. ONLY answer based on the provided context. Do not use outside knowledge.
2. If the context does not contain enough information to answer confidently, say: "I don't have enough information in the knowledge base to answer this confidently."
3. When referencing a speaker, mention their name naturally in your answer (e.g. "According to Ian Sells..." or "Yuri Dimitrov discussed...").
4. Be concise and direct.
5. If the question is ambiguous, state your interpretation before answering.
6. Do NOT include a "Sources:" section at the end of your answer. Source citations are handled separately by the UI.

FORMAT:
- Give a clear, direct answer referencing speakers by name
- Do NOT list sources at the end — the system displays them automatically
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


def format_context(docs_with_scores: list) -> str:
    """Format retrieved documents into context string with metadata."""
    parts = []
    for i, (doc, score) in enumerate(docs_with_scores, 1):
        meta = doc.metadata
        source_name = clean_source_name(meta.get("source", "Unknown"))
        source_info = f"Source: {source_name}"

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

    # Retrieve relevant chunks with scores
    docs_with_scores = vectorstore.similarity_search_with_score(
        question,
        k=config.TOP_K,
    )

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

    # Check if Claude's answer indicates it doesn't have enough info
    answer_text = response.content
    _no_info_phrases = [
        "i don't have enough information",
        "not enough information",
        "doesn't contain specific information",
        "doesn't contain enough information",
        "does not contain specific information",
        "does not contain enough information",
        "no relevant information",
        "not directly addressed",
    ]
    if any(phrase in answer_text.lower() for phrase in _no_info_phrases):
        avg_confidence = min(avg_confidence, 0.18)
        # Don't show misleading sources when bot has no relevant answer
        return {
            "answer": answer_text,
            "sources": [],
            "confidence": avg_confidence,
            "chunks_used": len(docs_with_scores),
        }

    # Build deduplicated, enriched source list (clean names, format dates)
    seen_speakers = set()
    enriched_sources = []
    for doc, _ in docs_with_scores:
        meta = doc.metadata
        raw_speaker = meta.get("speaker", "Unknown")
        # Deduplicate by raw value (unique per file)
        if raw_speaker in seen_speakers:
            continue
        seen_speakers.add(raw_speaker)

        # Use metadata date if available, otherwise extract from filename
        display_date = format_date_display(meta.get("date", ""))
        if not display_date:
            display_date = extract_date_from_speaker(raw_speaker)

        source_entry = {
            "speaker": format_display_name(raw_speaker),
            "date": display_date,
            "event": meta.get("event", ""),
            "topic": meta.get("topic", ""),
            "type": meta.get("type", ""),
            "source": clean_source_name(meta.get("source", "")),
        }
        # Include video URL if available (converted to user-facing format)
        video_url = meta.get("video_url", "")
        if video_url:
            source_entry["video_url"] = format_video_url(video_url)
        enriched_sources.append(source_entry)

    return {
        "answer": answer_text,
        "sources": enriched_sources,
        "confidence": avg_confidence,
        "chunks_used": len(docs_with_scores),
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
