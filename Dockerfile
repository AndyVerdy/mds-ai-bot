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

# Step 3: MDS Video Library transcripts.
#
# Source: `data/video_segments_baked.json` — a snapshot of every
# `videos` row + its `transcript_segments` from Postgres. Regenerated
# periodically (manually or by an admin tool) by querying Supabase
# directly and dumping the result. The JSON is checked into git.
#
# Why JSON instead of pulling from Postgres at build time:
# Render auto-forwards service env vars to docker build args ONLY
# when explicitly declared as ARG. For SUPABASE_SERVICE_ROLE_KEY this
# was failing silently — build container couldn't reach the DB, the
# `|| true` swallowed the error, and image deployed with 0 video
# chunks indexed. Switching to a checked-in JSON eliminates the
# build-time env-var dependency entirely; build is now reproducible
# from the repo state alone.
#
# Why bake at build time at all:
# (a) Render Starter (512MB) can't load the embedding model AND embed
#     ~400 chunks while serving /api/health within gunicorn's 120s
#     timeout — the worker dies and the chunks never commit. Verified
#     2026-05-10 with two separate failures (Render Shell nohup and a
#     web-process daemon thread, both silent OOM kills).
# (b) Every deploy rebuilds the ChromaDB-backed image fresh — chunks
#     live in the image, not in a runtime mount.
# (c) Build containers have looser memory limits than runtime workers.
#
# New videos uploaded BETWEEN deploys still flow in via the auto-ingest
# hook in transcripts.handle_webhook (commit 65144b3) — that runs in
# the worker but only embeds one video's worth of chunks at a time, so
# memory pressure stays low. To make those new uploads survive a deploy
# rebuild, regenerate `data/video_segments_baked.json` and commit it.
RUN python3 -c "from ingest import ingest_videos_from_json; print('[BUILD] video chunks added from JSON:', ingest_videos_from_json())"

# Copy the rest of the application code. This layer is invalidated by
# any *.py change but every layer above is cached, so the build skips
# straight from re-COPY to gunicorn.
COPY *.py ./

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "120", "--workers", "1", "web:app"]
