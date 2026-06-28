output "api_gateway_url" {
  description = "URL of the API Gateway"
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "cloudfront_url" {
  description = "URL of the CloudFront distribution"
  value       = "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "s3_frontend_bucket" {
  description = "Name of the S3 bucket for frontend"
  value       = aws_s3_bucket.frontend.id
}

output "s3_memory_bucket" {
  description = "Name of the S3 bucket for memory storage"
  value       = aws_s3_bucket.memory.id
}

output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.api.function_name
}

output "custom_domain_url" {
  description = "Root URL of the production site"
  value       = var.use_custom_domain ? "https://${var.root_domain}" : ""
}

output "sagemaker_embedding_endpoint_name" {
  description = "Name of the SageMaker embedding endpoint"
  value       = try(aws_sagemaker_endpoint.embedding_endpoint[0].name, "")
}

output "sagemaker_embedding_endpoint_arn" {
  description = "ARN of the SageMaker embedding endpoint"
  value       = try(aws_sagemaker_endpoint.embedding_endpoint[0].arn, "")
}

output "s3vectors_bucket_name" {
  description = "Name of the S3Vectors Bucket"
  value       = try(aws_s3vectors_vector_bucket.rag[0].vector_bucket_name, "")
}

output "s3vectors_bucket_arn" {
  description = "ARN of the S3Vectors Bucket"
  value       = try(aws_s3vectors_vector_bucket.rag[0].vector_bucket_arn, "")
}

output "s3vectors_index_name" {
  description = "Name of the S3Vectors index"
  value       = try(aws_s3vectors_index.rag[0].index_name, "")
}

output "s3vectors_index_arn" {
  description = "ARN of the S3Vectors index"
  value       = try(aws_s3vectors_index.rag[0].index_arn, "")
}

output "rag_docs_bucket" {
  description = "Name of the S3 bucket for raw RAG source documents"
  value       = try(aws_s3_bucket.rag_docs[0].id, "")
}

output "rag_ingest_queue_url" {
  description = "URL of the RAG ingestion SQS queue"
  value       = try(aws_sqs_queue.rag_ingest[0].id, "")
}

output "rag_ingest_dlq_url" {
  description = "URL of the RAG ingestion dead-letter queue"
  value       = try(aws_sqs_queue.rag_ingest_dlq[0].id, "")
}

output "rag_ingestion_manifest_table" {
  description = "Name of the DynamoDB ingestion manifest table"
  value       = try(aws_dynamodb_table.rag_ingestion_manifest[0].name, "")
}

output "rag_ingest_worker_name" {
  description = "Name of the RAG ingestion worker Lambda"
  value       = try(aws_lambda_function.rag_ingest_worker[0].function_name, "")
}