# ADR 0002: Document key layout, access scopes and business versioning

- **Status:** Proposed. Owner decisions of 2026-11-22 are incorporated (property tier now in scope, per-version keys, synthetic registry). **Slices 1-5 are implemented** (pure key/front-matter/metadata module, registry, corpus validator, access scopes, the worker, manifest, `/ingest` and Terraform manifest key on the new layout, and scope-based retrieval; unit-tested with fakes, never run against AWS). Before merging, run the live filter check (`python -m common.filter_probe`, owner-approved) and `terraform plan`: see "Cutover".
- **Date:** 2026-11-22
- **Roadmap position:** prerequisite for Milestone 1 "synthetic domain fixtures" (CLAUDE.md §13.4) and an early start on Month 2 metadata (effective dates, document type). P0 containment was done first (ADR 0003).
- **Supersedes (when accepted):** the "tenant = parent folder" rule in CLAUDE.md §3.3 and the single-`tenant_id` retrieval filter in §3.2.

## Context

Today the only access dimension is `tenant_id` (`T001`, `T002`, `admin`), taken from the **immediate parent folder** of the file (`parse_source_key`):

- Only three tiers exist: one partition per renter, or everything (`admin`). A document for all renters, one building or one lease must be copied into every renter partition.
- Staff-only documents can only live in the `admin` partition.
- No document type, no effective dates, no business version; `doc_type` is the file suffix (`.md`).
- Any subfolder silently becomes a new "tenant"; IDs are not validated.
- Vector IDs use the filename stem, so equal filenames in different folders collide.
- `/ingest` and the worker write different metadata keys (CLAUDE.md §3.2/§11).

Owner decisions (2026-11-22):

- `T001` is a **tenant (renter)**. A lease has one or more participating tenants. A structured **SQL database** (tenants, leases, tickets) will follow and be used with these documents.
- Scopes: **company-wide**, **property** and **lease**. **Unit** level is later but must be addable. House rules are property documents.
- Two audiences: **renter-facing** and **staff-only** (SOPs, contractor policies). Staff may access everything.
- One bucket, separated by directory. All current documents and vectors are replaced by a synthetic corpus.
- One S3 key per **business version**; effective dates and versions from the start.
- Document types now: lease contract, house rules, repair (maintenance) policy, vendor SOPs, contractor policies, utility statements.

## Decision

### 1. Key grammar (single source of truth for audience, scope and document type)

```text
incoming/{audience}/company/{doc_type}/{doc_id}/v{n}.md
incoming/{audience}/properties/{property_id}/{doc_type}/{doc_id}/v{n}.md
incoming/renters/leases/{lease_id}/{doc_type}/{doc_id}/v{n}.md
```

`audience` is `staff` or `renters`. There is **no** `staff/leases` row: lease documents are renter-facing; staff reach them because staff see everything. A table-driven grammar:

| Audience | Scope | Example |
| --- | --- | --- |
| renters | company | `incoming/renters/company/policies/repair-policy/v1.md` (general terms, privacy notice, repair policy) |
| renters | property | `incoming/renters/properties/P-001/policies/house-rules/v2.md` |
| renters | lease | `incoming/renters/leases/L-0001/contracts/lease-contract/v1.md`, `.../statements/utility-2026/v1.md` |
| staff | company | `incoming/staff/company/sops/water-damage/v1.md`, `.../policies/contractor-policy/v1.md` |
| staff | property | `incoming/staff/properties/P-001/sops/heating-vendor/v1.md` (building-specific vendor material) |
| reserved | unit | `.../properties/{property_id}/units/{unit_id}/...` is rejected now; see "Adding the unit level" |

