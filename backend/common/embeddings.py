"""SageMaker embedding invocation, decoupled from global client state.

The core ``embed_text`` helper accepts an explicit boto3 ``sagemaker-runtime``
client and endpoint name so it can be reused by the chat API (which owns a
module-level client) and the ingestion worker (which owns its own client).
"""
import json
from typing import Any, List


def embed_text(text: str, *, client: Any, endpoint: str) -> List[float]:
    """Return the embedding vector for ``text`` from a SageMaker endpoint.

    Args:
        text: The text to embed.
        client: A boto3 ``sagemaker-runtime`` client (or compatible stub).
        endpoint: The SageMaker endpoint name to invoke.

    Returns:
        A flat list of floats representing the embedding.
    """
    response = client.invoke_endpoint(
        EndpointName=endpoint,
        ContentType="application/json",
        Body=json.dumps({"inputs": text}),
    )
    result = json.loads(response["Body"].read().decode())

    if isinstance(result, list) and len(result) > 0:
        if isinstance(result[0], list) and len(result[0]) > 0:
            if isinstance(result[0][0], list):
                return result[0][0]
            return result[0]
    return result
