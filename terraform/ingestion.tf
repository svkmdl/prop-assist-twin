# Event-driven RAG ingestion pipeline
#
#   S3 (incoming/{tenant}/*.md) -> SQS -> Lambda worker -> S3 Vectors
#                                          |-> DynamoDB ingestion manifest
#   Failures: SQS DLQ + CloudWatch alarms
#
# All resources are gated by var.rag_ingest_enabled. Enabling this pipeline
# also requires var.s3vectors_enabled and var.sagemaker_embedding_enabled.

locals {
  rag_ingest_count = var.rag_ingest_enabled ? 1 : 0

  # Vector index + embedding endpoint the worker reads/writes. These come from
  # the (separately gated) S3 Vectors and SageMaker resources in main.tf.
  ingest_vector_index_arn = try(aws_s3vectors_index.rag[0].index_arn, "*")
  ingest_sagemaker_name   = try(aws_sagemaker_endpoint.embedding_endpoint[0].name, "")
  ingest_sagemaker_arn    = var.sagemaker_embedding_enabled ? "arn:aws:sagemaker:${var.default_aws_region}:${data.aws_caller_identity.current.account_id}:endpoint/${try(aws_sagemaker_endpoint.embedding_endpoint[0].name, "")}" : "*"

  rag_ingest_alarm_actions = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []
}

# ---------------------------------------------------------------------------
# S3 source bucket for raw documents
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "rag_docs" {
  count  = local.rag_ingest_count
  bucket = "${local.name_prefix}-rag-docs-${data.aws_caller_identity.current.account_id}"
  tags   = local.common_tags
}

