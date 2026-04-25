from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import os
import logging
from dotenv import load_dotenv
from typing import Optional, List, Dict
import json
import uuid
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
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

# Initialize SageMaker client
sagemaker_client = boto3.client(
    service_name = "sagemaker-runtime",
    region_name=DEFAULT_AWS_REGION
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
RETRIEVAL_TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "3"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_RETRIEVAL_DISTANCE = os.getenv("MAX_RETRIEVAL_DISTANCE", "").strip()
SOURCE_SNIPPET_CHARS = int(os.getenv("SOURCE_SNIPPET_CHARS", "280"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
RAW_FETCH_SIZE = int(os.getenv("RAW_FETCH_SIZE", "12"))
FINAL_TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "4"))
MAX_CHUNKS_PER_DOC = int(os.getenv("MAX_CHUNKS_PER_DOC", "2"))

# Set up logging
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# Initialize S3 client if needed
if USE_S3:
    s3_client = boto3.client("s3")

# Request/Response models
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

class SourceItem(BaseModel):
    id: str
    title: Optional[str] = None
    source_path: Optional[str] = None
    snippet: str
    doc_type: Optional[str] = None
    chunk_index: Optional[int] = None
    distance: Optional[float] = None

class ChatResponse(BaseModel):
    response: str
    session_id: str
    sources: List[SourceItem] = Field(default_factory=list) # List of source objects; defaults to a fresh empty list
    retrieval_used: bool = False

class Message(BaseModel):
    role: str
    content: str
    timestamp: str

# Embedding model
class EmbedRequest(BaseModel):
    text: str

# Memory management functions
def get_memory_path(session_id: str) -> str:
    return f"{session_id}.json"

def load_conversation(session_id: str) -> List[Dict]:
    """Load conversation history from storage"""
    if USE_S3:
        try:
            response = s3_client.get_object(Bucket=S3_BUCKET, Key=get_memory_path(session_id))
            return json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return []
            raise
    else:
        # Local file storage
        file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                return json.load(f)
        return []

def save_conversation(session_id: str, messages: List[Dict]):
    """Save conversation history to storage"""
    if USE_S3:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=get_memory_path(session_id),
            Body=json.dumps(messages, indent=2),
            ContentType="application/json",
        )
    else:
        # Local file storage
        os.makedirs(MEMORY_DIR, exist_ok=True)
        file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
        with open(file_path, "w") as f:
            json.dump(messages, f, indent=2)

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

    response = sagemaker_client.invoke_endpoint(
        EndpointName=SAGEMAKER_ENDPOINT,
        ContentType="application/json",
        Body=json.dumps({"inputs": text})
    )
    result = json.loads(response["Body"].read().decode())

    if isinstance(result, list) and len(result) > 0:
        if isinstance(result[0], list) and len(result[0]) > 0:
            if isinstance(result[0][0], list):
                return result[0][0]
            return result[0]
    return result

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
    """Generator that yields text chunks based on size and overlap."""
    cursor = 0
    while cursor < len(text):
        chunk = text[cursor: cursor + size]
        yield chunk
        if cursor + size >= len(text):
            break
        cursor += (size - overlap)

def index_text_chunk(
    text: str,
    vector_id: str,
    metadata: Dict
) -> str:
    if not VECTOR_BUCKET or not VECTOR_INDEX:
        raise HTTPException(status_code=500, detail="VECTOR_BUCKET or VECTOR_INDEX not configured")

    embedding = get_embedding(text)

    s3vectors_client.put_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        vectors=[
            {
                "key": vector_id,
                "data": {"float32": [float(x) for x in embedding]},
                "metadata": {
                    "chunk_text": text,
                    **metadata
                }
            }
        ]
    )
    return vector_id

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
    top_k: int = 5
) -> List[Dict]:
    if not VECTOR_BUCKET or not VECTOR_INDEX:
        raise HTTPException(status_code=500, detail="VECTOR_BUCKET or VECTOR_INDEX not configured")

    query_embedding =  get_embedding(query)

    response = s3vectors_client.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        queryVector={"float32": [float(x) for x in query_embedding]},
        topK=top_k,
        returnDistance=True,
        returnMetadata=True
    )
    return response.get("vectors", [])