- `doc_type` is a closed vocabulary: `contracts`, `policies`, `sops`, `statements`.
- `doc_id` is a lowercase slug (logical document). `v{n}` is the **business version**; each version is its own S3 key. S3 object versioning only covers overwriting the *same key* (ADR 0001).
- IDs are opaque and PII-free: `T001`, `L-0001`, `P-001`. Keys appear in S3 events, SQS messages, manifest rows and logs, so no names or addresses.
- The parser is **strict**. Anything outside the grammar, unknown `doc_type`, malformed IDs or a non-`v{n}.md` filename raises `InvalidDocumentError` (permanent; acknowledged, not sent to the DLQ). Whether an ID exists in the registry is checked by the corpus validator and by the scope resolver at query time, not by the worker (the worker has no registry, and IAM/S3 write rights are the upload control, CLAUDE.md §9.5).
- Tenant IDs are **not** part of any key. Tenant to lease, and lease to property, are data (a static synthetic registry now; the SQL database later).

### 2. Front matter (descriptive metadata, flat and strict)

```yaml
---
title: Hausordnung Objekt P-001
language: de
effective_from: 2027-01-01
effective_to: 2027-12-31
---
```

`effective_to` is optional; omitting it means open-ended. Inline comments are not supported (so a title may contain `#`); a whole line starting with `#` is a comment.

Required: `title`, `language` (`de`|`en`), `effective_from`. Optional: `effective_to`. Unknown keys and invalid values raise `InvalidDocumentError`. Path owns audience, scope, IDs, `doc_type`, `doc_id`, version; front matter owns title, language and dates, so nothing is stored twice. A small dependency-free flat parser is used (no change to `pyproject.toml`/`uv.lock`/`requirements.txt`) and the block is stripped before chunking. Dates must be strict `YYYY-MM-DD` within 2000-2100 (Python 3.11+ `fromisoformat` would also accept `20270101` and week dates). Violations raise `common.doc_keys.DocumentFormatError` (a `ValueError` carrying every problem found); the worker will map it to `InvalidDocumentError` in slice 3.

### 3. Vector metadata (one shared builder for the worker and `/ingest`)

Added: `audience` (`staff`|`renter`), `scope_type` (`company`|`property`|`lease`), `property_id` (property scope only), `lease_id` (lease scope only), `doc_type`, `doc_id`, `doc_version` (int), `language`, `effective_from` and `effective_to` (ints `YYYYMMDD`; open-ended = `99991231`, avoiding `$exists`).
Kept: `source_bucket`, `source_key`, `source_version` (S3 VersionId or SHA-256), `title`, `chunk_index`, `chunk_text` (non-filterable), `embedding_model`, `ingested_at`. Removed: `tenant_id`.
**A lease document does not carry `property_id`.** Lease to property is registry/SQL data; denormalizing it into vectors would go stale (and require a reindex) when a lease moves. `doc_type` changes meaning from "file suffix" to the vocabulary above.
Vector ID: `{relative key without incoming/ and .md}/{percent-encoded source_version}/{chunk_index}` (S3 VersionIds may contain `/` or `+`; encoding keeps them one segment).

### 4. Manifest

Hash key `tenant_id` becomes `scope_key` = the key up to the scope id (`renters/company`, `renters/properties/P-001`, `renters/leases/L-0001`, `staff/company`, `staff/properties/P-001`); sort key `source_sk` unchanged. The key schema change forces a table replacement (acceptable, the corpus is rebuilt). ADR 0001 supersession is unchanged per key; business versions are different keys, so `v1` stays indexed when `v2` arrives.

### 5. Scope and retrieval (the seam for the SQL database)

New pure module `common/access.py` (no AWS):

```python
Scope(role: "staff" | "renter", lease_ids: frozenset, property_ids: frozenset, as_of: date)
ScopeResolver.resolve(principal) -> Scope   # StaticScopeResolver now, SqlScopeResolver later
build_vector_filter(scope) -> dict
is_authorized(metadata, scope) -> bool      # re-checked on every returned hit
```