resource "aws_s3_bucket_versioning" "rag_docs" {
  count  = local.rag_ingest_count
  bucket = aws_s3_bucket.rag_docs[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "rag_docs" {
  count  = local.rag_ingest_count
  bucket = aws_s3_bucket.rag_docs[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "rag_docs" {
  count  = local.rag_ingest_count
  bucket = aws_s3_bucket.rag_docs[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "rag_docs" {
  count  = local.rag_ingest_count
  bucket = aws_s3_bucket.rag_docs[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# ---------------------------------------------------------------------------
# SQS queue + dead-letter queue
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "rag_ingest_dlq" {
  count                     = local.rag_ingest_count
  name                      = "${local.name_prefix}-rag-ingest-dlq"
  message_retention_seconds = 1209600 # 14 days
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "rag_ingest" {
  count = local.rag_ingest_count
  name  = "${local.name_prefix}-rag-ingest"

  # Visibility timeout must comfortably exceed the worker timeout so a single
  # message is not redelivered while still being processed.
  visibility_timeout_seconds = var.rag_ingest_lambda_timeout * 6
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.rag_ingest_dlq[0].arn
    maxReceiveCount     = var.rag_ingest_max_receive_count
  })

  tags = local.common_tags
}

# Allow the S3 source bucket to publish ObjectCreated events to the queue.
resource "aws_sqs_queue_policy" "rag_ingest_from_s3" {
  count     = local.rag_ingest_count
  queue_url = aws_sqs_queue.rag_ingest[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowS3SendMessage"
        Effect    = "Allow"
        Principal = { Service = "s3.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.rag_ingest[0].arn
        Condition = {
          ArnEquals    = { "aws:SourceArn" = aws_s3_bucket.rag_docs[0].arn }
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# S3 -> SQS notification (markdown uploads under incoming/)
# ---------------------------------------------------------------------------
resource "aws_s3_bucket_notification" "rag_docs" {
  count  = local.rag_ingest_count
  bucket = aws_s3_bucket.rag_docs[0].id

  dynamic "queue" {
    for_each = var.supported_suffixes
    content {
      queue_arn     = aws_sqs_queue.rag_ingest[0].arn
      events        = ["s3:ObjectCreated:*"]
      filter_prefix = "incoming/"
      filter_suffix = queue.value
    }
  }

  depends_on = [aws_sqs_queue_policy.rag_ingest_from_s3]
}

# ---------------------------------------------------------------------------
# DynamoDB ingestion manifest
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "rag_ingestion_manifest" {
  count        = local.rag_ingest_count
  name         = "${local.name_prefix}-rag-ingestion-manifest"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tenant_id"
  range_key    = "source_sk"

  attribute {
    name = "tenant_id"
    type = "S"
  }

  attribute {
    name = "source_sk"
    type = "S"
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# IAM role for the ingestion worker Lambda
# ---------------------------------------------------------------------------
resource "aws_iam_role" "rag_ingest_worker" {
  count = local.rag_ingest_count
  name  = "${local.name_prefix}-rag-ingest-worker-role"
  tags  = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action    = "sts:AssumeRole"
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "rag_ingest_worker_basic" {
  count      = local.rag_ingest_count
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  role       = aws_iam_role.rag_ingest_worker[0].name
}

resource "aws_iam_policy" "rag_ingest_worker" {
  count = local.rag_ingest_count
  name  = "${local.name_prefix}-rag-ingest-worker-policy"
  tags  = local.common_tags

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSourceDocuments"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.rag_docs[0].arn,
          "${aws_s3_bucket.rag_docs[0].arn}/*"
        ]
      },
      {
        Sid    = "ConsumeIngestQueue"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ChangeMessageVisibility"
        ]
        Resource = aws_sqs_queue.rag_ingest[0].arn
      },
      {
        Sid    = "UpdateManifest"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem"
        ]
        Resource = aws_dynamodb_table.rag_ingestion_manifest[0].arn
      },
      {
        Sid    = "WriteVectors"
        Effect = "Allow"
        Action = [
          "s3vectors:PutVectors",
          "s3vectors:DeleteVectors",
          "s3vectors:GetVectors",
          "s3vectors:ListVectors"
        ]
        Resource = local.ingest_vector_index_arn
      },
      {
        Sid      = "InvokeEmbeddingEndpoint"
        Effect   = "Allow"
        Action   = ["sagemaker:InvokeEndpoint"]
        Resource = local.ingest_sagemaker_arn
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "rag_ingest_worker" {
  count      = local.rag_ingest_count
  policy_arn = aws_iam_policy.rag_ingest_worker[0].arn
  role       = aws_iam_role.rag_ingest_worker[0].name
}

# ---------------------------------------------------------------------------
# Ingestion worker Lambda (shares the deployment zip with the chat Lambda)
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "rag_ingest_worker" {
  count            = local.rag_ingest_count
  s3_bucket        = aws_s3_bucket.lambda_deployments.id
  s3_key           = aws_s3_object.api_code.key
  function_name    = "${local.name_prefix}-rag-ingest-worker"
  role             = aws_iam_role.rag_ingest_worker[0].arn
  handler          = "ingestion.worker.handler"
  source_code_hash = filebase64sha256("${path.module}/../backend/lambda-deployment.zip")
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  timeout          = var.rag_ingest_lambda_timeout
  memory_size      = var.rag_ingest_lambda_memory
  tags             = local.common_tags

  reserved_concurrent_executions = var.rag_ingest_reserved_concurrency

  environment {
    variables = {
      DEFAULT_AWS_REGION     = var.default_aws_region
      LOG_LEVEL              = var.log_level
      SAGEMAKER_ENDPOINT     = local.ingest_sagemaker_name
      VECTOR_BUCKET          = try(aws_s3vectors_vector_bucket.rag[0].vector_bucket_name, "")
      VECTOR_INDEX           = try(aws_s3vectors_index.rag[0].index_name, "")
      EMBEDDING_MODEL        = var.sagemaker_embedding_model_name
      MANIFEST_TABLE         = aws_dynamodb_table.rag_ingestion_manifest[0].name
      RAG_DOCS_BUCKET        = aws_s3_bucket.rag_docs[0].id
      CHUNK_SIZE             = tostring(var.chunk_size)
      CHUNK_OVERLAP          = tostring(var.chunk_overlap)
      MAX_UPLOAD_BYTES       = tostring(var.max_upload_bytes)
      INGESTION_MAX_WORKERS  = tostring(var.ingestion_max_workers)
      EMBEDDING_MAX_ATTEMPTS = tostring(var.embedding_max_attempts)
      SUPPORTED_SUFFIXES     = join(",", var.supported_suffixes)
    }
  }
}

resource "aws_lambda_event_source_mapping" "rag_ingest" {
  count            = local.rag_ingest_count
  event_source_arn = aws_sqs_queue.rag_ingest[0].arn
  function_name    = aws_lambda_function.rag_ingest_worker[0].arn
  batch_size       = var.rag_ingest_batch_size

  function_response_types = ["ReportBatchItemFailures"]

  # The mapping is validated against the worker role at creation time, so the
  # SQS permissions must be attached first to avoid an intermittent
  # "role does not have permissions to call ReceiveMessage" apply failure.
  depends_on = [aws_iam_role_policy_attachment.rag_ingest_worker]
}

# ---------------------------------------------------------------------------
# CloudWatch alarms
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "rag_ingest_dlq_messages" {
  count               = local.rag_ingest_count
  alarm_name          = "${local.name_prefix}-rag-ingest-dlq-messages"
  alarm_description   = "Messages have landed in the RAG ingestion DLQ."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.rag_ingest_dlq[0].name
  }

  alarm_actions = local.rag_ingest_alarm_actions
  ok_actions    = local.rag_ingest_alarm_actions
  tags          = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "rag_ingest_lambda_errors" {
  count               = local.rag_ingest_count
  alarm_name          = "${local.name_prefix}-rag-ingest-lambda-errors"
  alarm_description   = "The RAG ingestion worker Lambda is reporting errors."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.rag_ingest_worker[0].function_name
  }

  alarm_actions = local.rag_ingest_alarm_actions
  ok_actions    = local.rag_ingest_alarm_actions
  tags          = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "rag_ingest_queue_age" {
  count               = local.rag_ingest_count
  alarm_name          = "${local.name_prefix}-rag-ingest-queue-age"
  alarm_description   = "The oldest message in the RAG ingestion queue is too old; the worker may be stuck or throttled."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.rag_ingest_queue_age_alarm_seconds
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.rag_ingest[0].name
  }

  alarm_actions = local.rag_ingest_alarm_actions
  ok_actions    = local.rag_ingest_alarm_actions
  tags          = local.common_tags
}
