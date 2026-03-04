"""
Query interface for MDS AI Bot.
Retrieves relevant chunks from the vector store and generates answers with citations.
Uses Claude (Anthropic) for LLM, OpenAI for embeddings.
"""

import sys

from langchain_anthropic import ChatAnthropic
from langchain_community.vectorstores import Chroma
from langchain_core.prompts import ChatPromptTemplate
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

import config

console = Console()

SYSTEM_PROMPT = """You are the MDS Knowledge Assistant, an AI that answers questions based on Million Dollar Sellers (MDS) content including video transcripts and presentation decks.

RULES:
1. ONLY answer based on the provided context. Do not use outside knowledge.
2. If the context does not contain enough information to answer confidently, say: "I don't have enough information in the knowledge base to answer this confidently."
3. Always cite your sources. For each claim, reference the source document and timestamp/page/slide if available.
4. Be concise and direct.
5. If the question is ambiguous, state your interpretation before answering.

FORMAT:
- Give a clear, direct answer
- Follow with "Sources:" listing the documents you referenced
"""

USER_PROMPT_TEMPLATE = """Context from the MDS knowledge base:
---
{context}
---

Question: {question}

Answer based ONLY on the context above. Cite sources."""


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
        source_info = f"Source: {meta.get('source', 'Unknown')}"

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

    # Check confidence threshold
    if avg_confidence < config.CONFIDENCE_THRESHOLD:
        return {
            "answer": "I don't have enough information in the knowledge base to answer this confidently. The most relevant content I found doesn't closely match your question.",
            "sources": [doc.metadata for doc, _ in docs_with_scores],
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

    return {
        "answer": response.content,
        "sources": [doc.metadata for doc, _ in docs_with_scores],
        "confidence": avg_confidence,
        "chunks_used": len(docs_with_scores),
    }


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
