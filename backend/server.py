from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Header, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import os
import logging
import re
import hmac
from dotenv import load_dotenv
from typing import Annotated, Optional, List, Dict, Tuple
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from common import chunking, embeddings, vector_store, config
from common.access import (
    Scope,
    StaticScopeResolver,
    UnknownPrincipalError,
    build_vector_filter,
    is_authorized,
)
from common.doc_keys import (
    DocumentFormatError,
    InvalidDocumentKeyError,
    build_vector_id,
    parse_document_key,
)
from common.models import SourceItem
from common.prepare import build_chunk_metadata, prepare_document
from common.registry import Registry, RegistryError, load_registry
from context import prompt, rewrite_prompt

# Load environment variables
load_dotenv()

app = FastAPI()

# Configure CORS
origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

DEFAULT_AWS_REGION = os.getenv("DEFAULT_AWS_REGION", "eu-central-1")

# Adaptive retries absorb throttling from the small-concurrency serverless
# embedding endpoint instead of failing the request after a few attempts.
EMBEDDING_MAX_ATTEMPTS = max(1, int(os.getenv("EMBEDDING_MAX_ATTEMPTS", "10")))

# Initialize SageMaker client
sagemaker_client = boto3.client(
    service_name = "sagemaker-runtime",
    region_name=DEFAULT_AWS_REGION,
    config=BotoConfig(
        retries={"max_attempts": EMBEDDING_MAX_ATTEMPTS, "mode": "adaptive"}
    )
)

# Initialize S3Vectors client
s3vectors_client = boto3.client(
    service_name="s3vectors",
    region_name=DEFAULT_AWS_REGION
)

# Initialize Bedrock client
bedrock_client = boto3.client(
    service_name="bedrock-runtime",
    region_name=DEFAULT_AWS_REGION
)

# Bedrock model selection
# Available aws-trained foundation models:
# - amazon.nova-micro-v1:0  (fastest, cheapest)
# - amazon.nova-lite-v1:0   (balanced - default)
# - amazon.nova-pro-v1:0    (most capable, higher cost)
# you might need to add us. or eu. prefix to the below model id to get the regional inference profile
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "eu.amazon.nova-pro-v1:0")
BEDROCK_LIGHT_MODEL_ID = os.getenv("BEDROCK_LIGHT_MODEL_ID", "eu.amazon.nova-micro-v1:0")

# Memory storage configuration
USE_S3 = os.getenv("USE_S3", "false").lower() == "true"
S3_BUCKET = os.getenv("S3_BUCKET", "")
MEMORY_DIR = os.getenv("MEMORY_DIR", "../memory")

# SageMaker endpoint configuration (for embeddings)
SAGEMAKER_ENDPOINT = os.getenv("SAGEMAKER_ENDPOINT", "")

# S3Vectors configuration
VECTOR_BUCKET = os.getenv("VECTOR_BUCKET", "")
VECTOR_INDEX = os.getenv("VECTOR_INDEX", "")

# RAG configuration
RAG_ENABLED = os.getenv("RAG_ENABLED", "true").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_RETRIEVAL_DISTANCE = os.getenv("MAX_RETRIEVAL_DISTANCE", "").strip()
MAX_RETRIEVAL_DISTANCE_VALUE = float(MAX_RETRIEVAL_DISTANCE) if MAX_RETRIEVAL_DISTANCE else None
SOURCE_SNIPPET_CHARS = int(os.getenv("SOURCE_SNIPPET_CHARS", "280"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "1500"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
RAW_FETCH_SIZE = int(os.getenv("RAW_FETCH_SIZE", "12"))
FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "3"))
MAX_CHUNKS_PER_DOC = int(os.getenv("MAX_CHUNKS_PER_DOC", "2"))

# ADMIN configuration
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
LOCAL_DEV = os.getenv("LOCAL_DEV", "false").lower() == "true"

