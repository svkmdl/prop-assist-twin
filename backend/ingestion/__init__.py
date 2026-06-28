"""Event-driven RAG ingestion worker.

Triggered by SQS messages carrying S3 ``ObjectCreated`` notifications for
markdown files uploaded under ``incoming/{tenant_id}/``. Reads the document,
chunks it, embeds each chunk, writes vectors to S3 Vectors, and records status
in the DynamoDB ingestion manifest.
"""
