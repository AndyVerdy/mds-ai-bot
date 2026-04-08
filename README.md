# MDS AI Bot

RAG-powered knowledge assistant for Million Dollar Sellers (MDS) content. Searches through video transcripts and presentation decks to answer questions with citations.

## Stack

- **LLM**: Claude Sonnet 4 (Anthropic)
- **Embeddings**: ChromaDB built-in (all-MiniLM-L6-v2, local, free)
- **Vector Store**: ChromaDB
- **Web UI**: Flask
- **CLI**: Rich

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env
# Optional for Slack bot support:
# - SLACK_BOT_TOKEN
# - SLACK_SIGNING_SECRET
```

## Usage

```bash
# Ingest documents
python3 bot.py ingest data/

# Ask a question
python3 bot.py ask "What did Josh Hadley talk about?"

# Interactive chat
python3 bot.py chat

# Start web UI
python3 bot.py web

# Check knowledge base status
python3 bot.py status

# Reset knowledge base
python3 bot.py reset
```

## Web UI

Start with `python3 bot.py web`, then open http://localhost:5000.

## Slack Bot

The app can also receive Slack Events API requests at `/slack/events`.

Required environment variables for Slack:

- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`

For production deploys, set those values in your hosting provider instead of committing them to the repo.

## Tests

```bash
python3 tests.py
```

## Supported file types

- `.txt` — Plain text and Otter.ai transcripts (auto-detected)
- `.vtt` / `.srt` — Subtitle files with timestamps
- `.pdf` — PDF documents
- `.pptx` — PowerPoint presentations
- `.md` — Markdown files

## How it works

1. **Ingest**: Documents are parsed, speaker names extracted from filenames, split into chunks (~2000 chars), embedded locally, and stored in ChromaDB
2. **Query**: Question is embedded, top-5 similar chunks retrieved, sent to Claude with instructions to answer only from context
3. **Confidence**: Based on cosine distance from ChromaDB — helps detect when the knowledge base lacks relevant data