# Request Configuration
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "3000"))
SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
TENANT_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"

# Ingestion file configuration
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "1048576"))
INGESTION_MAX_WORKERS = max(1, min(8, int(os.getenv("INGESTION_MAX_WORKERS", "4"))))

# Set up logging
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# Initialize S3 client if needed
if USE_S3:
    s3_client = boto3.client("s3")

# Who may see which documents (ADR 0002): the synthetic registry maps tenants to
# leases and properties. It ships in the Lambda ZIP with data/. An unusable file
# must not crash the API: the registry then stays empty and every chat request is
# refused (fail closed), visible in the log and in /health.
REGISTRY_PATH = Path(os.getenv("REGISTRY_PATH") or Path(__file__).resolve().parent / "data" / "registry.json")
_EMPTY_REGISTRY = Registry(
    properties=frozenset(), lease_property={}, tenant_leases={}, staff=frozenset()
)

def _load_registry(path: Path) -> Registry:
    try:
        return load_registry(path)
    except RegistryError as exc:
        logger.error(
            "Scope registry %s is unusable; every chat request will be refused: %s",
            path, "; ".join(exc.problems),
        )
        return _EMPTY_REGISTRY

REGISTRY: Registry = _load_registry(REGISTRY_PATH)
SCOPE_RESOLVER = StaticScopeResolver(REGISTRY)

def _is_known_principal(principal: str) -> bool:
    return REGISTRY.is_staff(principal) or principal in REGISTRY.tenant_leases

def _resolve_public_chat_tenant(raw: str) -> str:
    """Validate the one tenant whose scope anonymous /chat callers receive.

    Fails closed: it must be a registered *tenant* (never staff), otherwise
    anonymous chat is disabled instead of widened.
    """
    value = raw.strip()
    if not value:
        return ""
    if value not in REGISTRY.tenant_leases:
        logger.error(
            "PUBLIC_CHAT_TENANT_ID is not a registered tenant; anonymous chat stays disabled"
        )
        return ""
    return value

# P0 containment (docs/adr/0003): anonymous /chat callers are pinned to this
# single, synthetic-only tenant, and receive exactly that tenant's scope (its own
# leases, its properties and the company-wide documents; never staff documents).
# Empty means anonymous chat is disabled.
PUBLIC_CHAT_TENANT_ID = _resolve_public_chat_tenant(os.getenv("PUBLIC_CHAT_TENANT_ID", ""))

# Request/Response models
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)
    session_id: Optional[str] = Field(
        default=None,
        pattern=SESSION_ID_PATTERN,
        max_length=64
    )
    # Optional on purpose: the server, not the body, decides the effective scope
    # (see resolve_chat_scope). An omitted value must never mean "admin" for an
    # anonymous caller. Only the *shape* is validated here; whether the tenant
    # exists is decided after privilege is known, so an anonymous caller cannot
    # tell registered tenants from unregistered ones (uniform 403).
    tenant_id: Optional[str] = Field(
        default=None,
        pattern=TENANT_ID_PATTERN,
    )

class ChatResponse(BaseModel):
    response: str
    session_id: str
    tenant_id: str
    sources: List[SourceItem] = Field(default_factory=list) # List of source objects; defaults to a fresh empty list
    retrieval_used: bool = False

class Message(BaseModel):
    role: str
    content: str
    timestamp: str

# Embedding model
class EmbedRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)

# Memory management functions
def get_memory_path(session_id: str) -> str:
    safe_session_id = normalize_session_id(session_id)
    return f"sessions/{safe_session_id}.json"

