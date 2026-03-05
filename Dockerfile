FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn flask-cors

# Copy application code
COPY *.py ./

# Copy data for ingestion at build time
COPY data/ ./data/

# Build the vectorstore during image build (331 transcripts → ~9800 chunks)
RUN python3 -c "from ingest import ingest_directory; ingest_directory('data/otter-export-new/')"

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "120", "--workers", "1", "web:app"]
