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