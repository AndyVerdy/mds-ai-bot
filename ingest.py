"""
Document ingestion pipeline.
Supports: .txt, .md, .vtt, .srt, .pdf, .pptx
Chunks documents, embeds them, and stores in ChromaDB.

Improvements:
- Extracts speaker name from filename (e.g., "1. Josh Hadley_otter_ai.txt" → "Josh Hadley")
- Prepends speaker/source context header to each chunk
- Increased chunk size for better conversational context
"""

import json
import re
import sys
from pathlib import Path

import fitz  # pymupdf
from pptx import Presentation
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from rich.console import Console
from rich.progress import Progress

import config

console = Console()

# File metadata cache (loaded from data/metadata.json)
_file_metadata: dict = {}

# Video links cache (loaded from data/video_links.json)
_video_links: dict = {}


def load_file_metadata(data_dir: str = None) -> dict:
    """Load file metadata (dates, events, topics) from metadata.json."""
    global _file_metadata
    search_dirs = [data_dir] if data_dir else [str(config.DATA_DIR), "data/", "data"]
    for d in search_dirs:
        meta_path = Path(d) / "metadata.json"
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                _file_metadata = raw
                return _file_metadata
            except Exception:
                pass
    return {}


def load_video_links(data_dir: str = None) -> dict:
    """Load video links mapping from video_links.json."""
    global _video_links
    search_dirs = [data_dir] if data_dir else [str(config.DATA_DIR), "data/", "data"]
    for d in search_dirs:
        vl_path = Path(d) / "video_links.json"
        if vl_path.exists():
            try:
                with open(vl_path, "r", encoding="utf-8") as f:
                    _video_links = json.load(f)
                console.print(f"[blue]Loaded {len(_video_links)} video links[/blue]")
                return _video_links
            except Exception:
                pass
    return {}


def get_video_url(file_path: str) -> str:
    """Get video URL for a transcript file, if available."""
    filename = Path(file_path).name
    entry = _video_links.get(filename)
    if entry:
        return entry.get("video_url", "")
    return ""


def get_file_metadata(file_path: str) -> dict:
    """Get date/event/topic metadata for a specific file."""
    filename = Path(file_path).name
    files = _file_metadata.get("files", {})
    defaults = _file_metadata.get("default", {})

    if filename in files:
        meta = {**defaults, **files[filename]}
    else:
        meta = dict(defaults)
    return meta


def extract_speaker_name(file_path: str) -> str:
    """Extract speaker name from filename.

    Handles patterns like:
        "1. Josh Hadley_otter_ai.txt" → "Josh Hadley"
        "11. Ozlem Sengul_otter_ai.txt" → "Ozlem Sengul"
        "Alar Huul_otter_ai.txt" → "Alar Huul"
        "Matt Dreier_otter_ai.txt" → "Matt Dreier"
    """
    name = Path(file_path).stem  # remove extension

    # Remove _otter_ai suffix
    name = re.sub(r"_otter_ai$", "", name, flags=re.IGNORECASE)

    # Remove leading number + dot prefix (e.g., "1. ", "11. ")
    name = re.sub(r"^\d+\.\s*", "", name)

    return name.strip()


def make_context_header(metadata: dict) -> str:
    """Build a context header string to prepend to chunk content."""
    parts = []

    speaker = metadata.get("speaker")
    if speaker:
        parts.append(f"Speaker: {speaker}")

    event = metadata.get("event")
    if event:
        parts.append(f"Event: {event}")

    date = metadata.get("date")
    if date:
        parts.append(f"Date: {date}")

    topic = metadata.get("topic")
    if topic:
        parts.append(f"Topic: {topic}")

    source = metadata.get("source", "")
    if source:
        parts.append(f"Source: {Path(source).name}")

    doc_type = metadata.get("type", "")
    if doc_type:
        parts.append(f"Type: {doc_type}")

    if metadata.get("timestamp_start"):
        parts.append(f"Timestamp: {metadata['timestamp_start']}")
    if metadata.get("page"):
        parts.append(f"Page: {metadata['page']}")
    if metadata.get("slide"):
        parts.append(f"Slide: {metadata['slide']}")

    if parts:
        return "[" + " | ".join(parts) + "]\n"
    return ""


