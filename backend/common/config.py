"""Environment-driven configuration for the ingestion worker and shared logic.

Values are read once at import time. Tests re-import this module per case to
pick up overridden environment variables (see tests/conftest.py).

The chat API (server.py) keeps its own configuration block so its existing
tests remain stable; this module is the configuration surface for the
event-driven ingestion Lambda.
"""
import os

DEFAULT_AWS_REGION = os.getenv("DEFAULT_AWS_REGION", "eu-central-1")

# Embedding (SageMaker) + vector store (S3 Vectors)
SAGEMAKER_ENDPOINT = os.getenv("SAGEMAKER_ENDPOINT", "")
VECTOR_BUCKET = os.getenv("VECTOR_BUCKET", "")
VECTOR_INDEX = os.getenv("VECTOR_INDEX", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

# Chunking
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))

# Ingestion limits / concurrency
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "1048576"))
INGESTION_MAX_WORKERS = max(1, min(8, int(os.getenv("INGESTION_MAX_WORKERS", "4"))))

# Supported upload file extensions (comma-separated env var, e.g. ".md,.txt")
SUPPORTED_SUFFIXES: frozenset = frozenset(
    s.strip() for s in os.getenv("SUPPORTED_SUFFIXES", ".md").split(",") if s.strip()
)

# SageMaker invocation resilience. The serverless embedding endpoint has a
# small max-concurrency, so concurrent InvokeEndpoint calls can be throttled.
# Adaptive retries with a generous attempt budget smooth out those bursts.
EMBEDDING_MAX_ATTEMPTS = max(1, int(os.getenv("EMBEDDING_MAX_ATTEMPTS", "10")))

# Ingestion pipeline resources
MANIFEST_TABLE = os.getenv("MANIFEST_TABLE", "")
RAG_DOCS_BUCKET = os.getenv("RAG_DOCS_BUCKET", "")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
