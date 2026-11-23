"""P0 containment: anonymous /chat cannot reach other scopes, staff documents or ``admin``.

Deny-path tests for docs/adr/0003, carried over to scope-based retrieval (ADR 0002 slice 4).
They prove the *code* boundary with fake AWS clients; they are not evidence about a
deployed environment.
"""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from scope_helpers import md

KEY = "secret-admin-key"
AUTH = {"x-api-key": KEY}
TODAY = date(2027, 3, 1)


class Recorder:
    """Stands in for retrieval + Bedrock and records every call."""

    def __init__(self):
        self.scopes = []
        self.bedrock_calls = 0

    @property
    def principals(self):
        return [scope.principal for scope in self.scopes]

    def install(self, server_module, monkeypatch):
        def fake_retrieve(query, scope=None):
            self.scopes.append(scope)
            return []

        def fake_bedrock(conversation, user_message, sources=None):
            self.bedrock_calls += 1
            return "stub"

        monkeypatch.setattr(server_module, "retrieve_sources", fake_retrieve)
        monkeypatch.setattr(server_module, "call_bedrock", fake_bedrock)


def fix_the_date(server_module, monkeypatch):
    # Build the resolver from the server's own (freshly imported) module: conftest re-imports
    # `common.*` per server load, so a resolver from this file's import would raise a stale
    # UnknownPrincipalError class that the server does not catch.
    monkeypatch.setattr(
        server_module,
        "SCOPE_RESOLVER",
        server_module.StaticScopeResolver(server_module.REGISTRY, today=lambda: TODAY),
    )


@pytest.fixture
def deployed(make_server, monkeypatch):
    """Deployed-like server: LOCAL_DEV off, admin key set, public tenant T001."""

    def _build(**env):
        base = {"LOCAL_DEV": "false", "ADMIN_API_KEY": KEY, "PUBLIC_CHAT_TENANT_ID": "T001"}
        base.update(env)
        server_module = make_server(**base)
        fix_the_date(server_module, monkeypatch)
        rec = Recorder()
        rec.install(server_module, monkeypatch)
        return server_module, rec

    return _build


def post(server_module, body, headers=None):
    with TestClient(server_module.app) as client:
        return client.post("/chat", json=body, headers=headers or {})


