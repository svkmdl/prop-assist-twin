"""
Security / auth tests for the admin gate and session_id validation.

Covers `require_admin_api_key` (configured, bad key, missing key, unconfigured
with and without LOCAL_DEV) and `normalize_session_id` rejection of unsafe ids.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

VALID_KEY = "secret-admin-key"


class TestAdminGateConfigured:
    """ADMIN_API_KEY is set: only requests with the exact key are allowed."""

    def test_valid_key_allows_access(self, make_server):
        server_module = make_server(ADMIN_API_KEY=VALID_KEY, LOCAL_DEV="false")
        with TestClient(server_module.app) as client:
            resp = client.get(
                "/conversation/test-session", headers={"x-api-key": VALID_KEY}
            )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "test-session"

    def test_bad_key_rejected(self, make_server):
        server_module = make_server(ADMIN_API_KEY=VALID_KEY, LOCAL_DEV="false")
        with TestClient(server_module.app) as client:
            resp = client.get(
                "/conversation/test-session", headers={"x-api-key": "wrong-key"}
            )
        assert resp.status_code == 401

    def test_missing_key_rejected(self, make_server):
        server_module = make_server(ADMIN_API_KEY=VALID_KEY, LOCAL_DEV="false")
        with TestClient(server_module.app) as client:
            resp = client.get("/conversation/test-session")
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "method,path,kwargs",
        [
            ("get", "/conversation/test-session", {}),
            ("post", "/embed", {"json": {"text": "hello"}}),
        ],
        ids=["conversation", "embed"],
    )
    def test_gated_endpoints_reject_without_key(self, make_server, method, path, kwargs):
        server_module = make_server(ADMIN_API_KEY=VALID_KEY, LOCAL_DEV="false")
        with TestClient(server_module.app) as client:
            resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == 401


class TestIngestEndpointGating:
    """The synchronous /ingest endpoint exists only when LOCAL_DEV=true."""

    def test_ingest_absent_outside_local_dev(self, make_server):
        server_module = make_server(ADMIN_API_KEY=VALID_KEY, LOCAL_DEV="false")
        with TestClient(server_module.app) as client:
            resp = client.post(
                "/ingest",
                files={"file": ("notes.md", b"# hi", "text/markdown")},
                headers={"x-api-key": VALID_KEY},
            )
        assert resp.status_code == 404

    def test_ingest_present_and_gated_in_local_dev(self, make_server):
        server_module = make_server(ADMIN_API_KEY=VALID_KEY, LOCAL_DEV="true")
        with TestClient(server_module.app) as client:
            resp = client.post(
                "/ingest",
                files={"file": ("notes.md", b"# hi", "text/markdown")},
            )
        # Route exists but the admin gate rejects the missing key.
        assert resp.status_code == 401


class TestAdminGateUnconfigured:
    """ADMIN_API_KEY is not set: behavior depends on LOCAL_DEV."""

    def test_local_dev_leaves_gate_open(self, make_server):
        server_module = make_server(ADMIN_API_KEY="", LOCAL_DEV="true")
        with TestClient(server_module.app) as client:
            resp = client.get("/conversation/test-session")
        assert resp.status_code == 200

    def test_non_local_dev_returns_service_unavailable(self, make_server):
        server_module = make_server(ADMIN_API_KEY="", LOCAL_DEV="false")
        with TestClient(server_module.app) as client:
            resp = client.get("/conversation/test-session")
        assert resp.status_code == 503


class TestSessionIdValidation:
    """`normalize_session_id` must reject anything outside the safe pattern."""

    def test_accepts_valid_id(self, server_module):
        assert server_module.normalize_session_id("abc-123_XYZ") == "abc-123_XYZ"

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "bad!id", "has space", "x" * 65])
    def test_rejects_invalid_id_unit(self, server_module, bad):
        with pytest.raises(HTTPException) as exc:
            server_module.normalize_session_id(bad)
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("bad", ["bad!id", "has space", "x" * 65])
    def test_conversation_endpoint_rejects_invalid_id(self, client, bad):
        # Default fixture runs with LOCAL_DEV=true, so the admin gate is open
        # and the request reaches normalize_session_id.
        resp = client.get(f"/conversation/{bad}")
        assert resp.status_code == 400
