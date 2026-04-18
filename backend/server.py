from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import os
from dotenv import load_dotenv
from typing import Optional, List, Dict
import json
import uuid
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
from context import prompt

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

# Initialize SageMaker client
sagemaker_client = boto3.client(
    service_name = "sagemaker-runtime",
    region_name=os.getenv("DEFAULT_AWS_REGION", "eu-central-1")
)

# Initialize S3Vectors client
s3vectors_client = boto3.client(
    service_name="s3vectors",
    region_name=os.getenv("DEFAULT_AWS_REGION", "eu-central-1")
)

# Initialize Bedrock client
bedrock_client = boto3.client(
    service_name="bedrock-runtime",
    region_name=os.getenv("DEFAULT_AWS_REGION", "eu-central-1")
)

# Bedrock model selection
# Available aws-trained foundation models:
# - amazon.nova-micro-v1:0  (fastest, cheapest)
# - amazon.nova-lite-v1:0   (balanced - default)
# - amazon.nova-pro-v1:0    (most capable, higher cost)
# you might need to add us. or eu. prefix to the below model id to get the regional inference profile
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "eu.amazon.nova-lite-v1:0")

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

def call_bedrock(conversation: List[Dict], user_message: str) -> str:
    """Call AWS Bedrock with conversation history"""

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

    try:
        # Call Bedrock using the converse API
        response = bedrock_client.converse(
            modelId=BEDROCK_MODEL_ID,
            system = [{"text": prompt()}],
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

def index_text_chunk(
    text: str,
    metadata: Optional[Dict] = None,
    vector_id: Optional[str] = None
) -> str:
    if not VECTOR_BUCKET or not VECTOR_INDEX:
        raise HTTPException(status_code=500, detail="VECTOR_BUCKET or VECTOR_INDEX not configured")

    embedding = get_embedding(text)
    key = vector_id or str(uuid.uuid4())

    s3vectors_client.put_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        vectors=[
            {
                "key": key,
                "data": {"float32": [float(x) for x in embedding]},
                "metadata": {
                    "chunk_text": text,
                    **(metadata or {})
                }
            }
        ]
    )

    return key

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

def retrieve_sources(query: str, top_k: int = RETRIEVAL_TOP_K) -> List[SourceItem]:
    """
        Retrieves and cleans relevant document chunks based on a search query.
        Performs deduplication to optimize the LLM's context window.
    """

    if not is_rag_enabled():
        return []

    try:
        raw_results = search_text_chunks(query, top_k=top_k)
    except Exception as exc:
        print(f"Retrieval failed: {exc}")
        return []

    sources: List[SourceItem] = []
    seen_snippets = set()

    for item in raw_results:
        metadata = item.get("metadata") or {}
        snippet = (metadata.get("chunk_text") or "").strip()
        if not snippet:
            continue

        # Normalization: Standardize text to catch duplicates with different formatting
        normalized = " ".join(snippet.split()).lower()
        if normalized in seen_snippets:
            continue
        seen_snippets.add(normalized)

        distance = item.get("distance")

        sources.append(
            SourceItem(
                id=item.get("key") or str(uuid.uuid4()),
                title=metadata.get("title"),
                source_path=metadata.get("source_path"),
                snippet=snippet,
                doc_type=metadata.get("doc_type"),
                chunk_index=metadata.get("chunk_index"),
                distance=distance if isinstance(distance, (int, float)) else None,
            )
        )

    return sources

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
        "s3vectors_configured" : bool(VECTOR_BUCKET and VECTOR_INDEX)
    }

@app.post("/embed")
async def embed(request: EmbedRequest):
    return {"embedding": get_embedding(request.text)}

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        # Generate session ID if not provided
        session_id = request.session_id or str(uuid.uuid4())

        # Load conversation history
        conversation = load_conversation(session_id)

        # Call Bedrock for response
        assistant_response = call_bedrock(conversation, request.message)

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

        return ChatResponse(response=assistant_response,
                            session_id=session_id,
                            sources=[],
                            retrieval_used=False
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