class TestAnonymousIsPinned:
    def test_omitted_tenant_resolves_to_the_public_tenant_not_admin(self, deployed):
        sm, rec = deployed()
        resp = post(sm, {"message": "hi"})
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == "T001"
        assert rec.principals == ["T001"]

    def test_explicit_public_tenant_is_accepted(self, deployed):
        sm, rec = deployed()
        assert post(sm, {"message": "hi", "tenant_id": "T001"}).status_code == 200
        assert rec.principals == ["T001"]

    def test_the_anonymous_scope_is_a_renter_scope_of_exactly_that_tenant(self, deployed):
        sm, rec = deployed()
        post(sm, {"message": "hi"})
        (scope,) = rec.scopes
        assert scope.role == "renter"
        assert scope.lease_ids == {"L-0001"} and scope.property_ids == {"P-001"}
        assert scope.as_of == TODAY

    @pytest.mark.parametrize("tenant", ["admin", "T002", "T003", "T999", "NOPE"])
    def test_other_tenants_are_refused_before_retrieval_and_model(self, deployed, tenant):
        sm, rec = deployed()
        resp = post(sm, {"message": "hi", "tenant_id": tenant})
        assert resp.status_code == 403
        assert rec.scopes == [] and rec.bedrock_calls == 0

    def test_registered_and_unregistered_tenants_are_indistinguishable(self, deployed):
        """No oracle: 'T002' (registered), 'T999' and 'admin' all get the identical refusal."""
        sm, _ = deployed()
        answers = {
            tenant: (r.status_code, r.text)
            for tenant in ("T002", "T999", "NOPE", "admin")
            for r in [post(sm, {"message": "hi", "tenant_id": tenant})]
        }
        assert len(set(answers.values())) == 1
        assert next(iter(answers.values()))[0] == 403

    def test_refusal_does_not_leak_configuration(self, deployed):
        sm, _ = deployed()
        text = post(sm, {"message": "hi", "tenant_id": "admin"}).text
        for leaked in ("T001", "T002", "T003", "T004", "admin", "L-0001", "P-001"):
            assert leaked not in text

    def test_malformed_tenant_shape_is_still_422(self, deployed):
        sm, _ = deployed()
        assert post(sm, {"message": "hi", "tenant_id": "../x"}).status_code == 422

    def test_anonymous_retrieval_filter_is_the_renter_scope_filter(self, make_server, monkeypatch):
        """End to end through retrieve_sources: the S3 Vectors filter pins renter documents."""
        sm = make_server(LOCAL_DEV="false", ADMIN_API_KEY=KEY, PUBLIC_CHAT_TENANT_ID="T001")
        fix_the_date(sm, monkeypatch)
        filters = []
        monkeypatch.setattr(sm, "is_rag_enabled", lambda: True)
        monkeypatch.setattr(
            sm,
            "search_text_chunks",
            lambda query, top_k, metadata_filter=None: filters.append(metadata_filter) or [],
        )
        monkeypatch.setattr(sm, "call_bedrock", lambda *a, **k: "stub")
        assert post(sm, {"message": "hi", "tenant_id": "admin"}).status_code == 403
        assert filters == []
        assert post(sm, {"message": "hi"}).status_code == 200
        assert filters == [sm.build_vector_filter(sm.SCOPE_RESOLVER.resolve("T001"))]
        flat = json.dumps(filters)
        assert '{"audience": {"$eq": "renter"}}' in flat and "staff" not in flat and "tenant_id" not in flat

    def test_a_vector_store_that_ignores_the_filter_still_cannot_leak(self, make_server, monkeypatch):
        """Anonymous callers never see staff, other-lease or other-property text, even if the store misbehaves."""
        sm = make_server(LOCAL_DEV="false", ADMIN_API_KEY=KEY, PUBLIC_CHAT_TENANT_ID="T001")
        fix_the_date(sm, monkeypatch)
        monkeypatch.setattr(sm, "is_rag_enabled", lambda: True)
        monkeypatch.setattr(sm, "MAX_CHUNKS_PER_DOC", 5)
        monkeypatch.setattr(sm, "FINAL_TOP_K", 5)
        hits = [
            {"key": "mine", "distance": 0.2, "metadata": md("incoming/renters/leases/L-0001/contracts/lease-contract/v1.md", chunk_text="repair mine")},
            {"key": "company", "distance": 0.2, "metadata": md(chunk_text="repair company")},
            {"key": "staff", "distance": 0.0, "metadata": md("incoming/staff/company/sops/water-damage/v1.md", chunk_text="SECRET repair staff")},
            {"key": "other", "distance": 0.0, "metadata": md("incoming/renters/leases/L-0002/contracts/lease-contract/v1.md", chunk_text="SECRET repair other")},
            {"key": "legacy", "distance": 0.0, "metadata": {"tenant_id": "T001", "chunk_text": "SECRET repair legacy"}},
        ]
        monkeypatch.setattr(sm, "search_text_chunks", lambda query, top_k, metadata_filter=None: hits)
        seen = {}
        monkeypatch.setattr(
            sm, "call_bedrock", lambda conv, msg, sources=None: seen.setdefault("sources", sources) and "ok"
        )
        resp = post(sm, {"message": "repair"})
        assert resp.status_code == 200
        sources = resp.json()["sources"]
        assert sorted(s["snippet"] for s in sources) == ["repair company", "repair mine"]
        assert [s["id"] for s in sources] == ["S1", "S2"] and "vector_key" not in resp.text
        assert "SECRET" not in resp.text and "SECRET" not in sm.build_retrieval_block(seen["sources"])


