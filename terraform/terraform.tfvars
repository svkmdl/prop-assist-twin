default_aws_region                       = "eu-central-1"
project_name                             = "prop-assist-twin"
environment                              = "dev"
bedrock_model_id                         = "eu.amazon.nova-pro-v1:0"
bedrock_light_model_id                   = "eu.amazon.nova-micro-v1:0"
lambda_timeout                           = 60
api_throttle_burst_limit                 = 10
api_throttle_rate_limit                  = 5
use_custom_domain                        = false
root_domain                              = ""
sagemaker_embedding_enabled              = true
sagemaker_embedding_model_name           = "sentence-transformers/all-MiniLM-L6-v2"
sagemaker_embedding_image_uri            = "763104351884.dkr.ecr.eu-central-1.amazonaws.com/huggingface-pytorch-inference:1.13.1-transformers4.26.0-cpu-py39-ubuntu20.04"
sagemaker_embedding_serverless_memory_mb = 3072
sagemaker_embedding_max_concurrency      = 2
s3vectors_enabled                        = true
s3vectors_index_name                     = "property-kb"
s3vectors_dimension                      = 384
s3vectors_distance_metric                = "cosine"
# Only chunk_text is large; all other worker metadata (tenant_id, source_key,
# title, doc_type, chunk_index, ...) is small and stays filterable. Changing
# this list is immutable and forces the vector index to be replaced, so leave
# it as-is to avoid wiping existing embeddings.
s3vectors_non_filterable_metadata_keys = [
  "chunk_text"
]
rag_enabled            = true
final_top_k            = 3
log_level              = "INFO"
max_retrieval_distance = 0.35
raw_fetch_size         = 5
max_chunks_per_doc     = 2
max_context_chars      = 1500
source_snippet_chars   = 200
chunk_size             = 1500
chunk_overlap          = 200
max_message_chars      = 3000
max_upload_bytes       = 1048576
supported_suffixes     = [".md"]
# Keep the per-document embedding fan-out at/below the serverless endpoint's
# max-concurrency (sagemaker_embedding_max_concurrency) to avoid self-inflicted
# InvokeEndpoint throttling. Cross-invocation bursts are absorbed by adaptive
# retries (embedding_max_attempts).
ingestion_max_workers  = 2
embedding_max_attempts = 10

# Event-driven RAG ingestion pipeline
rag_ingest_enabled                 = true
rag_ingest_lambda_timeout          = 300
rag_ingest_lambda_memory           = 1024
rag_ingest_reserved_concurrency    = 3
rag_ingest_max_receive_count       = 5
rag_ingest_batch_size              = 1
rag_ingest_queue_age_alarm_seconds = 900
alarm_sns_topic_arn                = ""