def parse_vtt_srt(text: str, file_path: str) -> list[Document]:
    """Parse VTT/SRT subtitle files into timestamped chunks."""
    lines = text.strip().split("\n")
    segments = []
    current_time = ""
    current_text = []
    file_meta = get_file_metadata(file_path)

    timestamp_pattern = re.compile(
        r"(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->\s*(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}"
    )

    for line in lines:
        line = line.strip()
        if timestamp_pattern.search(line):
            if current_text:
                segments.append({
                    "timestamp": current_time,
                    "text": " ".join(current_text),
                })
            current_time = line.split("-->")[0].strip()
            current_text = []
        elif line and not line.isdigit() and not line.startswith("WEBVTT"):
            clean = re.sub(r"<[^>]+>", "", line)
            if clean.strip():
                current_text.append(clean.strip())

    if current_text:
        segments.append({
            "timestamp": current_time,
            "text": " ".join(current_text),
        })

    speaker = extract_speaker_name(file_path)

    # Group segments into larger chunks
    docs = []
    chunk_text = []
    chunk_start = segments[0]["timestamp"] if segments else ""
    char_count = 0

    for seg in segments:
        chunk_text.append(seg["text"])
        char_count += len(seg["text"])
        if char_count >= config.CHUNK_SIZE:
            meta = {
                "source": file_path,
                "type": "transcript",
                "timestamp_start": chunk_start,
                "speaker": speaker,
                **{k: v for k, v in file_meta.items() if k != "_comment"},
            }
            content = make_context_header(meta) + " ".join(chunk_text)
            docs.append(Document(page_content=content, metadata=meta))
            chunk_text = []
            chunk_start = seg["timestamp"]
            char_count = 0

    if chunk_text:
        meta = {
            "source": file_path,
            "type": "transcript",
            "timestamp_start": chunk_start,
            "speaker": speaker,
            **{k: v for k, v in file_meta.items() if k != "_comment"},
        }
        content = make_context_header(meta) + " ".join(chunk_text)
        docs.append(Document(page_content=content, metadata=meta))

    return docs


def parse_otter_transcript(text: str, file_path: str) -> list[Document]:
    """Parse Otter.ai transcript .txt files with speaker/timestamp awareness.

    Format: "Unknown Speaker  0:05\ntext...\n\nUnknown Speaker  1:23\ntext..."
    """
    speaker_from_filename = extract_speaker_name(file_path)
    file_meta = get_file_metadata(file_path)

    # Split into speaker segments
    # Pattern: "Speaker Name  H:MM:SS" or "Unknown Speaker  M:SS"
    segment_pattern = re.compile(
        r"^(.+?)\s{2,}(\d{1,2}:\d{2}(?::\d{2})?)\s*$", re.MULTILINE
    )

    segments = []
    matches = list(segment_pattern.finditer(text))

    for i, match in enumerate(matches):
        seg_speaker = match.group(1).strip()
        timestamp = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()

        if content:
            segments.append({
                "speaker": seg_speaker,
                "timestamp": timestamp,
                "text": content,
            })

    if not segments:
        # Fallback: treat as plain text if no speaker patterns found
        meta = {
            "source": file_path,
            "type": "transcript",
            "speaker": speaker_from_filename,
            **{k: v for k, v in file_meta.items() if k != "_comment"},
        }
        return [Document(page_content=text, metadata=meta)]

    # Group segments into chunks of ~CHUNK_SIZE characters
    docs = []
    chunk_parts = []
    chunk_start_ts = segments[0]["timestamp"] if segments else ""
    char_count = 0

    for seg in segments:
        chunk_parts.append(seg["text"])
        char_count += len(seg["text"])

        if char_count >= config.CHUNK_SIZE:
            meta = {
                "source": file_path,
                "type": "transcript",
                "timestamp_start": chunk_start_ts,
                "speaker": speaker_from_filename,
                **{k: v for k, v in file_meta.items() if k != "_comment"},
            }
            content = make_context_header(meta) + "\n".join(chunk_parts)
            docs.append(Document(page_content=content, metadata=meta))
            chunk_parts = []
            chunk_start_ts = seg["timestamp"]
            char_count = 0

    if chunk_parts:
        meta = {
            "source": file_path,
            "type": "transcript",
            "timestamp_start": chunk_start_ts,
            "speaker": speaker_from_filename,
            **{k: v for k, v in file_meta.items() if k != "_comment"},
        }
        content = make_context_header(meta) + "\n".join(chunk_parts)
        docs.append(Document(page_content=content, metadata=meta))

    return docs


def parse_pdf(file_path: str) -> list[Document]:
    """Extract text from PDF, one chunk per page (then split further)."""
    doc = fitz.open(file_path)
    speaker = extract_speaker_name(file_path)
    file_meta = get_file_metadata(file_path)
    documents = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text().strip()
        if text:
            meta = {
                "source": file_path,
                "type": "pdf",
                "page": page_num + 1,
                "speaker": speaker,
                **{k: v for k, v in file_meta.items() if k != "_comment"},
            }
            content = make_context_header(meta) + text
            documents.append(Document(page_content=content, metadata=meta))
    doc.close()
    return documents


def parse_pptx(file_path: str) -> list[Document]:
    """Extract text from PPTX, one document per slide."""
    prs = Presentation(file_path)
    speaker = extract_speaker_name(file_path)
    file_meta = get_file_metadata(file_path)
    documents = []
    for slide_num, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = paragraph.text.strip()
                    if text:
                        texts.append(text)
        if texts:
            meta = {
                "source": file_path,
                "type": "presentation",
                "slide": slide_num,
                "speaker": speaker,
                **{k: v for k, v in file_meta.items() if k != "_comment"},
            }
            content = make_context_header(meta) + "\n".join(texts)
            documents.append(Document(page_content=content, metadata=meta))
    return documents