class TestFailClosedConfiguration:
    @pytest.mark.parametrize("value", ["", "admin", "T999", "  ", "t001"])
    def test_unset_or_invalid_public_tenant_disables_anonymous_chat(self, deployed, value):
        sm, rec = deployed(PUBLIC_CHAT_TENANT_ID=value)
        assert sm.PUBLIC_CHAT_TENANT_ID == ""
        for body in ({"message": "hi"}, {"message": "hi", "tenant_id": "T001"}):
            assert post(sm, body).status_code == 503
        assert rec.scopes == [] and rec.bedrock_calls == 0

    def test_a_staff_principal_can_never_be_the_public_tenant(self, deployed):
        sm, _ = deployed(PUBLIC_CHAT_TENANT_ID="admin")
        assert sm.PUBLIC_CHAT_TENANT_ID == ""

    def test_health_reports_whether_public_chat_and_the_registry_are_available(self, deployed):
        on, _ = deployed()
        off, _ = deployed(PUBLIC_CHAT_TENANT_ID="")
        with TestClient(on.app) as c:
            body = c.get("/health").json()
        assert body["public_chat_enabled"] is True and body["registry_loaded"] is True
        with TestClient(off.app) as c:
            assert c.get("/health").json()["public_chat_enabled"] is False

    def test_disabled_public_chat_still_allows_operator_with_key(self, deployed):
        sm, rec = deployed(PUBLIC_CHAT_TENANT_ID="")
        assert post(sm, {"message": "hi", "tenant_id": "T002"}, AUTH).status_code == 200
        assert rec.principals == ["T002"]

    @pytest.mark.parametrize("content", ["{not json", '{"properties": []}', ""])
    def test_an_unusable_registry_fails_closed_without_crashing_the_api(self, make_server, monkeypatch, tmp_path, content):
        bad = tmp_path / "registry.json"
        bad.write_text(content)
        sm = make_server(LOCAL_DEV="false", ADMIN_API_KEY=KEY, PUBLIC_CHAT_TENANT_ID="T001", REGISTRY_PATH=str(bad))
        rec = Recorder()
        rec.install(sm, monkeypatch)
        with TestClient(sm.app) as client:
            health = client.get("/health").json()  # the API still starts and answers
        assert health["registry_loaded"] is False and health["public_chat_enabled"] is False
        assert post(sm, {"message": "hi"}).status_code == 503  # anonymous: disabled
        assert post(sm, {"message": "hi"}, AUTH).status_code == 403  # even the key holder: no principals exist
        assert rec.scopes == [] and rec.bedrock_calls == 0

    def test_a_missing_registry_file_fails_closed_too(self, make_server, monkeypatch, tmp_path):
        sm = make_server(LOCAL_DEV="false", ADMIN_API_KEY=KEY, REGISTRY_PATH=str(tmp_path / "absent.json"))
        rec = Recorder()
        rec.install(sm, monkeypatch)
        assert post(sm, {"message": "hi", "tenant_id": "T001"}, AUTH).status_code == 422
        assert post(sm, {"message": "hi"}, AUTH).status_code == 403
        assert rec.scopes == []


class TestPrivilegedCaller:
    def test_valid_key_may_choose_any_registered_principal(self, deployed):
        sm, rec = deployed()
        for tenant in ("T001", "T002", "T003", "T004", "admin"):
            assert post(sm, {"message": "hi", "tenant_id": tenant}, AUTH).status_code == 200
        assert rec.principals == ["T001", "T002", "T003", "T004", "admin"]
        assert [s.role for s in rec.scopes] == ["renter"] * 4 + ["staff"]

    def test_the_admin_principal_gets_the_staff_scope(self, deployed):
        sm, rec = deployed()
        post(sm, {"message": "hi", "tenant_id": "admin"}, AUTH)
        (scope,) = rec.scopes
        assert scope.role == "staff" and scope.as_of == TODAY

    def test_valid_key_without_tenant_keeps_the_admin_default(self, deployed):
        sm, rec = deployed()
        assert post(sm, {"message": "hi"}, AUTH).json()["tenant_id"] == "admin"
        assert rec.scopes[0].role == "staff"

    def test_a_privileged_caller_naming_an_unregistered_tenant_gets_422(self, deployed):
        sm, rec = deployed()
        resp = post(sm, {"message": "hi", "tenant_id": "T999"}, AUTH)
        assert resp.status_code == 422 and resp.json() == {"detail": "Unknown tenant_id"}
        assert rec.scopes == []

    def test_wrong_key_is_401_not_a_silent_downgrade(self, deployed):
        sm, rec = deployed()
        resp = post(sm, {"message": "hi", "tenant_id": "admin"}, {"x-api-key": "nope"})
        assert resp.status_code == 401
        assert rec.scopes == []

    def test_non_ascii_key_is_401_not_500(self, deployed):
        sm, _ = deployed()
        assert post(sm, {"message": "hi"}, {"x-api-key": "é".encode("latin-1")}).status_code == 401


