"""SQS-triggered Lambda entrypoint for the RAG ingestion pipeline.

Each SQS record carries an S3 ``ObjectCreated`` notification. Permanent
failures (invalid documents, malformed events) are acknowledged so the message
leaves the queue; transient failures are reported via partial batch responses
so SQS can retry and ultimately route to the DLQ.
"""
import json
import logging

from common import config
from ingestion import ingest_document
from ingestion.ingest_document import InvalidDocumentError

logging.getLogger().setLevel(config.LOG_LEVEL)
logger = logging.getLogger(__name__)


def _process_s3_event(payload: dict) -> None:
    """Process every S3 record contained in one SQS message body."""
    s3_records = payload.get("Records", [])
    if not s3_records:
        # e.g. the S3 "s3:TestEvent" sent on notification setup. Nothing to do.
        logger.info("No S3 records in message; ignoring.")
        return

    for s3_record in s3_records:
        s3_info = s3_record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        obj = s3_info.get("object", {})
        key = obj.get("key")
        version_id = obj.get("versionId")

        if not bucket or not key:
            raise InvalidDocumentError("S3 event missing bucket or object key")

        ingest_document.process_document(
            bucket=bucket, key=key, version_id=version_id
        )


def _process_record(record: dict) -> None:
    """Parse one SQS record and dispatch its S3 event(s)."""
    body = record.get("body")
    if not body:
        raise InvalidDocumentError("SQS record had no body")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        # A non-JSON body will never succeed on retry; acknowledge it.
        raise InvalidDocumentError(f"SQS body was not valid JSON: {exc}") from exc

    _process_s3_event(payload)


def handler(event, context):
    """Lambda handler. Returns SQS partial-batch failures for retryable errors."""
    records = event.get("Records", [])
    batch_item_failures = []

    for record in records:
        message_id = record.get("messageId")
        try:
            _process_record(record)
        except InvalidDocumentError as exc:
            # Permanent failure: ack the message (do not retry).
            logger.warning("Dropping message %s (permanent): %s", message_id, exc)
        except Exception as exc:  # noqa: BLE001 - transient: allow SQS retry -> DLQ
            logger.exception("Message %s failed (transient): %s", message_id, exc)
            if message_id:
                batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