def parse_text(file_path: str) -> list[Document]:
    """Parse plain text or markdown files."""
    text = Path(file_path).read_text(encoding="utf-8")
    speaker = extract_speaker_name(file_path)
    file_meta = get_file_metadata(file_path)
    meta = {
        "source": file_path,
        "type": "text",
        "speaker": speaker,
        **{k: v for k, v in file_meta.items() if k != "_comment"},
    }
    content = make_context_header(meta) + text
    return [Document(page_content=content, metadata=meta)]


def is_otter_transcript(file_path: str) -> bool:
    """Detect if a .txt file is an Otter.ai transcript."""
    name = Path(file_path).name.lower()
    if "_otter_ai" in name:
        return True
    # Check first few lines for Otter format
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            head = f.read(500)
        return bool(re.search(r"Unknown Speaker\s{2,}\d+:\d{2}", head))
    except Exception:
        return False


def load_document(file_path: str) -> list[Document]:
    """Load and parse a document based on its extension."""
    path = Path(file_path)
    ext = path.suffix.lower()

    if ext in (".vtt", ".srt"):
        text = path.read_text(encoding="utf-8")
        return parse_vtt_srt(text, str(path))
    elif ext == ".pdf":
        return parse_pdf(str(path))
    elif ext == ".pptx":
        return parse_pptx(str(path))
    elif ext == ".txt":
        if is_otter_transcript(str(path)):
            text = path.read_text(encoding="utf-8")
            return parse_otter_transcript(text, str(path))
        return parse_text(str(path))
    elif ext == ".md":
        return parse_text(str(path))
    else:
        console.print(f"[yellow]Skipping unsupported file: {path.name}[/yellow]")
        return []


def chunk_documents(documents: list[Document]) -> list[Document]:
    """Split documents into smaller chunks for embedding."""
    # Transcripts are already chunked by timestamp/speaker
    already_chunked = [d for d in documents if d.metadata.get("type") == "transcript"]
    needs_chunking = [d for d in documents if d.metadata.get("type") != "transcript"]

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    chunked = splitter.split_documents(needs_chunking)
    return already_chunked + chunked


def ingest_files(file_paths: list[str]) -> int:
    """Ingest a list of files into the vector store. Returns number of chunks indexed."""
    all_docs = []
    with Progress() as progress:
        task = progress.add_task("Loading documents...", total=len(file_paths))
        for fp in file_paths:
            docs = load_document(fp)
            all_docs.extend(docs)
            progress.update(task, advance=1)

    if not all_docs:
        console.print("[yellow]No documents to ingest.[/yellow]")
        return 0

    console.print(f"Loaded {len(all_docs)} raw segments from {len(file_paths)} files")

    # Enrich documents with video URLs
    video_count = 0
    for doc in all_docs:
        source = doc.metadata.get("source", "")
        video_url = get_video_url(source)
        if video_url:
            doc.metadata["video_url"] = video_url
            video_count += 1
    if video_count:
        console.print(f"[blue]Attached video URLs to {video_count} segments[/blue]")

    # Chunk
    chunks = chunk_documents(all_docs)
    console.print(f"Split into {len(chunks)} chunks")

    # Embed and store using ChromaDB's built-in embeddings (free, local)
    console.print("Embedding and indexing (local model)...")
    vectorstore = Chroma(
        collection_name=config.COLLECTION_NAME,
        persist_directory=str(config.VECTORSTORE_DIR),
    )

    # Add in batches
    batch_size = 100
    with Progress() as progress:
        task = progress.add_task("Indexing chunks...", total=len(chunks))
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            vectorstore.add_documents(batch)
            progress.update(task, advance=len(batch))

    console.print(f"[green]Indexed {len(chunks)} chunks into vector store.[/green]")
    return len(chunks)


def ingest_directory(directory: str) -> int:
    """Ingest all supported files from a directory."""
    # Load file metadata (dates, events) before ingesting
    load_file_metadata(directory)
    # Load video links (transcript → video URL mapping)
    load_video_links(str(Path(directory).parent))

    path = Path(directory)
    supported = {".txt", ".md", ".vtt", ".srt", ".pdf", ".pptx"}
    files = [str(f) for f in path.rglob("*") if f.suffix.lower() in supported]

    if not files:
        console.print(f"[yellow]No supported files found in {directory}[/yellow]")
        return 0

    console.print(f"Found {len(files)} files in {directory}")
    return ingest_files(files)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        console.print("Usage: python ingest.py <file_or_directory> [file2] [file3] ...")
        console.print("Supported formats: .txt, .md, .vtt, .srt, .pdf, .pptx")
        sys.exit(1)

    paths = sys.argv[1:]
    total_chunks = 0

    for p in paths:
        path = Path(p)
        if path.is_dir():
            total_chunks += ingest_directory(str(path))
        elif path.is_file():
            total_chunks += ingest_files([str(path)])
        else:
            console.print(f"[red]Not found: {p}[/red]")

    console.print(f"\n[bold green]Done! Total chunks indexed: {total_chunks}[/bold green]")