- **Renter:** `audience = renter AND (scope_type = company OR (scope_type = property AND property_id IN property_ids) OR (scope_type = lease AND lease_id IN lease_ids)) AND effective_from <= as_of AND effective_to >= as_of`. `property_ids` is derived from the renter's leases; empty sets drop their branch (no empty `$in`). A renter with leases in two properties gets both.
- **Staff:** no audience/scope restriction; the as-of filter still applies so answers use the rule in force.
- `as_of` defaults to today (Europe/Berlin); tickets can later pass the incident date.
- `retrieve_sources` runs `is_authorized` on every hit as defence in depth and drops (and logs without content) any hit that fails.
- The request keeps `tenant_id` (contract unchanged); the server maps it through the resolver (`server.resolve_chat_scope`). P0 (ADR 0003) already pins anonymous callers server-side. **Refinement of the first draft:** instead of a separate fixed "anonymous demo scope", `PUBLIC_CHAT_TENANT_ID` (and its Terraform variable) is kept and now names a *registered tenant* whose renter scope anonymous callers receive; staff are rejected as the public tenant. This needs no Terraform change, and a dedicated demo tenant is simply another registry entry with its own lease. Either way the public demo can never reach staff, other-property or other-lease documents.
- **Uniform refusals.** The body only needs a well-formed `tenant_id`; membership is decided after privilege is known. Anonymous callers get the same 403 for any tenant but the pinned one, registered or not, so the response reveals nothing about which tenants exist (the former 422 listed them). A key holder naming an unregistered tenant gets 422 "Unknown tenant_id". An unusable registry file leaves the registry empty and every request refused (anonymous 503; key holder 403, or 422 when naming a `tenant_id`) without crashing the API.
- `retrieve_sources(..., *, scope)` takes a required keyword-only scope: there is no code path that retrieves without one. The unverified body identity for privileged callers remains a Month 3 item.
- `SqlScopeResolver` is the only addition when the SQL database arrives. Documents already use the future tables' IDs (`T001`, `L-0001`, `P-001`), and `doc_id`/`doc_version` give tickets stable citation targets.

### 6. Synthetic registry and corpus

Registry (synthetic, `backend/data/registry.json`, loaded and validated by `common/registry.py`; it lives in `data/` because the chat Lambda needs it at runtime from slice 2): properties `P-001`, `P-002`; leases `L-0001` (P-001), `L-0002` (P-002), `L-0003` (P-001); tenants `T001` → L-0001, `T002` → L-0002, `T003` and `T004` → L-0003 (co-tenants); `admin` = staff.
This gives the isolation tests: T001 (L-0001) and T003 (L-0003) share the P-001 house rules but not each other's lease documents; T003 and T004 share all L-0003 documents as co-tenants; T002 sees only P-002 and company documents and nothing of P-001; no renter sees `staff/`.
The corpus mirrors the bucket at `backend/corpus/incoming/...` so `aws s3 sync` is the upload (owner-approved only). It lives **outside `data/`** because `deploy.py` copies all of `data/` into the Lambda ZIP and staff-only documents must not ship in a deployment package. `uv run python -m common.corpus` (module `common/corpus.py`, also run by pytest in CI) checks grammar, UTF-8, size, front matter, that every `property_id`/`lease_id` exists in the registry, one `doc_type` per `doc_id` within a scope, versions that take effect in order, and **no overlapping effective windows between versions** (an open-ended predecessor must be closed). The five `backend/data/kb/*.md` files are removed in slice 5.

### 7. Adding the unit level later

Add one grammar row (`.../properties/{property_id}/units/{unit_id}/...`), a `unit_id` metadata key and filter branch, a `units` registry level (lease to unit), and `unit_ids` on `Scope`. No existing key moves; `scope_key` gains one more form. This is why `property_id` is not denormalized into lease vectors and why every ID is opaque.

## Options considered

