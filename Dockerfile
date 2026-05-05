FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn flask-cors

# Copy application code
COPY *.py ./

# Copy data for ingestion at build time
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

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "120", "--workers", "1", "web:app"]
