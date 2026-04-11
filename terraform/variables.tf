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