| Topic | Options | Choice and why |
| --- | --- | --- |
| Where scope lives | A. folder per tenant (today); B. company / property / lease folders; C. tenant IDs in keys with an ACL table | **B.** Co-tenants share a lease; no duplication; no person IDs in keys; person to lease stays in SQL |
| Audience split | A. separate bucket; B. first path segment; C. metadata only | **B.** Owner chose one bucket; a top-level prefix allows prefix-scoped IAM later; enforcement is the retrieval filter plus `is_authorized` |
| Versioning | A. overwrite one key; B. one key per business version | **B.** Needed for effective dates, future-dated documents and "rule at incident date" |
| Property on lease vectors | A. denormalize at index time; B. resolve at query time | **B.** Avoids stale data and reindexing on lease changes |
| Front matter parser | A. PyYAML; B. flat strict parser | **B.** No dependency change in three files |
| Vector index | A. one index; B. one per audience | **A** for now; revisit if the filter proves error-prone |

## Consequences

- Breaking change for every key, vector ID, manifest row and the meaning of `doc_type`: full wipe and re-ingest, no backfill.
- Code touched: `common/doc_keys.py`, `common/access.py`, `common/registry.py`, `common/corpus.py`, `common/prepare.py` (all new), `common/config.py` (registry replaces `ALLOWED_TENANT_IDS`, slice 4), `ingestion/ingest_document.py`, `ingestion/manifest.py`, `ingestion/worker.py`, `server.py` (`/ingest`, filter, hit check, resolver), `terraform/ingestion.tf` (manifest key), tests, `evals/`, README, CLAUDE.md.
- Eval fixtures can reference expected `(doc_id, doc_version)`; that needs additive `doc_id`/`doc_version` on `SourceItem` (separate, backward-compatible API change).
- Retrieved text stays untrusted; the model never decides access.

## Implementation plan (one reviewable slice each; each adds tests; none touches AWS)

| # | Slice | Proof artifact |
| --- | --- | --- |
| 0 | **P0 containment** (ADR 0003) | Done: deny-path tests |
| 1 | **Done.** `common/doc_keys.py` (grammar table, front-matter parser, metadata and vector-ID builders), `common/registry.py`, `common/corpus.py` (`python -m common.corpus`), `data/registry.json`, `corpus/README.md` | 173 unit tests (grammar rows and rejections, front-matter errors, metadata shape, registry integrity, validator on tmp trees incl. window overlaps); four deliberate code breaks were each caught |
| 2 | **Done.** `common/access.py`: `Scope`, `ScopeResolver` protocol, `StaticScopeResolver`, `berlin_today`, `build_vector_filter`, `is_authorized`, `FILTER_OPERATORS_USED`; test-side filter evaluator | 154 tests: exact visibility per principal on a realistic 11-document universe, cross-lease/cross-property/staff-leak negatives, inclusive date boundaries, filter and check equal on every well-formed vector x scope x day (11 x 9 x 6), fail-closed on 18 malformed shapes for everyone plus 5 scope-id defects that hide a vector from every renter while staff still see it; five deliberate code breaks were caught |
| 3 | **Done.** `common/prepare.py` (shared key/size/UTF-8/front-matter/chunking/metadata step, also used by the corpus validator); worker, `/ingest` and manifest (`scope_key`) use it; Terraform manifest hash key `tenant_id` -> `scope_key` | 71 worker tests (every scope row, business versions coexist, supersession per key and scope, all legacy layouts acknowledged for create and delete events, manifest partitioned by scope), 28 `/ingest` tests, 14 `prepare` tests; five deliberate breaks caught. Worker tests take `DocumentFormatError` indirectly (via `handler` results) because `tests/conftest.py` pops `common.*` on every load |
| 4 | **Done.** `server.py`: registry loaded at start (fail-closed), `resolve_chat_scope`, `retrieve_sources(..., *, scope)` with `build_vector_filter` and a per-hit `is_authorized` check, `ALLOWED_TENANT_IDS` removed, tenant membership moved out of request validation, group key without `tenant_id`; `common/filter_probe.py` (owner-run live check); access-boundary eval cases updated | 40 containment tests (pinned scope, uniform refusals, ignored-filter leak, registry failure, session reuse), 8 retrieval tests, 28 probe tests; every deny path mutation-checked. **Live run on the `dev` index done 2026-11-23; see "Verification plan" (one finding, handled)** |
| 5 | **Done.** Synthetic German corpus in `backend/corpus/` (19 logical documents in 21 files: 2 properties, 3 leases incl. co-tenants, expired and current versions, a staff set; the old `data/kb` seed files removed); 100 labeled cases in `backend/evals/property_cases.jsonl` (17 categories, each naming its `expected_docs`); runner schema extended (`category`, `expected_docs`, `language`, results per category); `docs/use_case.md` | 216 alignment tests prove, offline, that every required fact exists in the cited evidence, every cited document is visible to the asking tenant, leak markers exist only outside the tenant's scope and outdated markers only in expired versions, and that results never depend on the calendar; five data mutations were caught. **Labels are not yet run against a live ingested deployment** |
| 6 | Additive `doc_id`/`doc_version` on `SourceItem`; evals reference them | Recall@k on expected document IDs |
| 7 | Cutover through the CI pipeline (below) | Reviewed `terraform plan`, manifest statuses, empty DLQ, baseline run |