def load_conversation(session_id: str) -> Tuple[Optional[str], List[Dict]]:
    """Load conversation history from storage.

    Returns:
        A ``(stored_tenant_id, messages)`` tuple.  ``stored_tenant_id`` is
        *None* when the session does not exist yet (new session).
    """
    if USE_S3:
        try:
            response = s3_client.get_object(Bucket=S3_BUCKET, Key=get_memory_path(session_id))
            raw = json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None, []
            raise
    else:
        # Local file storage
        file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        if not os.path.exists(file_path):
            return None, []
        with open(file_path, "r") as f:
            raw = json.load(f)

    # Support both legacy format (plain list) and new format ({tenant_id, messages})
    if isinstance(raw, list):
        return None, raw
    return raw.get("tenant_id"), raw.get("messages", [])

def save_conversation(session_id: str, messages: List[Dict], tenant_id: str = config.DEFAULT_TENANT_ID):
    """Save conversation history to storage"""
    data = {"tenant_id": tenant_id, "messages": messages}
    if USE_S3:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=get_memory_path(session_id),
            Body=json.dumps(data, indent=2),
            ContentType="application/json",
        )
    else:
        # Local file storage
        os.makedirs(MEMORY_DIR, exist_ok=True)
        file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2)

def normalize_session_id(session_id: str) -> str:
    if not re.fullmatch(SESSION_ID_PATTERN, session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    return session_id

# Admin API functions
def require_admin_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """Small PoC admin gate for non-browser operational endpoints.

    Enterprise prod version should use OIDC/SAML/Cognito and user/tenant claims. Alternatively can use REST via API Gateway and API Key in the medium term.
    """
    if not ADMIN_API_KEY:
        if LOCAL_DEV:
            logger.warning("ADMIN_API_KEY is not configured; admin endpoint is open because LOCAL_DEV=true")
            return
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Admin API key not configured")
    if not _admin_key_matches(x_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")

def _admin_key_matches(x_api_key: Optional[str]) -> bool:
    """Constant-time key check. Bytes keep non-ASCII input from raising TypeError."""
    if not x_api_key or not ADMIN_API_KEY:
        return False
    return hmac.compare_digest(x_api_key.encode("utf-8"), ADMIN_API_KEY.encode("utf-8"))

def is_privileged_chat_caller(x_api_key: Optional[str]) -> bool:
    """Whether /chat may honour a caller-chosen tenant scope (including ``admin``).

    - ``ADMIN_API_KEY`` unset: only ``LOCAL_DEV=true`` is privileged; a header is ignored.
    - ``ADMIN_API_KEY`` set: only a matching ``x-api-key``. A supplied but wrong key is
      a 401 (not a silent downgrade); an absent key is an anonymous caller.
    """
    if not ADMIN_API_KEY:
        return LOCAL_DEV
    if not x_api_key:
        return False
    if not _admin_key_matches(x_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
    return True

def resolve_chat_scope(
    requested: Optional[str], stored: Optional[str], privileged: bool
) -> Tuple[str, Scope]:
    """Server-side scope decision for /chat. The request body never grants scope.

    Returns ``(principal, scope)``. Anonymous callers only ever get the scope of
    ``PUBLIC_CHAT_TENANT_ID``; asking for any other tenant (registered or not),
    or reusing a session bound to another tenant, is refused with the same 403
    before any retrieval or model call, so the response does not reveal which
    tenants exist. Privileged callers (admin key, or LOCAL_DEV without a key) may
    choose a registered principal, including a staff one; an existing session's
    stored principal wins over the request.
    """
    denied = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access to the requested scope is not permitted",
    )
    if privileged:
        if stored is not None:
            principal = stored
        else:
            principal = requested or config.DEFAULT_TENANT_ID
            if requested is not None and not _is_known_principal(requested):
                raise HTTPException(
                    status_code=422,
                    detail="Unknown tenant_id",
                )
    else:
        if not PUBLIC_CHAT_TENANT_ID:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Chat is not available",
            )
        for value in (requested, stored):
            if value is not None and value != PUBLIC_CHAT_TENANT_ID:
                # Same message for request and session mismatches: no oracle for
                # which sessions or tenants exist.
                raise denied
        principal = PUBLIC_CHAT_TENANT_ID

    try:
        return principal, SCOPE_RESOLVER.resolve(principal)
    except UnknownPrincipalError:
        # e.g. a stored session whose tenant was removed from the registry
        raise denied from None

def call_bedrock(
        conversation: List[Dict],
        user_message: str,
        sources: Optional[List[SourceItem]] = None
) -> str:
    """Call AWS Bedrock with conversation history & optional RAG sources"""

    # Build messages in Bedrock format
    messages = []

    # Add conversation history (limit to last 10 exchanges as context)
    for msg in conversation[-20:]:  # Last 10 back-and-forth exchanges
        messages.append({
            "role": msg["role"],
            "content": [{"text": msg["content"]}]
        })

    # Add current user message
    messages.append({
        "role": "user",
        "content": [{"text": user_message}]
    })

    system_prompt = prompt()
    retrieval_block = build_retrieval_block(sources or [])

    if retrieval_block:
        system_prompt = f"{system_prompt}\n\n{retrieval_block}"

    try:
        # Call Bedrock using the converse API
        response = bedrock_client.converse(
            modelId=BEDROCK_MODEL_ID,
            system = [{"text": system_prompt}],
            messages=messages,
            inferenceConfig={
                "maxTokens": 2000,
                "temperature": 0.7,
                "topP": 0.9
            }
        )

        # Extract the response text
        return response["output"]["message"]["content"][0]["text"]

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'ValidationException':
            # Handle message format issues
            print(f"Bedrock validation error: {e}")
            raise HTTPException(status_code=400, detail="Invalid message format for Bedrock")
        elif error_code == 'AccessDeniedException':
            print(f"Bedrock access denied: {e}")
            raise HTTPException(status_code=403, detail="Access denied to Bedrock model")
        else:
            print(f"Bedrock error: {e}")
            raise HTTPException(status_code=500, detail=f"Bedrock error: {str(e)}")

def get_embedding(text: str) -> List[float]:
    if not SAGEMAKER_ENDPOINT:
        raise HTTPException(status_code=500, detail="SAGEMAKER ENDPOINT not configured")

    return embeddings.embed_text(text, client=sagemaker_client, endpoint=SAGEMAKER_ENDPOINT)

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
    """Yield markdown-aware text chunks (see common.chunking.chunk_text)."""
    return chunking.chunk_text(text, size=size, overlap=overlap)



def _document_group_key(metadata: Dict) -> str:
    """Return an internal identifier used to group chunks by source document.

    Vectors written under ADR 0002 carry ``source_key`` (the scope-qualified S3
    key, unique per document *and business version*, so same-named documents in
    different leases, properties or audiences stay distinct). ``source_key`` is
    used for grouping only and is never returned to clients or put in the
    prompt, because it exposes the internal bucket layout.

    ``source_path`` and ``title`` remain as fallbacks. Vectors from before
    ADR 0002 (``tenant_id`` metadata) never reach this function: they lack the
    audience and effective-date fields, so the retrieval filter and the per-hit
    check exclude them for every principal.
    """
    return (
        metadata.get("source_path")
        or metadata.get("source_key")
        or metadata.get("title")
        or "unknown"
    )


def index_text_chunk(
    text: str,
    vector_id: str,
    metadata: Dict
) -> str:
    if not VECTOR_BUCKET or not VECTOR_INDEX:
        raise HTTPException(status_code=500, detail="VECTOR_BUCKET or VECTOR_INDEX not configured")

    embedding = get_embedding(text)

    return vector_store.put_vector(
        client=s3vectors_client,
        bucket=VECTOR_BUCKET,
        index=VECTOR_INDEX,
        vector_id=vector_id,
        embedding=embedding,
        metadata={"chunk_text": text, **metadata},
    )

def rewrite_query(conversation: List[Dict], user_message: str) -> str:
    """Uses LLM to turn conversational follow-ups into standalone search queries."""
    if not conversation:
        return user_message

    # Prepare context: combine last few turns for brevity
    context_text = ""
    for msg in conversation[-5:]: # Only the recent context
        context_text += f"{msg['role']}: {msg['content']}\n"

    messages = [{
        "role": "user",
        "content": [{"text": f"CONVERSATION HISTORY:\n{context_text}\n\nFOLLOW-UP: {user_message}"}]
    }]

    try:
        response = bedrock_client.converse(
            modelId=BEDROCK_LIGHT_MODEL_ID,
            system=[{"text": rewrite_prompt()}],
            messages=messages,
            inferenceConfig={"maxTokens": 100, "temperature": 0}
        )
        rewritten = response["output"]["message"]["content"][0]["text"].strip()
        logger.info(f"Query Rewritten: '{user_message}' -> '{rewritten}'")
        return rewritten
    except Exception as e:
        logger.warning(f"Query rewrite failed, falling back to original: {e}")
        return user_message

def search_text_chunks(
    query: str,
    top_k: int = 5,
    metadata_filter: Optional[Dict] = None,
) -> List[Dict]:
    if not VECTOR_BUCKET or not VECTOR_INDEX:
        raise HTTPException(status_code=500, detail="VECTOR_BUCKET or VECTOR_INDEX not configured")

    query_embedding = get_embedding(query)

    return vector_store.query_vectors(
        client=s3vectors_client,
        bucket=VECTOR_BUCKET,
        index=VECTOR_INDEX,
        embedding=query_embedding,
        top_k=top_k,
        metadata_filter=metadata_filter,
    )

def is_rag_enabled() -> bool:
    return RAG_ENABLED and bool(SAGEMAKER_ENDPOINT and VECTOR_BUCKET and VECTOR_INDEX)

def get_lexical_score(query: str, document: str) -> float:
    """
    Calculate BM25-inspired lexical relevance score between query and document.

    This implementation uses BM25 concepts optimized for single-document scoring:
    - Term frequency (how often query terms appear in the document)
    - Saturation (diminishing returns for repeated terms)
    - Length normalization

    For real-world RAG retrieval, this is faster and more appropriate than
    full corpus-based BM25, while preserving the key benefits of considering
    term frequency and avoiding raw term overlap.

    Args:
        query: The search query text
        document: The document text to score

    Returns:
        A BM25-inspired relevance score between 0 and 1
    """
    # Tokenize query and document (lowercase, split on whitespace and punctuation)
    query_tokens = re.findall(r'\b\w+\b', query.lower())
    doc_tokens = re.findall(r'\b\w+\b', document.lower())

    if not query_tokens or not doc_tokens:
        return 0.0

    # Count term frequencies in the document
    doc_tf = {}
    for token in doc_tokens:
        doc_tf[token] = doc_tf.get(token, 0) + 1

    # BM25 parameters (tuned for single-doc scoring)
    k1 = 1.5  # Term frequency saturation (controls how much term frequency matters)
    b = 0.75  # Length normalization (controls effect of document length)

    # Average document length (for this case, it's just this doc's length)
    avg_doc_len = len(doc_tokens)
    doc_len = len(doc_tokens)

    # Calculate BM25-style score
    score = 0.0
    matched_terms = 0

    for query_term in query_tokens:
        if query_term in doc_tf:
            matched_terms += 1
            tf = doc_tf[query_term]

            # BM25 term frequency saturation
            # tf / (tf + k1 * (1 - b + b * (doc_len / avg_doc_len)))
            tf_component = (k1 + 1) * tf / (
                k1 * (1 - b + b * (doc_len / avg_doc_len)) + tf
            )

            # IDF approximation (for single doc, use log of query diversity)
            # This gives a small boost to queries with diverse terms
            idf = 1.0

            score += tf_component * idf

    # Normalize to 0-1 range
    # Maximum possible score is roughly len(query_tokens) * (k1 + 1)
    max_score = len(query_tokens) * (k1 + 1)
    normalized_score = min(score / max_score, 1.0)

    return normalized_score

def retrieve_sources(
        query: str,
        fetch_n: int = RAW_FETCH_SIZE,
        return_n: int = FINAL_TOP_K,
        *,
        scope: Scope,
) -> List[SourceItem]:
    """
        Retrieves sources using a two-stage process:
        1. Fetch a large candidate pool (fetch_n).
        2. Rerank by combined score and limit per-document chunks.

        ``scope`` is required and keyword-only: there is deliberately no way to
        retrieve without one. It is enforced twice (ADR 0002): the S3 Vectors
        metadata filter built from it keeps unauthorized vectors out of the
        result, and every returned hit is re-checked in code; a hit outside the
        scope is dropped and logged (without its content).
    """

    if not is_rag_enabled():
        logger.info("retrieval disabled")
        return []

    metadata_filter = build_vector_filter(scope)

    try:
        # The "Wide Net" fetch from Vector DB
        candidates = search_text_chunks(query, top_k=fetch_n, metadata_filter=metadata_filter)
        logger.info(f"Vector DB returned {len(candidates)} raw candidates.")
    except Exception as exc:
        logger.error(f"Vector search failed: {exc}", exc_info=True)
        return []

    raw_results = [hit for hit in candidates if is_authorized(hit.get("metadata"), scope)]
    if len(raw_results) != len(candidates):
        logger.warning(
            "Dropped %d retrieved vector(s) outside the caller's scope (role=%s, principal=%s)",
            len(candidates) - len(raw_results), scope.role, scope.principal,
        )

    # Scoring and Reranking
    scored_items = []
    for item in raw_results:
        metadata = item.get("metadata") or {}
        chunk_body = metadata.get("chunk_text") or ""
        distance = item.get("distance")

        if (
                MAX_RETRIEVAL_DISTANCE_VALUE is not None
                and distance is not None
                and distance > MAX_RETRIEVAL_DISTANCE_VALUE
        ):
            continue

        # Calculate scores
        lex_score = get_lexical_score(query, chunk_body)
        distance_for_score = distance if distance is not None else 1.0  # Assume 1.0 if missing (max distance)

        # Combine: Lower cosine distance is better, higher lexical is better
        # Invert distance (1-d) to get 'vector similarity'
        combined_score = (1.0 - distance_for_score) + (lex_score * 0.5)

        scored_items.append({
            "score": combined_score,
            "item": item,
            "doc_id": _document_group_key(metadata)
        })

    # Sort by the new combined score (highest first)
    scored_items.sort(key=lambda x: x["score"], reverse=True)

    # Diversification Filter
    final_sources: List[SourceItem] = []
    doc_counts = {}
    dropped_by_diversity = 0

    for entry in scored_items:
        if len(final_sources) >= return_n:
            break

        doc_id = entry["doc_id"]
        count = doc_counts.get(doc_id, 0)

        # Only add if the per-document limit isn't exceeded
        if count < MAX_CHUNKS_PER_DOC:
            doc_counts[doc_id] = count + 1
            item = entry["item"]
            metadata = item.get("metadata") or {}
            chunk_body = metadata.get("chunk_text", "")
            final_sources.append(
                SourceItem(
                    id=f"S{len(final_sources) + 1}",
                    vector_key=item.get("key"),
                    title=metadata.get("title"),
                    source_path=metadata.get("source_path"),
                    snippet=shorten_snippet(chunk_body),
                    context=shorten_snippet(chunk_body, MAX_CONTEXT_CHARS),
                    doc_type=metadata.get("doc_type"),
                    chunk_index=metadata.get("chunk_index"),
                    distance=item.get("distance"),
                )
            )
        else:
            dropped_by_diversity += 1

    logger.info(
        f"Retrieval complete: final_count={len(final_sources)}, "
        f"dropped_by_diversity={dropped_by_diversity}, "
        f"docs_represented={len(doc_counts)}"
    )

    return final_sources

def shorten_snippet(text: str, max_chars: int = SOURCE_SNIPPET_CHARS) -> str:
    """
        Normalizes whitespace and truncates text to a maximum character length.

        Args:
            text: The raw string to be cleaned and shortened.
            max_chars: The character limit, including the trailing ellipsis.

        Returns:
            A single-line string stripped of extra whitespace and truncated with '…'
            if it exceeds max_chars.
    """
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"

def build_retrieval_block(sources: List[SourceItem]) -> str:
    """
        Formats a list of retrieved sources into a structured text block for model grounding.

        This function constructs a system-level prompt section that provides the AI with
        factual context, explicit citation rules, and formatted source metadata. Each
        source is assigned a unique identifier (e.g., [S1], [S2]) for inline citation.

        Args:
            sources: A list of SourceItem objects containing the title, path, and text snippets.

        Returns:
            A formatted string containing the knowledge block and citation instructions,
            or an empty string if no sources are provided.
    """
    if not sources:
        return ""

    lines = [
        "## RETRIEVED KNOWLEDGE",
        "Use retrieved knowledge only when it is relevant to the latest user request.",
        "When you use a retrieved source, cite it inline as [S1], [S2], etc.",
        "Do not invent citations and do not cite sources you did not use.",
    ]

    for index, source in enumerate(sources, start=1):
        header = source.title or source.source_path or source.id
        lines.append(f"[S{index}] {header}")

        if source.source_path:
            lines.append(f"Path: {source.source_path}")

        lines.append(source.context or source.snippet)

    return "\n".join(lines)

@app.get("/")
async def root():
    return {
        "message": "KI Immobilien-Assistentin für XYZ Immobilien",
        "memory_enabled": True,
        "storage": "S3" if USE_S3 else "local",
        "ai_model": BEDROCK_MODEL_ID
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "use_s3": USE_S3,
        "bedrock_model": BEDROCK_MODEL_ID,
        "sagemaker_endpoint_configure" : bool(SAGEMAKER_ENDPOINT),
        "s3vectors_configured" : bool(VECTOR_BUCKET and VECTOR_INDEX),
        "rag_enabled": is_rag_enabled(),
        "ingestion_api_enabled": LOCAL_DEV,
        "public_chat_enabled": bool(PUBLIC_CHAT_TENANT_ID),
        "registry_loaded": REGISTRY is not _EMPTY_REGISTRY,
    }

@app.post("/embed", dependencies=[Depends(require_admin_api_key)])
async def embed(request: EmbedRequest):
    return {"embedding": get_embedding(request.text)}

# /ingest has no manifest, so every local upload uses this fixed source version:
# re-uploading a key overwrites its own vector ids instead of piling up copies.
LOCAL_INGEST_SOURCE_VERSION = "local"

async def ingest_file(
    file: Annotated[UploadFile, File()],
    source_key: Annotated[str, Form(max_length=300)],
):
    # The target scope is the S3-style key, validated by the same grammar and the
    # same preparation code as the event-driven worker (ADR 0002), so a document
    # indexed here has exactly the metadata the worker would write.
    #
    # TRUST MODEL: the scope (audience, company/property/lease) is chosen by the
    # operator calling this local-only, admin-gated route, like the S3 key for the
    # worker. It is NOT a pattern for user-facing paths: with LOCAL_DEV=true and no
    # ADMIN_API_KEY it is plain request input. Production scope must come from
    # server-verified identity claims (CLAUDE.md §9.1, §9.4, §13.3.1).
    try:
        parse_document_key(source_key)
    except InvalidDocumentKeyError as exc:
        raise HTTPException(status_code=422, detail="; ".join(exc.problems))

    try:
        content_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File uploaded too large")

        try:
            prepared = prepare_document(
                source_key,
                content_bytes,
                max_bytes=MAX_UPLOAD_BYTES,
                chunk_size=CHUNK_SIZE,
                chunk_overlap=CHUNK_OVERLAP,
            )
        except DocumentFormatError as exc:
            raise HTTPException(status_code=400, detail="; ".join(exc.problems))

        ingested_at = datetime.now(timezone.utc).isoformat()
        futures = []
        with ThreadPoolExecutor(max_workers=INGESTION_MAX_WORKERS) as executor:
            for count, chunk in enumerate(prepared.chunks):
                vector_id = build_vector_id(prepared.key, LOCAL_INGEST_SOURCE_VERSION, count)
                metadata = build_chunk_metadata(
                    prepared,
                    chunk_index=count,
                    chunk_text=chunk,
                    source_version=LOCAL_INGEST_SOURCE_VERSION,
                    embedding_model=config.EMBEDDING_MODEL,
                    ingested_at=ingested_at,
                )
                futures.append(executor.submit(index_text_chunk, chunk, vector_id, metadata))

            chunks_processed = 0
            for future in as_completed(futures):
                future.result()
                chunks_processed += 1

        return {
            "source_key": source_key,
            "doc_id": prepared.key.doc_id,
            "doc_version": prepared.key.doc_version,
            "chunks_indexed": chunks_processed,
            "status": "success"
        }
    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Ingestion failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ingestion error: {str(e)}")

# Production ingestion runs in the dedicated event-driven worker Lambda.
# The synchronous /ingest endpoint is only exposed for local development.
if LOCAL_DEV:
    app.post("/ingest", dependencies=[Depends(require_admin_api_key)])(ingest_file)

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, x_api_key: Optional[str] = Header(default=None)):
    try:
        # Decide privilege first: a wrong key must fail before any session I/O.
        privileged = is_privileged_chat_caller(x_api_key)

        # Generate session ID if not provided
        session_id = request.session_id or str(uuid.uuid4())

        # Load conversation history; resolve effective tenant from stored session
        stored_tenant_id, conversation = load_conversation(session_id)
        # The server decides the scope (P0 containment). An existing session's
        # stored tenant is never widened; anonymous callers are pinned to
        # PUBLIC_CHAT_TENANT_ID and refused otherwise, before retrieval or Bedrock.
        effective_tenant_id, scope = resolve_chat_scope(
            request.tenant_id, stored_tenant_id, privileged
        )
        if not privileged and stored_tenant_id is None and conversation:
            # A legacy list-format session has no tenant binding; its history may come
            # from another scope (e.g. admin) and must not become model context here.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access to the requested scope is not permitted",
            )

        # Determine the search query (using history if it exists)
        search_query = rewrite_query(conversation, request.message)

        # Retrieve sources scoped to the effective tenant
        sources = retrieve_sources(search_query, scope=scope)

        # Call Bedrock for response
        assistant_response = call_bedrock(
            conversation,
            request.message,
            sources= sources
        )

        # Update conversation history
        conversation.append(
            {"role": "user", "content": request.message, "timestamp": datetime.now().isoformat()}
        )
        conversation.append(
            {
                "role": "assistant",
                "content": assistant_response,
                "timestamp": datetime.now().isoformat(),
            }
        )

        # Save conversation with the effective tenant
        save_conversation(session_id, conversation, tenant_id=effective_tenant_id)

        return ChatResponse(
            response=assistant_response,
            session_id=session_id,
            tenant_id=effective_tenant_id,
            sources=sources,
            retrieval_used=bool(sources)
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in chat endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/conversation/{session_id}", dependencies=[Depends(require_admin_api_key)])
async def get_conversation(session_id: str):
    """Retrieve conversation history"""
    try:
        _, conversation = load_conversation(session_id)
        return {"session_id": session_id, "messages": conversation}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)