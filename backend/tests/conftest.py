from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Create a blank object to act as a fake AWS client.
# This prevents tests from attempting to connect to real AWS services.
def _fake_boto3_client(*args, **kwargs):
    return SimpleNamespace()


@pytest.fixture
def server_module(monkeypatch, tmp_path):
    monkeypatch.chdir(BACKEND_DIR)
    monkeypatch.syspath_prepend(str(BACKEND_DIR))

    # Force the application into a local testing state.
    # We disable S3 and EC2 metadata to avoid timeouts and costs during tests.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("USE_S3", "false")

    monkeypatch.setenv("DEFAULT_AWS_REGION", "eu-central-1")
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("SAGEMAKER_ENDPOINT", "")
    monkeypatch.setenv("VECTOR_BUCKET", "")
    monkeypatch.setenv("VECTOR_INDEX", "")
    monkeypatch.setenv("RAG_ENABLED", "true")

    monkeypatch.setattr(boto3, "client", _fake_boto3_client)

    # Clear the cache of these modules.
    # This forces Python to re-import them with the NEW environment variables
    # defined above, ensuring a clean state for every test run.
    for name in ("server", "context", "resources", "lambda_handler"):
        sys.modules.pop(name, None)

    # Import as `server`, not `backend.server`
    return importlib.import_module("server")


@pytest.fixture
def client(server_module):
    """
        Provides a FastAPI TestClient.
        The 'with' block ensures the app's startup and shutdown events are triggered.
    """
    with TestClient(server_module.app) as test_client:
        yield test_client