## Verification plan

1. Unit tests as listed per slice (fakes only).
2. CI: corpus validator.
3. **dev only, owner-approved:** `cd backend && uv run python -m common.filter_probe --bucket <vector-bucket> --index <index> --yes`. It writes about twenty probe vectors (keys `__filter-probe__/<run>/...`, tagged with a unique `probe_run`), queries every registry principal on three dates with the exact filters `build_vector_filter` emits, compares each result with `is_authorized`, and deletes the probes (also on failure, then verifies). It confirms: the operators in `common.access.FILTER_OPERATORS_USED` are accepted, nested as built; no filter returns documents outside its scope (reported as `FILTER TOO LAX`) or hides authorized ones; malformed vectors (missing key, string or bool dates) match nothing, with array-valued metadata being the one known exception (below); and whether numbers come back as integers or floats. Run it when nobody is chatting (the probes look like documents while they exist). Failure modes without it: an unsupported operator makes every search fail, which `/chat` swallows as "no sources" (`retrieval_used=false`), so retrieval would be silently empty. Filterable-metadata size limits are not covered by the probe. Mock tests do not prove any of this, which is why the check exists.

**Result of the first live run (owner, `dev` index, 2026-11-23, 17 probes)**

- **Operators:** all six (`$and`, `$or`, `$eq`, `$in`, `$lte`, `$gte`) are accepted, nested as `build_vector_filter` emits them. Every query succeeded.
- **Dates and well-formed documents:** for every principal the returned set equalled the `is_authorized` set on all three dates (the 2026-12-31 boundary run was clean).
- **Missing keys and wrong types:** a missing `effective_to`, string dates and boolean dates matched nothing, as assumed.
- **Numbers:** integers come back as integers; a float written as `20260101.0` comes back as a float (the check accepts integral floats).
- **Cleanup:** the probes were deleted and the tool's verification query found none left.
- **One finding: array-valued metadata matches by ANY element.** The probe `audience: ["renter"]` was returned for `audience = renter` and `audience in [renter, staff]`. The first tool version called this "too lax". Consequences and handling: (1) ingestion cannot produce it: the path-derived fields are plain strings built from regex-validated segments, and a test pins that every filter field of a built document is a scalar; (2) `is_authorized` rejects every non-string where a string belongs, so the in-code check drops such a vector, which makes that check **load-bearing, not belt-and-braces**; a list spanning two leases (`lease_id: ["L-0001", "L-0002"]`) is returned for both tenants' filters and dropped for both; (3) the unit-test evaluator now models any-element array matching, so the unit tests reproduce this behaviour; (4) the probe classifies this exact case as a NOTE (and prints `ok*`), still fails on any other leak, and also tolerates a future index that stops matching arrays; four array-valued probes (`audience`, `audience` with `staff`, `scope_type`, `lease_id`) now cover it. A second run with the updated tool is expected to pass with notes.
- **Not covered:** filterable-metadata size limits, behaviour at real data volumes, and any other index or service version.

