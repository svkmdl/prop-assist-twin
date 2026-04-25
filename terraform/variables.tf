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
    condition = contains(["cosine","euclidean"], var.s3vectors_distance_metric)
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

variable "retrieval_top_k" {
  description = "How many vector search results to retrieve for RAG"
  type        = number
  default     = 3

  validation {
    condition     = var.retrieval_top_k > 0
    error_message = "retrieval_top_k must be greater than 0."
  }
}

variable "log_level"{
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

variable "chunk_size"{
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