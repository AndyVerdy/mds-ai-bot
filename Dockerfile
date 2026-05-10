FROM python:3.12-slim

WORKDIR /app

# Install dependencies (cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn flask-cors

# Copy ONLY the files the embed step needs: config.py (chunk size,
# collection name, vectorstore dir) + ingest.py (the chunker itself) +
# the data/ tree. Keeping these in their own layer means changes to
# query.py / auth.py / web.py / bot.py / etc. don't invalidate the
# expensive embed step below — backend-only deploys go from ~40 min
# to ~1 min.
#
# config.py is here (not in the bottom COPY) because changing
# CHUNK_SIZE / COLLECTION_NAME / CHUNK_OVERLAP without re-embedding
# would silently corrupt retrieval. CONFIDENCE_THRESHOLD changes also
# trigger a re-embed — that's fine, we'd rather pay 40 min than risk a
# stale vectorstore. The savings are for code-only iterations.
COPY config.py ingest.py ./
COPY data/ ./data/

# Build the vectorstore during image build.
# Step 1: Otter transcripts (479 files → ~9800 chunks).
RUN python3 -c "from ingest import ingest_directory; ingest_directory('data/otter-export-new/')"

# Step 2: WhatsApp digests pulled from Airtable Summaries.raw_log.
# Render auto-forwards service env vars to docker build args when the
# Dockerfile declares matching ARG. Without this declaration, AIRTABLE_PAT
# is invisible at build time and ingest_whatsapp silently returns 0 chunks.
ARG AIRTABLE_PAT
ENV AIRTABLE_PAT=${AIRTABLE_PAT}
RUN python3 -c "import os; print('[BUILD] AIRTABLE_PAT visible during build:', bool(os.getenv('AIRTABLE_PAT')))"
RUN python3 -c "from ingest import ingest_whatsapp; ingest_whatsapp()"

# Step 3: MDS Video Library transcripts pulled from Postgres
# `transcript_segments` (joined per-video). Same pattern as WA — service
# env vars must be ARG-declared to be visible at build time. The
# `videos.py` module reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.
#
# Why bake this at build time instead of running it on first request:
# (a) Render Starter (512MB) can't load the embedding model AND embed
#     ~400 chunks while serving /api/health within gunicorn's 120s
#     timeout — the worker dies and the chunks never commit. Verified
#     2026-05-10 with two separate failures (Render Shell nohup and a
#     web-process daemon thread, both silent OOM kills).
# (b) Every deploy rebuilds the ChromaDB-backed image fresh anyway —
#     so chunks live in the image, not in a runtime mount.
# (c) Build containers have looser memory limits than runtime workers.
#
# New videos uploaded BETWEEN deploys still flow in via the auto-ingest
# hook in transcripts.handle_webhook (commit 65144b3) — that runs in
# the worker but only embeds one video's worth of chunks at a time, so
# memory pressure stays low.
ARG SUPABASE_URL
ARG SUPABASE_SERVICE_ROLE_KEY
ENV SUPABASE_URL=${SUPABASE_URL}
ENV SUPABASE_SERVICE_ROLE_KEY=${SUPABASE_SERVICE_ROLE_KEY}
RUN python3 -c "import os; print('[BUILD] SUPABASE_SERVICE_ROLE_KEY visible during build:', bool(os.getenv('SUPABASE_SERVICE_ROLE_KEY')))"
# `|| true` so the build doesn't fail if the videos table is empty
# (e.g. brand-new deployment without uploads). On subsequent deploys
# with content, the chunks bake in.
RUN python3 -c "from ingest import ingest_videos; ingest_videos()" || true

# Copy the rest of the application code. This layer is invalidated by
# any *.py change but every layer above is cached, so the build skips
# straight from re-COPY to gunicorn.
COPY *.py ./

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "120", "--workers", "1", "web:app"]