## Cutover (deployed through the CI pipeline)

Deployment is the GitHub Actions workflow: a push to `main` runs lint, build, `terraform validate`, pytest and then `scripts/deploy.sh`, which runs `terraform apply -auto-approve` against `dev`. There is no plan-review step, so **merging is the apply**. Nothing below happens until the branch is merged; the commits themselves deploy nothing.

**What one merge of slices 0-3 does to AWS:**

1. **Replaces the DynamoDB manifest table** (`aws_dynamodb_table.rag_ingestion_manifest[0]`). DynamoDB cannot alter a key schema, so Terraform destroys and recreates it (same name, so IAM and the worker's `MANIFEST_TABLE` are unchanged). Every manifest row is lost by design. Vectors indexed before the change are no longer tracked. If the apply fails between destroy and create, the worker fails transiently (SQS retries, then the DLQ) until a re-run of the workflow recreates the table.
2. **Updates the chat and worker Lambdas** (one ZIP): the worker now accepts only the new key layout; anonymous `/chat` is pinned by ADR 0003 and, with `public_chat_tenant_id` unset, returns 503 to everyone without the admin key.
3. **Rebuilds and syncs the frontend** (no tenant picker).

It does **not** touch the vector index, delete or upload any S3 object, or enable public chat.

**Merge gate.** Slice 4 (retrieval on the new scopes) is on the branch, so the worker's new schema and the retrieval filter now change together. The remaining gate is the live filter check below.

**Before merging** (CI does none of this): (1) run the live filter check (Verification plan item 3) against the `dev` index and fix anything it reports; (2) run `terraform plan` locally against the `dev` workspace. The expected changes are exactly the manifest-table replacement, the Lambda code/environment updates and the frontend objects. Anything else, in particular a change to the vector index, the buckets or the queue, means stop and investigate.

**After the deploy** (owner actions; CI performs none of them):

1. Old documents. The old `incoming/{tenant}/...` objects are inert: keys outside the new layout are acknowledged without being read. Permanently delete them (all versions) to tidy the bucket; the resulting `ObjectRemoved:Delete` events are acknowledged as well. A plain delete on the versioned bucket only creates delete markers (a different, unsubscribed event type) and removes nothing.
2. Old vectors. They stay in the index, untracked. After slice 4 every filter requires `audience` and the effective dates, which those vectors lack, so they are unreachable for every principal, staff included (fail-closed); they only cost storage. Removing them needs a purpose-built, owner-run cleanup (list the index, delete vectors without `audience`) or a recreated index; neither is part of the deploy. Unfiltered retrieval before slice 4 still sees them.
3. `cd backend && uv run python -m common.corpus --require-documents`, then `aws s3 sync` the validated corpus (README, "RAG knowledge ingestion"); check manifest statuses and the DLQ; run the eval baseline.
4. Only then enable public chat for an index that holds synthetic data only (ADR 0003): see "Open items" for how the variable reaches the pipeline.

## Open items

- How `public_chat_tenant_id` reaches the pipeline. The workflow only passes `TF_VAR_admin_api_key`. Options: (a) set it in the committed `terraform/terraform.tfvars` (simple, but a deployed-scope change then needs a commit and is per repo, not per environment); (b) add `TF_VAR_public_chat_tenant_id: ${{ vars.PUBLIC_CHAT_TENANT_ID }}` to the workflow so each GitHub environment sets its own (a CI change, needs the owner's approval, CLAUDE.md section 8.4). Not decided.
- The CI pipeline has no `terraform plan` step. A plan-and-approve job (or a required reviewer on the `dev` environment) would make this kind of schema change reviewable; also not decided.
- A cleanup tool for vectors indexed before this ADR (see Cutover, after-deploy step 2).
- Whether staff should later be scoped by property (today: everything).
