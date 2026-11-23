# ADR 0003: P0 containment of anonymous `/chat`

- **Status:** Accepted. Implemented and unit-tested with fake AWS clients; **not verified against a deployed environment** (see "Verification").
- **Date:** 2026-11-22
- **Roadmap position:** Milestone 1, P0 (CLAUDE.md §0, §9.1, §13.4). This is containment, **not** the Month 3 identity workstream.
- **Code:** `backend/server.py` (`resolve_chat_tenant`, `is_privileged_chat_caller`, `_resolve_public_chat_tenant`), `backend/tests/test_chat_containment.py`, `terraform/variables.tf` + `main.tf` (`public_chat_tenant_id`), `frontend/components/twin.tsx`.

## Context

`POST /chat` was anonymous and trusted the body field `tenant_id`. An omitted value defaulted to `admin`, which removes the vector tenant filter, so any caller (or the default UI picker) could retrieve across all partitions. A session ID reuse honoured the stored tenant, so a session created as `admin` stayed `admin`. The frontend picker and metadata filters are not authorization (§9.1).

## Options considered

| Option | Why not / why |
| --- | --- |
| A. Hide the picker only | Explicitly insufficient (§9.1); the API still accepts `admin` |
| B. Disable the public API Gateway route | Safe, but also removes the synthetic demo and the operator path; needs a Terraform apply and loses eval access |
| C. Remove `admin` from the allowlist | Breaks operator/local use and the existing partition model |
| **D. Server-pinned anonymous scope + key-gated privileged scope (chosen)** | Anonymous callers get exactly one configured synthetic tenant; anything else needs the admin API key. Fail-closed when unconfigured. Small, testable, and the seam for the future scope resolver |

## Decision

1. **Anonymous callers are pinned** to `PUBLIC_CHAT_TENANT_ID` (Terraform `public_chat_tenant_id`). Requesting another tenant, or reusing a session bound to another tenant, returns **403 before any retrieval or model call**. The refusal text contains no tenant names.
2. **Fail closed.** Empty (the Terraform default) or invalid (`admin`, unknown tenant) disables anonymous chat: **503**. The owner must set the variable deliberately, after confirming that tenant holds **synthetic data only**.
3. **Privileged callers** may choose any tenant, including `admin`: `x-api-key` matching `ADMIN_API_KEY` (constant-time, bytes comparison so non-ASCII input is a 401, not a 500). A supplied wrong key is **401**, never a silent downgrade. With no `ADMIN_API_KEY`, only `LOCAL_DEV=true` is privileged, and a header cannot elevate. A configured key is always required, even in `LOCAL_DEV`.
4. **The request cannot grant scope.** `ChatRequest.tenant_id` is optional; an omitted value no longer means `admin` for anonymous callers. Privileged callers keep the old default (`admin`) and the "stored session tenant wins" rule.
5. **Frontend:** the picker is shown only with `NEXT_PUBLIC_SHOW_TENANT_PICKER=true` (local development). Otherwise no `tenant_id` is sent. 403/503 get plain German messages.
6. `/health` adds `public_chat_enabled` (boolean only).

## Consequences

- **Deploying this change disables anonymous chat in every environment** until `public_chat_tenant_id` is set. This is intentional (§13.4 "disable until a fixed-scope synthetic demo is safe"). `main` auto-deploys to `dev`, so containment takes effect on merge.
- `/chat` now reads `x-api-key` (additive). The live eval harness sends none, so it runs in the public scope against a deployed API; it cannot test other tenants without a key.
- `ChatResponse.tenant_id` reports the effective scope; existing clients unaffected.

## Residual risks (not closed by this ADR)

- Anyone who knows a session ID of the **public** scope can continue that session. IDs are UUIDs but there is no ownership check (Month 3).
- The public tenant's contents are the owner's responsibility. Until the synthetic corpus replaces the old documents, **do not set the variable** for a partition that holds non-synthetic data.
- `/chat` is still unauthenticated and unthrottled for the public scope: abuse costs Bedrock and SageMaker money. API Gateway throttling or a WAF rate rule is a recommended follow-up; it is not part of this change.
- The shared admin key must never be placed in the frontend or any `NEXT_PUBLIC_*` variable.

## Verification

- Done: deny-path unit tests (omitted/explicit/other tenant, `admin`, session reuse including legacy untagged sessions, wrong/non-ASCII key, `LOCAL_DEV` combinations, fail-closed configuration, the exact S3 Vectors filter). A mutation check that weakened the policy made 9 tests fail.
- **Pending, owner-approved deploy only:** after a deployed `dev` run, run `EVAL_CASES=evals/access_boundary_cases.jsonl API_URL=... uv run python eval_chat.py` anonymously: `anon_admin_scope_denied` and `anon_unregistered_tenant_denied` must both PASS (403). With the variable unset both report ERROR (503), which also confirms anonymous chat is off. Mock tests do not prove deployed security (§13.7).

## Update (ADR 0002 slice 4)

Retrieval no longer filters on `tenant_id`; the pinned tenant now selects a **scope** (ADR 0002 section 5), and the decisions above still hold with these changes:

- `PUBLIC_CHAT_TENANT_ID` must be a **registered tenant** in `backend/data/registry.json` (never staff). The anonymous caller receives exactly that tenant's renter scope: company-wide documents, its properties' and its leases' documents in force today, never `staff/` documents.
- The refusal is uniform: any tenant other than the pinned one, registered or not, gets the same 403, so an anonymous caller cannot learn which tenants exist (a malformed `tenant_id` shape is still 422, and so is an unregistered one for a key holder). The earlier 422 that listed the valid tenants is gone.
- Every retrieved hit is re-checked in code against the scope; hits outside it are dropped and logged without content.
- An unusable registry fails closed without crashing the API (anonymous 503; a key holder gets 403, or 422 when naming a `tenant_id`, since no principal is registered).
- An anonymous caller cannot continue an untagged legacy (list-format) session that has history: its messages could come from another scope. Same 403. (Found in review, 2026-11-23.)
- Public source `id`s are positional (`S1`, ...); the vector key, which encodes scope, lease/property ids and S3 version ids, is never serialized. (Found in review.)

