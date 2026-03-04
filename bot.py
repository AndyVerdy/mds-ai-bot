#!/usr/bin/env python3
"""
MDS AI Bot — Main CLI entry point.

Usage:
    python bot.py ingest <file_or_dir> [file2] ...    Ingest documents into the knowledge base
    python bot.py ask "your question here"             Ask a single question
    python bot.py chat                                 Interactive Q&A mode
    python bot.py status                               Show knowledge base stats
    python bot.py reset                                Clear the knowledge base
    python bot.py web                                  Start web UI (http://localhost:5000)
"""

import sys
import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table

import config

console = Console()


def cmd_ingest(paths: list[str]):
    from ingest import ingest_files, ingest_directory

    total = 0
    for p in paths:
        path = Path(p)
        if path.is_dir():
            total += ingest_directory(str(path))
        elif path.is_file():
            total += ingest_files([str(path)])
        else:
            console.print(f"[red]Not found: {p}[/red]")

    console.print(f"\n[bold green]Done! Total chunks indexed: {total}[/bold green]")


def cmd_ask(question: str):
    from query import ask
    from rich.markdown import Markdown

    result = ask(question, verbose=True)
    console.print()
    console.print(Markdown(result["answer"]))
    conf = result["confidence"]
    conf_color = "green" if conf > 0.6 else "yellow" if conf > 0.3 else "red"
    console.print(f"\n[{conf_color}]Confidence: {conf:.0%}[/{conf_color}]")


def cmd_chat():
    from query import interactive
    interactive()


def cmd_status():
    if not config.VECTORSTORE_DIR.exists():
        console.print("[yellow]No knowledge base found. Run 'python bot.py ingest' first.[/yellow]")
        return

    from langchain_community.vectorstores import Chroma

    vectorstore = Chroma(
        collection_name=config.COLLECTION_NAME,
        persist_directory=str(config.VECTORSTORE_DIR),
    )

    collection = vectorstore._collection
    count = collection.count()

    if count == 0:
        console.print("[yellow]Knowledge base is empty.[/yellow]")
        return

    # Get source file stats
    results = collection.get(include=["metadatas"])
    sources = {}
    for meta in results["metadatas"]:
        src = meta.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1

    table = Table(title="Knowledge Base Status")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Total chunks", str(count))
    table.add_row("Source files", str(len(sources)))

    console.print(table)
    console.print()

    src_table = Table(title="Indexed Files")
    src_table.add_column("File", style="cyan")
    src_table.add_column("Chunks", style="green")
    for src, cnt in sorted(sources.items()):
        src_table.add_row(Path(src).name, str(cnt))

    console.print(src_table)


def cmd_reset():
    if config.VECTORSTORE_DIR.exists():
        shutil.rmtree(config.VECTORSTORE_DIR)
        console.print("[green]Knowledge base cleared.[/green]")
    else:
        console.print("[yellow]No knowledge base to clear.[/yellow]")


def main():
    if len(sys.argv) < 2:
        console.print(__doc__)
        return

    command = sys.argv[1].lower()

    if command == "ingest":
        if len(sys.argv) < 3:
            console.print("Usage: python bot.py ingest <file_or_dir> [file2] ...")
            return
        cmd_ingest(sys.argv[2:])

    elif command == "ask":
        if len(sys.argv) < 3:
            console.print("Usage: python bot.py ask \"your question here\"")
            return
        cmd_ask(" ".join(sys.argv[2:]))

    elif command == "chat":
        cmd_chat()

    elif command == "status":
        cmd_status()

    elif command == "reset":
        cmd_reset()

    elif command == "web":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
        console.print(f"[bold blue]Starting web UI at http://localhost:{port}[/bold blue]")
        from web import app
        app.run(host="0.0.0.0", port=port, debug=True)

    else:
        console.print(f"[red]Unknown command: {command}[/red]")
        console.print(__doc__)


if __name__ == "__main__":
    main()
