from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
LAMBDA_PKG_DIR = BACKEND_DIR / "lambda-package"

# Create a blank object to act as a fake AWS client.
# This prevents tests from attempting to connect to real AWS services.
def _fake_boto3_client(*args, **kwargs):
    return SimpleNamespace()


# Default environment for a hermetic, local testing state.
# Disables S3 and EC2 metadata to avoid timeouts and costs during tests.
DEFAULT_TEST_ENV = {
    "AWS_EC2_METADATA_DISABLED": "true",
    "USE_S3": "false",
    "DEFAULT_AWS_REGION": "eu-central-1",
    "SAGEMAKER_ENDPOINT": "",
    "VECTOR_BUCKET": "",
    "VECTOR_INDEX": "",
    "RAG_ENABLED": "true",
    "LOCAL_DEV": "true",
}


def _load_server_module(monkeypatch, tmp_path, env_overrides=None):
    """Import a fresh `server` module with a controlled environment.

    Re-importing per call forces the module-level configuration constants
    (read from env at import time) to pick up the requested overrides.
    """
    monkeypatch.chdir(BACKEND_DIR)
    monkeypatch.syspath_prepend(str(BACKEND_DIR))
    monkeypatch.syspath_prepend(str(LAMBDA_PKG_DIR))

    env = dict(DEFAULT_TEST_ENV)
    env["MEMORY_DIR"] = str(tmp_path / "memory")
    if env_overrides:
        env.update(env_overrides)

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setattr(boto3, "client", _fake_boto3_client)

    # Force a clean re-import so the new environment variables take effect.
    for name in ("server", "context", "resources", "lambda_handler"):
        sys.modules.pop(name, None)

    # Import as `server`, not `backend.server`
    return importlib.import_module("server")


@pytest.fixture
def server_module(monkeypatch, tmp_path):
    return _load_server_module(monkeypatch, tmp_path)


@pytest.fixture
def make_server(monkeypatch, tmp_path):
    """Factory to import `server` with custom environment overrides.

    Intended to be called once per test (each call re-imports the module).
    """
    def _factory(**env_overrides):
        return _load_server_module(monkeypatch, tmp_path, env_overrides)

    return _factory


@pytest.fixture
def client(server_module):
    """
        Provides a FastAPI TestClient.
        The 'with' block ensures the app's startup and shutdown events are triggered.
    """
    with TestClient(server_module.app) as test_client:
        yield test_client


def make_client(server_module):
    """Build a TestClient context manager for a custom server module."""
    return TestClient(server_module.app)