def is_rag_enabled() -> bool:
    return RAG_ENABLED and bool(SAGEMAKER_ENDPOINT and VECTOR_BUCKET and VECTOR_INDEX)

def get_lexical_score(query: str, document: str) -> float:
    """Returns a simple (Jaccard-style token intersection) overlap score between 0 and 1.
        TODO : use rank_bm25 or a similar algorithm for better lexical scoring
    """
    query_words = set(query.lower().split())
    doc_words = set(document.lower().split())
    if not query_words:
        return 0.0
    intersection = query_words.intersection(doc_words)
    return len(intersection) / len(query_words)

def retrieve_sources(
        query: str,
        fetch_n: int = RAW_FETCH_SIZE,
        return_n: int = FINAL_TOP_K
) -> List[SourceItem]:
    """
        Retrieves sources using a two-stage process:
        1. Fetch a large candidate pool (fetch_n).
        2. Rerank by combined score and limit per-document chunks.
    """

    if not is_rag_enabled():
        logger.info("retrieval disabled")
        return []

    try:
        # The "Wide Net" fetch from Vector DB
        raw_results = search_text_chunks(query, top_k=fetch_n)
        logger.info(f"Vector DB returned {len(raw_results)} raw candidates.")
    except Exception as exc:
        logger.error(f"Vector search failed: {exc}", exc_info=True)
        return []

    # Scoring and Reranking
    scored_items = []
    for item in raw_results:
        metadata = item.get("metadata") or {}
        snippet = metadata.get("chunk_text") or ""

        # Calculate scores
        lex_score = get_lexical_score(query, snippet)
        distance = item.get("distance") or 1.0  # Assume 1.0 if missing (max distance)

        # Combine: Lower cosine distance is better, higher lexical is better
        # Invert distance (1-d) to get 'vector similarity'
        combined_score = (1.0 - distance) + (lex_score * 0.5)

        scored_items.append({
            "score": combined_score,
            "item": item,
            "doc_id": metadata.get("source_path") or metadata.get("title") or "unknown"
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
            final_sources.append(
                SourceItem(
                    id=item.get("key") or str(uuid.uuid4()),
                    title=metadata.get("title"),
                    source_path=metadata.get("source_path"),
                    snippet=shorten_snippet(metadata.get("chunk_text", "")),
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

        lines.append(source.snippet)

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
        "rag_enabled": is_rag_enabled()
    }

@app.post("/embed")
async def embed(request: EmbedRequest):
    return {"embedding": get_embedding(request.text)}

@app.post("/ingest")
async def ingest_file(file: UploadFile = File(...)):
    if not file.filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Only .md files are supported")

    try:

        content = (await file.read()).decode("utf-8")

        chunks_processed = 0
        for count, chunk in enumerate(chunk_text(content)):

            vector_id = f"{file.filename}_{count}"

            metadata = {
                "source_path": f"api_upload/{file.filename}",
                "title": file.filename.rsplit('.', 1)[0],
                "doc_type": ".md",
                "chunk_index": count
            }

            index_text_chunk(chunk, vector_id, metadata) # TODO : use a ThreadPoolExecutor to run index_text_chunk in parallel
            chunks_processed += 1

        return {
            "filename": file.filename,
            "chunks_indexed": chunks_processed,
            "status": "success"
        }

    except Exception as e:
        logger.error(f"Ingestion failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ingestion error: {str(e)}")

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        # Generate session ID if not provided
        session_id = request.session_id or str(uuid.uuid4())

        # Load conversation history
        conversation = load_conversation(session_id)

        # Determine the search query (using history if it exists)
        search_query = rewrite_query(conversation, request.message)

        # Retrieve sources
        sources = retrieve_sources(search_query)

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

        # Save conversation
        save_conversation(session_id, conversation)

        return ChatResponse(
            response=assistant_response,
            session_id=session_id,
            sources=sources,
            retrieval_used=bool(sources)
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in chat endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/conversation/{session_id}")
async def get_conversation(session_id: str):
    """Retrieve conversation history"""
    try:
        conversation = load_conversation(session_id)
        return {"session_id": session_id, "messages": conversation}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)