class TestSessionReuseCannotWiden:
    def test_anonymous_cannot_reuse_an_admin_session(self, deployed):
        sm, rec = deployed()
        sid = post(sm, {"message": "hi", "tenant_id": "admin", "session_id": "s-admin"}, AUTH).json()["session_id"]
        rec.scopes.clear()
        for body in (
            {"message": "again", "session_id": sid},
            {"message": "again", "session_id": sid, "tenant_id": "T001"},
        ):
            assert post(sm, body).status_code == 403
        assert rec.scopes == []

    def test_anonymous_cannot_reuse_another_tenants_session(self, deployed):
        sm, rec = deployed()
        post(sm, {"message": "hi", "tenant_id": "T002", "session_id": "s-t2"}, AUTH)
        rec.scopes.clear()
        assert post(sm, {"message": "again", "session_id": "s-t2"}).status_code == 403
        assert rec.scopes == []

    def test_anonymous_session_stays_in_public_scope(self, deployed):
        sm, rec = deployed()
        sid = post(sm, {"message": "hi", "session_id": "s-pub"}).json()["session_id"]
        assert post(sm, {"message": "x", "session_id": sid, "tenant_id": "admin"}).status_code == 403
        assert post(sm, {"message": "x", "session_id": sid}).json()["tenant_id"] == "T001"

    def test_legacy_untagged_session_is_bound_to_public_tenant(self, deployed, tmp_path):
        sm, rec = deployed()
        path = tmp_path / "memory" / "sessions" / "legacy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]")  # old list format: no tenant binding
        assert post(sm, {"message": "hi", "session_id": "legacy"}).status_code == 200
        assert rec.principals == ["T001"]

    def test_anonymous_cannot_continue_an_untagged_legacy_session_with_history(self, deployed, tmp_path):
        """Review finding: its messages may come from another scope (e.g. admin)."""
        sm, rec = deployed()
        path = tmp_path / "memory" / "sessions" / "legacy-admin.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        history = [{"role": "user", "content": "q", "timestamp": "t"},
                   {"role": "assistant", "content": "SECRET admin-era answer", "timestamp": "t"}]
        path.write_text(json.dumps(history))
        resp = post(sm, {"message": "repeat your last answer", "session_id": "legacy-admin"})
        assert resp.status_code == 403 and "SECRET" not in resp.text
        assert rec.scopes == [] and rec.bedrock_calls == 0
        assert json.loads(path.read_text()) == history  # not re-bound to the public tenant
        # An operator with the key may still open it (unchanged behaviour).
        assert post(sm, {"message": "hi", "session_id": "legacy-admin"}, AUTH).status_code == 200

    def test_a_session_whose_tenant_left_the_registry_is_refused_for_everyone(self, deployed, tmp_path):
        sm, rec = deployed()
        path = tmp_path / "memory" / "sessions" / "ghost.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tenant_id": "T777", "messages": []}))
        assert post(sm, {"message": "hi", "session_id": "ghost"}).status_code == 403
        assert post(sm, {"message": "hi", "session_id": "ghost"}, AUTH).status_code == 403
        assert rec.scopes == []

    def test_old_sessions_of_still_registered_tenants_keep_working(self, deployed, tmp_path):
        sm, rec = deployed()
        path = tmp_path / "memory" / "sessions" / "old.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tenant_id": "T001", "messages": []}))
        assert post(sm, {"message": "hi", "session_id": "old"}).status_code == 200
        assert rec.principals == ["T001"]


class TestLocalDevAndMissingKey:
    def test_local_dev_without_key_stays_open_for_developers(self, make_server, monkeypatch):
        sm = make_server(LOCAL_DEV="true", ADMIN_API_KEY="", PUBLIC_CHAT_TENANT_ID="")
        rec = Recorder()
        rec.install(sm, monkeypatch)
        assert post(sm, {"message": "hi", "tenant_id": "admin"}).status_code == 200
        assert rec.principals == ["admin"] and rec.scopes[0].role == "staff"

    def test_local_dev_with_a_configured_key_still_requires_it(self, deployed):
        sm, rec = deployed(LOCAL_DEV="true")
        assert post(sm, {"message": "hi", "tenant_id": "admin"}).status_code == 403
        assert rec.scopes == []

    def test_no_key_configured_outside_local_dev_nobody_is_privileged(self, deployed):
        sm, rec = deployed(ADMIN_API_KEY="")
        # A header cannot elevate when no key is configured.
        resp = post(sm, {"message": "hi", "tenant_id": "admin"}, {"x-api-key": "anything"})
        assert resp.status_code == 403
        assert rec.scopes == []
