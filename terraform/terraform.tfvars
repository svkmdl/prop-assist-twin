default_aws_region                        = "eu-central-1"
project_name                              = "prop-assist-twin"
environment                               = "dev"
bedrock_model_id                          = "eu.amazon.nova-micro-v1:0"
lambda_timeout                            = 60
api_throttle_burst_limit                  = 10
api_throttle_rate_limit                   = 5
use_custom_domain                         = false
root_domain                               = ""
sagemaker_embedding_enabled               = true
sagemaker_embedding_model_name            = "sentence-transformers/all-MiniLM-L6-v2"
sagemaker_embedding_image_uri             = "763104351884.dkr.ecr.eu-central-1.amazonaws.com/huggingface-pytorch-inference:1.13.1-transformers4.26.0-cpu-py39-ubuntu20.04"
sagemaker_embedding_serverless_memory_mb  = 3072
sagemaker_embedding_max_concurrency       = 2
s3vectors_enabled                         = true
s3vectors_index_name                      = "property-kb"
s3vectors_dimension                       = 384
s3vectors_distance_metric                 = "cosine"
s3vectors_non_filterable_metadata_keys = [
  "chunk_text"
]
rag_enabled                               = true
retrieval_top_k                           = 3
log_level                                 = "INFO"
max_retrieval_distance                    = 0.5
source_snippet_chars                      = 200
chunk_size                                = 1500
chunk_overlap                             = 200