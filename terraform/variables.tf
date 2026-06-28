variable "project_name" {
  description = "Name prefix for all resources"
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.project_name))
    error_message = "Project name must contain only lowercase letters, numbers, and hyphens."
  }
}

variable "environment" {
  description = "Environment name (dev, test, prod)"
  type        = string
  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Environment must be one of: dev, test, prod."
  }
}

variable "bedrock_model_id" {
  description = "Bedrock model ID"
  type        = string
  default     = "eu.amazon.nova-pro-v1:0"
}

variable "bedrock_light_model_id" {
  description = "Bedrock light model ID"
  type        = string
  default     = "eu.amazon.nova-micro-v1:0"
}

variable "lambda_timeout" {
  description = "Lambda function timeout in seconds"
  type        = number
  default     = 60
}

variable "api_throttle_burst_limit" {
  description = "API Gateway throttle burst limit"
  type        = number
  default     = 10
}

variable "api_throttle_rate_limit" {
  description = "API Gateway throttle rate limit"
  type        = number
  default     = 5
}

variable "use_custom_domain" {
  description = "Attach a custom domain to CloudFront"
  type        = bool
  default     = false
}

variable "root_domain" {
  description = "Apex domain name, e.g. mydomain.com"
  type        = string
  default     = ""
}

variable "sagemaker_embedding_enabled" {
  description = "Create a SageMaker serverless endpoint for embeddings"
  type        = bool
  default     = false
}

variable "sagemaker_embedding_model_name" {
  description = "Huggingface model ID for embeddings"
  type        = string
  default     = "sentence-transformers/all-MiniLM-L6-v2"
}

variable "sagemaker_embedding_image_uri" {
  description = "Region-specific Huggingface SageMaker inference image URI"
  type        = string
  default     = ""
}

variable "sagemaker_embedding_serverless_memory_mb" {
  description = "Memory size for the SageMaker serverless embedding endpoint"
  type        = number
  default     = 3072
}

variable "sagemaker_embedding_max_concurrency" {
  description = "Max concurrency for the SageMaker serverless embedding endpoint"
  type        = number
  default     = 2
}

variable "s3vectors_enabled" {
  description = "Create an S3 Vectors bucket and index for RAG embeddings"
  type        = bool
  default     = false
}

variable "s3vectors_index_name" {
  description = "Index name inside the S3 Vectors bucket"
  type        = string
  default     = "property-kb"
}

variable "s3vectors_dimension" {
  description = "Embedding dimension of the vector index in S3 Vectors"
  type        = number
  default     = 384
}

variable "s3vectors_distance_metric" {
  description = "Distance metric for similarity search"
  type        = string
  default     = "cosine"

  validation {
    condition     = contains(["cosine", "euclidean"], var.s3vectors_distance_metric)
    error_message = "s3vectors_distance_metric must be either 'cosine' or 'euclidean'."
  }
}

variable "s3vectors_non_filterable_metadata_keys" {
  description = "List of metadata keys that should be retrievable but not filterable in S3 Vectors"
  type        = list(string)
  default     = ["chunk_text"]
}

variable "default_aws_region" {
  description = "Region used by backend clients inside Lambda"
  type        = string
  default     = "eu-central-1"
}

variable "rag_enabled" {
  description = "Enable retrieval-augmented generation (RAG) in the backend"
  type        = bool
  default     = true
}

variable "final_top_k" {
  description = "How many vector search results to consider for RAG"
  type        = number
  default     = 3

  validation {
    condition     = var.final_top_k > 0
    error_message = "final_top_k must be greater than 0."
  }
}

variable "log_level" {
  description = "Backend log level"
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], upper(var.log_level))
    error_message = "log_level must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL."
  }
}

variable "max_retrieval_distance" {
  description = "The maximum allowed distance for RAG context retrieval"
  type        = number
  default     = 0.5

  validation {
    condition     = var.max_retrieval_distance >= 0
    error_message = "The max_retrieval_distance must be a non-negative number."
  }
}

variable "source_snippet_chars" {
  description = "The maximum number of characters allowed for RAG source snippets."
  type        = number
  default     = 280

  validation {
    condition     = var.source_snippet_chars > 0
    error_message = "The source_snippet_chars must be a positive integer."
  }
}

variable "chunk_size" {
  description = "The number of characters in each chunk when splitting documents for RAG."
  type        = number
  default     = 1500

  validation {
    condition     = var.chunk_size > 0
    error_message = "The chunk_size must be a positive integer."
  }
}

variable "chunk_overlap" {
  description = "The number of overlapping characters between chunks when splitting documents for RAG."
  type        = number
  default     = 200
}

variable "admin_api_key" {
  description = "Temporary admin API key for /ingest, /embed, and /conversation. Will use real OIDC/SAML/Cognito etc for production."
  type        = string
  default     = ""
  sensitive   = true
}

variable "max_message_chars" {
  description = "Maximum user message length accepted by the backend"
  type        = number
  default     = 3000
}

variable "max_upload_bytes" {
  description = "Maximum markdown ingestion upload size"
  type        = number
  default     = 1048576
}

variable "raw_fetch_size" {
  description = "Number of raw vector candidates fetched before reranking"
  type        = number
  default     = 12
}

variable "max_chunks_per_doc" {
  description = "Maximum source chunks selected from one document"
  type        = number
  default     = 2
}

variable "max_context_chars" {
  description = "Maximum retrieved context characters passed to the answer model per source"
  type        = number
  default     = 1500
}
variable "ingestion_max_workers" {
  description = "Maximum number of concurrent ingestion workers"
  type        = number
  default     = 4
}

variable "embedding_max_attempts" {
  description = "Max botocore (adaptive) retry attempts for SageMaker InvokeEndpoint, to absorb serverless-endpoint throttling."
  type        = number
  default     = 10
}

# --- Event-driven RAG ingestion pipeline ---

variable "rag_ingest_enabled" {
  description = "Create the event-driven RAG ingestion pipeline (S3 -> SQS -> Lambda -> DynamoDB + S3 Vectors). Requires s3vectors_enabled and sagemaker_embedding_enabled."
  type        = bool
  default     = false
}

variable "rag_ingest_lambda_timeout" {
  description = "Ingestion worker Lambda timeout in seconds"
  type        = number
  default     = 300
}

variable "rag_ingest_lambda_memory" {
  description = "Ingestion worker Lambda memory size in MB"
  type        = number
  default     = 1024
}

variable "rag_ingest_reserved_concurrency" {
  description = "Reserved concurrency for the ingestion worker Lambda"
  type        = number
  default     = 3
}

variable "rag_ingest_max_receive_count" {
  description = "Number of delivery attempts before a message is routed to the DLQ"
  type        = number
  default     = 5
}

variable "rag_ingest_batch_size" {
  description = "Number of SQS messages delivered to the ingestion worker per invocation"
  type        = number
  default     = 1
}

variable "rag_ingest_queue_age_alarm_seconds" {
  description = "Alarm threshold for the age (seconds) of the oldest message in the ingestion queue"
  type        = number
  default     = 900
}

variable "alarm_sns_topic_arn" {
  description = "Optional SNS topic ARN to notify on ingestion CloudWatch alarms. Empty disables notifications (alarms still visible in the console)."
  type        = string
  default     = ""
}