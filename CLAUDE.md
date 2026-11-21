# CLAUDE.md - prop-assist-twin

This is the repository-level operating guide for Claude Code and other coding agents working on `prop-assist-twin`. Read this before changing implementation, tests, or infrastructure. Prefer the checked-out code over this document when they differ, and update this guide when a change alters a documented contract.

> Review basis: the uploaded project archive (`backend/`, `frontend/`, `terraform/`, `scripts/`) **plus separately supplied root `README.md` and `deploy.yml`**, which documents the `.github/workflows/deploy.yml` workflow. The archive did not itself include the root README, workflow folder, `.gitignore`, or the README-mentioned `temp/build_corpus_markdown.py`. The CI section below describes the supplied YAML, not a verified GitHub run; compare all externally supplied files with the real checkout before editing. No live AWS operations were executed as part of this review.

## 1. Purpose and stack

A proof-of-concept AI real-estate assistant for **XYZ Immobilienverwaltung GmbH**, presenting the assistant persona **Marie Stein**. The client is a static Next.js chat interface. The server is a separate Python/FastAPI API, hosted on AWS Lambda via Mangum in deployment. Conversations use Bedrock Nova; optional retrieval-augmented generation (RAG) grounds answers in tenant-scoped markdown documents embedded by SageMaker and indexed in S3 Vectors. Document indexing is primarily event-driven and separate from chat requests.

| Layer | Actual implementation |
| --- | --- |
| Web UI | Next.js **16.1.6** App Router, React **19.2.3**, TypeScript, Tailwind CSS 4, `lucide-react` |
| Frontend deploy | `output: 'export'` in `frontend/next.config.ts`; build output `frontend/out/` hosted in an S3 website bucket behind CloudFront |
| API | Python **3.12** (`backend/.python-version`), FastAPI, Pydantic v2, boto3, Mangum, Uvicorn |
| Answer LLM | Bedrock Converse API, default `eu.amazon.nova-pro-v1:0` |
| Query rewriting | Bedrock Converse API, default `eu.amazon.nova-micro-v1:0`; only for conversations with history |
| Embeddings | SageMaker `InvokeEndpoint`, default Hugging Face model `sentence-transformers/all-MiniLM-L6-v2` |
| Retrieval | S3 Vectors, cosine distance by default, lexical/vector reranking, per-document source diversity |
| Ingestion | Versioned S3 source bucket -> SQS -> worker Lambda -> SageMaker + S3 Vectors; DynamoDB manifest, DLQ and CloudWatch alarms |
| Session history | Local JSON files in development or S3 `sessions/<session_id>.json` in deployed Lambda |
| Infrastructure | Terraform AWS provider 6.x; API Gateway HTTP API, Lambda, S3, CloudFront, optional Route 53 + ACM |

**Do not treat this as a Next.js full-stack application.** There are no Next.js API routes in the supplied frontend. The browser calls a separate FastAPI endpoint.

## 2. Repository map: edit the correct owner

```text
backend/
  server.py                    FastAPI app, chat orchestration, env config, retrieval, admin API
  context.py                   Marie persona/system prompt and follow-up query rewrite prompt
  lambda_handler.py            Mangum handler for FastAPI API Lambda
  deploy.py                    Docker-based Python 3.12 x86_64 Lambda ZIP builder
  pyproject.toml               Python dependencies, pytest settings, >=90% coverage gate
  uv.lock                      Lockfile for uv development environment
  requirements.txt             Separate Lambda build dependency list (keep in sync)
  eval_chat.py                 Live HTTP evaluation harness; NOT a mocked unit test
  evals/golden_chat_cases.jsonl  Four golden prompts and response assertions
  me.txt                       Additional persona notes; not imported by current code
  data/kb/*.md                 Five example knowledge documents; NOT auto-indexed by source control
  common/
    config.py                  Import-time configuration for ingestion/shared modules
    models.py                  SourceItem Pydantic model
    chunking.py                RecursiveCharacterTextSplitter helper
    embeddings.py              SageMaker endpoint invocation and response parsing
    vector_store.py            S3 Vectors put/query/delete functions
  ingestion/
    worker.py                  SQS Lambda handler with partial-batch failure contract
    ingest_document.py         S3 object validation, chunk/embed/index and deletion logic
    manifest.py                DynamoDB status/metadata/idempotency helpers
  tests/                       Unit/integration-style pytest tests using fake AWS clients
frontend/
  app/page.tsx                 Single-page layout around Twin component
  app/layout.tsx               Fonts/metadata/html document
  app/globals.css              Tailwind entrypoint and typing-indicator animations
  components/twin.tsx          Client-side chat state, API fetch, tenant picker, sources
  next.config.ts               Static export config (no Next server runtime)
  package.json                 npm scripts and dependency versions
  package-lock.json            npm dependency lockfile
  tsconfig.json                Strict TypeScript settings, @/* path alias
  eslint.config.mjs            Next.js + TypeScript ESLint configuration
  public/                      Default/static SVG assets
terraform/
  main.tf                      API, networking, storage, vectors, SageMaker, CloudFront
  ingestion.tf                 S3->SQS->Lambda ingestion resources, IAM, alarms
  variables.tf                 Infrastructure feature flags, settings and defaults
  outputs.tf                   Deployment outputs used by scripts and operators
  backend.tf                   S3 remote state backend placeholder
  versions.tf                  AWS Terraform provider requirements and region aliases
  terraform.tfvars             Environment-specific input values: treat as sensitive
scripts/
  deploy.sh                    Builds Lambda ZIP, applies Terraform, builds/uploads frontend
  destroy.sh                   Destructive Terraform teardown and S3 bucket cleanup
README.md                      Root architecture, setup, deployment and K2-corpus guide
.github/workflows/deploy.yml   GitHub Actions deployment workflow (supplied separately as deploy.yml)
```

The five markdown KB files cover listings, areas, buying process, company FAQ, and service areas. Merely shipping them in the Lambda ZIP does **not** upload them to the ingestion S3 bucket or write their vectors. The root `README.md` and `deploy.yml` were supplied after the code archive; their paths are documented here but were not present in that archive.

## 3. Request and data-flow contracts

### 3.1 Browser -> chat API -> answer

```text
frontend/components/twin.tsx
  POST ${NEXT_PUBLIC_API_URL}/chat
       {message, session_id?, tenant_id}
    -> backend/server.py:chat()
       -> load_conversation(session_id) from S3 or local JSON
       -> keep the stored tenant if the session already exists
       -> rewrite_query() with Nova Micro if history exists
       -> retrieve_sources(query, effective tenant)
          -> SageMaker embed_text() -> S3 Vectors query_vectors()
          -> distance filter, custom lexical rerank, diversity cap
       -> call_bedrock() using Nova Pro + retrieved source block
       -> save_conversation() with effective tenant
    <- {response, session_id, tenant_id, sources, retrieval_used}
  Render response with [S1], [S2], ... source cards
```

- `frontend/components/twin.tsx` defaults the UI tenant to `T001`; server-side `ChatRequest` defaults to `admin` when the caller omits the field. Do not confuse these defaults.
- Frontend session ID is React state (not persistent browser storage). The tenant dropdown locks after the first successful response; this is only UI behavior, **not authentication**.
- The API stores a UUID for a new session. Existing sessions honor the stored tenant, even if a later request submits a different `tenant_id`. Old list-format session files have no tenant binding.
- Only the last **20** history messages go to the answer model. Query rewriting uses the last **5** history messages, not the entire transcript.
- `context.prompt()` is the active persona. It instructs same-language responses, conservative factual grounding, and inline `[S#]` citations. `backend/me.txt` is not loaded or used by `server.py`.
- Answer generation continues with no retrieved sources if RAG is disabled/misconfigured or retrieval fails; do not represent those answers as verified by a knowledge base.

### 3.2 Retrieval and citations

`server.py` controls the retrieval path; `common/` provides AWS/client-agnostic helpers.

1. `is_rag_enabled()` requires `RAG_ENABLED` **and** all three nonempty values `SAGEMAKER_ENDPOINT`, `VECTOR_BUCKET`, `VECTOR_INDEX`.
2. For `T001` or `T002`, retrieval forwards the S3 Vectors metadata filter `{"tenant_id": {"$eq": "T001"}}` (substitute the actual tenant). For `admin` or `None`, **no** tenant filter is applied.
3. Query embedding calls SageMaker with `{"inputs": text}`. `common/embeddings.py` handles common nested vector-response shapes.
4. Fetch `RAW_FETCH_SIZE` candidate vectors (default **12**); optionally reject vectors whose distance exceeds `MAX_RETRIEVAL_DISTANCE` (code default unset; Terraform default **0.5**).
5. Rerank candidates via `(1.0 - distance) + (0.5 * lexical_score)`. The lexical score is a **custom single-document BM25-inspired approximation**, NOT corpus-based BM25, even though `rank-bm25` is a declared dependency.
6. Keep at most `MAX_CHUNKS_PER_DOC` (**2**) chunks per document, then return up to `FINAL_TOP_K` (**3**) source items.
7. Document grouping (`_document_group_key`) uses `source_path`, else `source_key`, else `title`, else `unknown`, prefixed with `tenant_id` when present (so same-named docs of different tenants stay distinct under admin retrieval). `source_key` is used for grouping only and is never exposed in responses or the prompt. `shorten_snippet` normalizes whitespace; response snippets default to **280** chars and model context to **1,500** chars per source.
8. `build_retrieval_block()` numbers final sources `[S1]`, `[S2]`, etc. `SourceItem.context` is internal (`exclude=True`) and must not appear in public JSON.

**Metadata compatibility:** the ingestion worker writes `source_key` but not `source_path`, whereas `/ingest` writes `source_path` and `tenant_id` (no `source_key`). Thus worker-indexed sources have a null `source_path` in chat responses (deliberately: the raw S3 key reveals bucket layout and tenant folders), but grouping still distinguishes them via `source_key`. Both paths currently set `doc_type` from the filename suffix (usually `.md`), while the `README.md` example shows `"markdown"`; follow code, not the illustrative example, when updating tests or clients. Consider compatibility and a backfill/migration before changing metadata keys.

### 3.3 Document ingestion (production path)

```text
S3 versioned rag-docs bucket: incoming/.../{tenant}/{document}.md
  S3 ObjectCreated notification, suffix filtered
    -> SQS ingest queue
       -> ingestion.worker.handler (Lambda; ReportBatchItemFailures)
          -> ingestion.ingest_document.process_document()
             S3 GetObject [specific VersionId when provided]
             -> UTF-8/extension/size validation
             -> RecursiveCharacterTextSplitter
             -> concurrent SageMaker embeddings (adaptive retries)
             -> deterministic S3 Vector IDs and tenant_id metadata
             -> DynamoDB ingestion manifest
         transient errors -> retry -> SQS DLQ after maxReceiveCount
  S3 ObjectRemoved:Delete notification
    -> ingestion.ingest_document.process_deletion()
       -> remove vector IDs from matching manifest version
       -> mark manifest record DELETED
```

- `parse_source_key()` requires a key beginning `incoming/` with at least three path components. Tenant is **the immediate parent folder** of the file: `incoming/T001/faq.md` and `incoming/Tenants/T001/faq.md` both resolve to `T001`.
- Default supported extension is `.md` (configured via `SUPPORTED_SUFFIXES`); documents must be nonempty UTF-8 and at most `MAX_UPLOAD_BYTES` (**1,048,576** bytes). Defaults: `CHUNK_SIZE=1500`, `CHUNK_OVERLAP=200`.
- Use S3 `VersionId`, if supplied or returned by the object, else content SHA-256 as `source_version`. Manifest sort key: `source_key#source_version`; partition key: `tenant_id`.
- Chunk vector ID: `{tenant_id}/{doc_id}/{source_version}/{chunk_index}`. Metadata includes `tenant_id`, `source_bucket`, `source_key`, `source_version`, `title`, `doc_type`, `chunk_index`, `chunk_text`, `embedding_model`, `ingested_at`.
- `manifest.py` defines `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `SKIPPED`, `DELETED`, `SUPERSEDED`; the active ingest code writes `RUNNING`/`SUCCEEDED`/`FAILED`/`SKIPPED`/`SUPERSEDED`, and deletion writes `DELETED`.
- `SUCCEEDED` + same SHA-256 is considered a duplicate and marked `SKIPPED`; other cases get (re)indexed. Vector writes may run in a thread pool with `INGESTION_MAX_WORKERS` limited to **1..8** (default **4**).
- **Permanent** invalid documents (`InvalidDocumentError`) are logged/acknowledged and are not sent to the DLQ; when a manifest key can be established, validation failures mark it `FAILED`. **Transient** unexpected errors return an SQS `batchItemFailures` entry for retry. Do not replace this distinction with blanket exception swallowing.
- Deletion requires a matching *versioned* manifest record, uses `common.vector_store.delete_vectors()` (batches of at most **500** IDs), then marks history `DELETED`.

**Be careful with S3 versioning:** validate how real `ObjectRemoved:Delete`/`DeleteMarkerCreated` events carry version IDs before changing deletion behavior. The currently configured notification only subscribes to `s3:ObjectRemoved:Delete`, while deletion logic expects an indexed object's matching version. **Overwrites:** after a successful (or skipped) ingest, `_supersede_stale_versions()` reads the key's current version via `HeadObject` and deletes vectors of every other `SUCCEEDED`/`SKIPPED` version (manifest `SUPERSEDED`, found via `manifest.list_versions()` = DynamoDB `Query`, requiring `dynamodb:Query` IAM). Using S3's current version, not "the version just processed", keeps out-of-order delivery safe. Cleanup is skipped for unversioned buckets and when the object is gone (404/delete marker). Not handled: permanently deleting the *current* version makes S3 serve the previous version again, whose vectors were already superseded.

**Idempotency edge case:** `process_document()` skips only prior `SUCCEEDED` records, but the skip changes status to `SKIPPED`. A *third* delivery of the same event may therefore reindex. Preserve deterministic vector IDs and add explicit repeated-delivery tests before adjusting this behavior.

### 3.4 Locally exposed synchronous ingestion

`POST /ingest` is only **registered** when `LOCAL_DEV=true`. It accepts multipart fields `file` and a **required** `tenant_id` (pattern + `ALLOWED_TENANT_IDS`; missing/malformed -> 422, unknown -> 400), validates a safe filename, supported extension, UTF-8, and upload size, then indexes chunks synchronously with `tenant_id` metadata and vector IDs `{tenant_id}/{filename}_{n}`. It is guarded by `require_admin_api_key` unless local mode explicitly permits an absent key. This is **not** the deployed production ingestion path. API Gateway Terraform does not define a `/ingest` route.

## 4. HTTP contract and error behavior

Defined in `backend/server.py`:

| Method/path | Contract | Access |
| --- | --- | --- |
| `GET /` | Informational assistant/storage/model response | Public |
| `GET /health` | Returns configured feature flags and `status: healthy`; **not** a live AWS service health probe | Public |
| `POST /chat` | `{message: string, session_id?: string, tenant_id?: string}` -> `ChatResponse` | Currently public; caller supplies tenant |
| `POST /embed` | `{text: string}` -> `{embedding: number[]}` | `x-api-key` admin gate |
| `GET /conversation/{session_id}` | Returns persisted history | `x-api-key` admin gate |
| `POST /ingest` | Multipart `file` + `tenant_id` -> indexed chunk count | Only in `LOCAL_DEV=true`; admin gate |

`ChatRequest.message`: length **1..MAX_MESSAGE_CHARS** (default **3000**). Session IDs: `^[A-Za-z0-9_-]{1,64}$`. Tenant IDs must match `^[A-Za-z0-9_-]{1,32}$` **and** one of `T001`, `T002`, `admin` from `common/config.py`.

`ChatResponse`: `response`, `session_id`, `tenant_id`, `sources: SourceItem[]`, `retrieval_used`. Source fields: `id`, `title?`, `source_path?`, `snippet`, `doc_type?`, `chunk_index?`, `distance?`; internal `context` is excluded from serialization.

Admin access: when `ADMIN_API_KEY` is configured, verify `x-api-key` via `hmac.compare_digest`. When missing and `LOCAL_DEV=true`, admin endpoints are **open** by design of this PoC. When missing outside local development, they return **503**; bad/missing key with a configured key returns **401**. `/chat` does **not** use this admin gate. Keep API Gateway route definitions synchronized with any new API paths.

## 5. Frontend implementation constraints

- `frontend/components/twin.tsx` is a `'use client'` component and owns chat messages, input state, loading, `sessionId`, `tenantId`, `isTenantLocked`, fetch requests and rendering. UI error strings are currently German and some product copy is mixed German/English.
- It sends `POST ${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}/chat` with JSON; it does not proxy through Next.js.
- After first successful reply, it stores the server's `session_id` and locks the tenant picker. Tenant IDs visible to users include **admin**: do not mistake the picker for authorization.
- The frontend renders the raw assistant response as React text (`whitespace-pre-wrap`) and separate source cards, not HTML/Markdown. Preserve safe escaping if adding rich text rendering.
- `frontend/app/page.tsx` supplies a single-page layout, `app/layout.tsx` sets metadata/fonts, and CSS comes from Tailwind 4 via `@import 'tailwindcss'`.
- `next.config.ts` specifies `output: 'export'` and `images.unoptimized: true`. **No Next server or runtime API environment exists after export.** `NEXT_PUBLIC_*` is compiled into the frontend at build time. `next start` is listed in `package.json` but is not the production serving strategy for this static export.
- `frontend/tsconfig.json` uses strict checking and `@/*` imports. Respect React/Next App Router client/server boundaries.

## 6. Local setup and verification

Execute commands from their documented directories, not arbitrarily from repo root. Python 3.12, uv, Node.js/npm, and AWS credentials (for live Bedrock/SageMaker requests) are prerequisites. There is no requirement for real AWS credentials to run the isolated pytest suite because its fixtures stub boto3 clients.

**Backend (terminal 1):**

```bash
cd backend
uv sync
# Provide required values through your shell or a local, untracked .env.
# For local route exposure, set LOCAL_DEV=true. Never expose dev endpoints publicly.
uv run uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

Without configured Bedrock credentials a real `/chat` request is not expected to work. The unit tests replace AWS calls with fakes. Local memory defaults to `MEMORY_DIR=../memory`, relative to the backend process working directory; when `USE_S3=true`, set `S3_BUCKET` and valid AWS access.

**Frontend (terminal 2):**

```bash
cd frontend
npm ci
# Optionally set NEXT_PUBLIC_API_URL=http://localhost:8000 in .env.local
npm run dev
# Browser: http://localhost:3000
```

**Checks before proposing a PR:**

```bash
cd backend && uv run pytest -q
cd frontend && npm run lint && npx tsc --noEmit && npm run build
cd terraform && terraform fmt -check -recursive
# After provider initialization and when infrastructure validation is relevant:
cd terraform && terraform init -backend=false && terraform validate
```

`backend/pyproject.toml` runs coverage for `server`, `common`, `ingestion`, has `--cov-report=term-missing`, and requires **90% overall coverage** (`fail_under = 90`). Prefer `uv run pytest tests/test_<area>.py -q` for targeted iteration, then run the complete suite. Tests re-import `server`, `common`, and `ingestion` to handle import-time env config. **Do not call AWS from tests**; use monkeypatch/fake clients as existing tests do.

**Live answer evaluation (requires a running API and its real dependencies):**

```bash
cd backend
API_URL=http://localhost:8000 uv run python eval_chat.py
```

`EVAL_CASES` and `EVAL_TIMEOUT_SECONDS` override case path and request timeout. `eval_chat.py` loads JSONL and checks response text/source/citation heuristics. Its four cases are **not** deterministic isolated unit tests, and passing them does not establish grounding correctness.

**Observed verification for this archive:** Python syntax compilation, `bash -n` for both scripts, frontend JSON parsing, and JSONL parsing passed. The full pytest, npm, Terraform and real AWS integration checks were **not executed** in this review environment. In particular Python 3.12 was unavailable there; do not claim those checks passed until re-run on a configured machine.

## 7. Environment and configuration ownership

**Read-time behavior matters:** `common/config.py` reads environment variables at module import; `server.py` has a separate API config block initialized at import. `server.py` imports `common.config` *before* calling `load_dotenv()`, so values supplied only in a local `.env` may be seen by `server.py` but missed by the already-imported shared config. Set important shared env variables in the process environment before import, or fix startup ordering with tests. The tests explicitly re-import modules after changing env.

Key application variables:

| Variable | Purpose / effective default |
| --- | --- |
| `DEFAULT_AWS_REGION` | boto3 clients, `eu-central-1` |
| `BEDROCK_MODEL_ID`, `BEDROCK_LIGHT_MODEL_ID` | answer and rewrite models; Nova Pro / Nova Micro defaults above |
| `USE_S3`, `S3_BUCKET`, `MEMORY_DIR` | session storage; `false`, empty, `../memory` |
| `CORS_ORIGINS` | comma-separated origins; `http://localhost:3000` locally |
| `NEXT_PUBLIC_API_URL` | browser API base URL; fallback `http://localhost:8000` |
| `RAG_ENABLED` | chat retrieval switch, `true` but inert if RAG services not set |
| `SAGEMAKER_ENDPOINT`, `VECTOR_BUCKET`, `VECTOR_INDEX` | required for active RAG; worker uses same resources |
| `RAW_FETCH_SIZE`, `FINAL_TOP_K`, `MAX_CHUNKS_PER_DOC` | retrieval pool 12, final 3, max 2 per document |
| `MAX_RETRIEVAL_DISTANCE` | code unset by default; Terraform passes 0.5 by default |
| `SOURCE_SNIPPET_CHARS`, `MAX_CONTEXT_CHARS` | 280 and 1500 characters |
| `CHUNK_SIZE`, `CHUNK_OVERLAP` | 1500 and 200 characters |
| `SUPPORTED_SUFFIXES`, `MAX_UPLOAD_BYTES` | ingestion `.md`, 1048576 bytes |
| `INGESTION_MAX_WORKERS`, `EMBEDDING_MAX_ATTEMPTS` | 4 and 10; workers clamped 1..8 |
| `MANIFEST_TABLE`, `RAG_DOCS_BUCKET`, `EMBEDDING_MODEL` | worker manifest/source/model configuration |
| `ADMIN_API_KEY`, `LOCAL_DEV` | admin gate, local-only ingestion registration; empty/false |
| `MAX_MESSAGE_CHARS`, `LOG_LEVEL` | 3000 and INFO |

Keep `backend/pyproject.toml`, `backend/uv.lock`, and **`backend/requirements.txt`** consistent when dependencies change: development uses uv; `backend/deploy.py` packages production dependencies with `pip install -r requirements.txt` inside the AWS Python 3.12 Docker image. Changing one dependency file alone can leave Lambda different from local development.

## 8. Infrastructure and deployment

### 8.1 Terraform feature gates

- `sagemaker_embedding_enabled` defaults to **false**. Enables SageMaker model, serverless endpoint and invoke permissions; the container image URI must be supplied for a real deployment.
- `s3vectors_enabled` defaults to **false**. Enables S3 Vectors bucket/index; default dimension **384**, metric `cosine`, index `property-kb`. The embedding model and vector index dimension must match.
- `rag_ingest_enabled` defaults to **false**. Adds versioned source-doc S3 bucket, S3 notifications, SQS, DLQ, DynamoDB manifest, worker Lambda and CloudWatch alarms. Requires the vector and embedding resources to actually exist.
- `rag_enabled` defaults to **true** but does not by itself provision RAG infrastructure. The chat API disables retrieval when endpoint/bucket/index are absent.
- `use_custom_domain` defaults **false**. If enabled, Route 53 and an ACM certificate in `us-east-1` are involved; changes can alter DNS and TLS behavior.

Terraform defaults/variables are in `terraform/variables.tf`, but the **provided** `terraform/terraform.tfvars` explicitly enables SageMaker, S3 Vectors and event-driven RAG ingestion (`sagemaker_embedding_enabled=true`, `s3vectors_enabled=true`, `rag_ingest_enabled=true`), and sets `raw_fetch_size=5`, `max_retrieval_distance=0.35`, `source_snippet_chars=200`, and `ingestion_max_workers=2`. These override module defaults. Deploy scripts also pass `-var="project_name=..."` and `-var="environment=..."`, which override `.tfvars`. Never echo or copy secrets from `.tfvars`, shell environments, Terraform state, or plan outputs into logs, code, or documentation.

### 8.2 Deployed AWS topology

- **Chat Lambda:** `lambda_handler.handler` / Mangum, Python 3.12 x86_64, S3-backed memory, Bedrock, optional vector-query permissions. API Gateway HTTP API registers GET `/`, GET `/health`, POST `/chat`, POST `/embed`; there is no production `/conversation` gateway route even though FastAPI defines it.
- **Worker Lambda:** `ingestion.worker.handler`, Python 3.12 x86_64, deployed using the **same** `backend/lambda-deployment.zip` as the chat Lambda. Receives SQS batch events with `ReportBatchItemFailures`; default worker Lambda timeout 300 seconds, memory 1024 MB, reserved concurrency 3, batch size 1.
- **Ingestion queue:** max receive count 5, source S3 notifications under `incoming/`, file suffix filters from `supported_suffixes`, DLQ retention 14 days; alarms for DLQ size, worker errors, oldest message age. Optional SNS actions require `alarm_sns_topic_arn`.
- **Frontend:** static `out/` files uploaded to a **public S3 website bucket** with CloudFront as a custom (HTTP) origin. This is a PoC hosting posture, not an Origin Access Control/private-origin setup.
- **Session memory:** separate S3 memory bucket under `sessions/*` with Lambda IAM permissions; don't put secrets or cross-tenant data in frontend-accessible S3 objects.
- **Terraform remote state:** `terraform/backend.tf` declares S3 backend without values. `scripts/deploy.sh` and `destroy.sh` expect a pre-existing state bucket `twin-terraform-state-${AWS_ACCOUNT_ID}` and DynamoDB lock table `twin-terraform-locks`, plus an appropriate configured AWS account.

### 8.3 Scripts and change control

`bash scripts/deploy.sh dev twin` (arguments default to `dev` and `twin`) builds the Lambda ZIP with Docker via `cd backend && uv run deploy.py`, initializes the state backend, selects/creates a Terraform workspace, and runs **`terraform apply -auto-approve`**. It then writes `frontend/.env.production` with the Terraform API URL, executes `npm ci`, lint, build and `aws s3 sync --delete`. **This changes deployed infrastructure and remote storage; obtain explicit approval first.** The root README's manual example uses `./scripts/deploy.sh dev prop-assist-twin` while the GitHub workflow passes only an environment, yielding the script's default project **`twin`**. Do not assume these targets have identical resource names or state. The script refers to `terraform/prod.tfvars` for `prod` **unconditionally**, but that file was absent from the uploaded archive; verify its existence before production deployment.

`bash scripts/destroy.sh <environment> [project]` empties buckets and runs **`terraform destroy -auto-approve`**. It is destructive and needs a separate explicit approval. Also verify AWS region before using it: `deploy.sh` defaults `DEFAULT_AWS_REGION` to `eu-central-1`, while `destroy.sh` defaults that environment variable to `us-east-1` if unset. Do not assume both scripts select the same state/region by default.

The Terraform AWS *default* provider uses the AWS CLI/environment configuration, whereas an aliased provider is fixed to `eu-central-1` and the ACM alias to `us-east-1`; align these with `default_aws_region` before deployment. Always inspect `terraform plan` and scope before applying, even when the script offers an automatic apply. The workflow was supplied separately from the project ZIP and is described in the next section.

### 8.4 GitHub Actions deployment workflow (separately supplied `deploy.yml`)

Treat the provided `deploy.yml` as the content of `.github/workflows/deploy.yml` **only after verifying it matches the checked-out workflow**. It is a deployment workflow, not just a CI test workflow:

- **Triggers:** every push to `main` deploys to GitHub environment **`dev`**; manual `workflow_dispatch` exposes `dev`, `test`, `prod` (default `dev`). The job sets `environment` to the selection, so repository environment approvals may protect it **if configured**; the YAML alone does not prove such protection exists. No pull-request trigger is defined.
- **Permissions/identity:** grants `id-token: write` and `contents: read`. `aws-actions/configure-aws-credentials@v4` assumes `secrets.AWS_ROLE_ARN` using GitHub OIDC, with region from `secrets.DEFAULT_AWS_REGION`. Keep the IAM role trust policy narrowly scoped to the intended repo/branches/environments. Do not add stored static AWS keys as a shortcut.
- **Toolchain:** `ubuntu-latest`, Python **3.12**, uv, Terraform (wrapper disabled for raw output capture), Node **20** with npm cache. This matches the backend `.python-version` and frontend constraints.
- **Checks, in current order:** `npm ci`, `npm run lint`, `NEXT_PUBLIC_API_URL=http://localhost:8000 npm run build`; `uv pip install -r requirements.txt --system || true` and `uv run deploy.py` (Docker Lambda ZIP); `terraform fmt -check -recursive`, `terraform init -backend=false`, `terraform validate`; `uv pip install -r requirements.txt --system` and `pytest -q` from `backend/`. The Python `requirements.txt` includes `pytest-cov`, so pytest enforces the **90%** coverage gate in `pyproject.toml`. The workflow does **not** explicitly run `npx tsc --noEmit`, `eval_chat.py`, or a deployed HTTP smoke test.
- **Deployment:** runs `./scripts/deploy.sh <environment>` with `TF_VAR_admin_api_key` from `secrets.ADMIN_API_KEY`; the script rebuilds the Lambda ZIP, calls `terraform apply -auto-approve`, then rebuilds the frontend with the **actual** Terraform API URL and syncs static assets to S3 with `--delete`. The initial frontend build's localhost API setting is a pre-deployment build check, **not** what the deploy script should publish.
- **Outputs/cache:** selects the Terraform workspace and reads `cloudfront_url`, `api_gateway_url`, and `s3_frontend_bucket`; searches the CloudFront distribution list for a matching S3 website-origin hostname and issues an invalidation (`/*`) if one is found; prints URL/bucket summary. This discovery relies on reconstructing an AWS website hostname: the authoritative origin in Terraform is `aws_s3_bucket_website_configuration.frontend.website_endpoint`. Prefer an explicit Terraform `cloudfront_distribution_id` output if this lookup is unreliable. Confirm that CloudFront invalidation ran rather than assuming it always succeeds.
- **Secrets referenced in this YAML:** `AWS_ROLE_ARN`, `DEFAULT_AWS_REGION`, `ADMIN_API_KEY`, and **`AWS_ACCOUNT_ID`**. The root README lists only the first three. The workflow exports `AWS_ACCOUNT_ID` during deploy, but `scripts/deploy.sh` independently obtains the AWS account ID using `aws sts get-caller-identity`, making that export redundant in the supplied script. GitHub Actions environment/repo secret configuration was **not** provided or verified.

**Security/operational guardrails:** before changing CI, check environment protection rules, Terraform state isolation, IAM role trust policy, secret handling, project name, region, and whether a `prod.tfvars` file is available. `main` push currently leads to an automatic `dev` apply; do not turn manual `prod` into an unreviewed deployment. `terraform init -backend=false` validates configuration without the live S3 state, and passing it is not proof that a later remote Terraform apply will succeed.

### 8.5 Knowledge seeding and the README-only K2 workflow

- The repository has five example markdown documents in `backend/data/kb/`. The README documents manual S3 uploads to `incoming/Tenants/${TENANT_ID}/` on the bucket obtained from `terraform -chdir=terraform output -raw rag_docs_bucket`; S3 notifications, not the build, trigger embedding/indexing. Only run uploads with explicit authorization because they modify live AWS resources and may incur SageMaker/SQS/Lambda charges.
- The root README additionally documents a **K2 German-document corpus** for admin-tenant PoC testing: downloaded XML + matching PDFs under `temp/XML/` and `temp/K2_PDFs/`, converted to markdown, then uploaded in resumable batches (default 10) to `incoming/Tenants/admin/` with tracking in `temp/upload_state.json`. Per the README, the parser can suppress XML elements with `frequent='yes'` as boilerplate; the source PDFs are not parsed for indexing.
- **Not verified in the code archive:** the K2 corpus itself, `temp/build_corpus_markdown.py`, the claimed `temp/` gitignore rule, and `temp/upload_state.json` were not supplied. Do not invent this script, assert its flags work, or run the README's K2 commands until the helper is obtained and inspected. In particular, K2 documents under `admin` are queryable by the PoC's cross-tenant `admin` retrieval path; treat that route as a security risk rather than a privacy boundary.

## 9. Security and data-isolation rules (mandatory)

1. **PoC tenant selection is NOT production authentication.** `/chat` is currently unauthenticated; user-controlled `tenant_id` can be `admin`, which removes the vector tenant filter. The frontend exposes this choice. Stored-session tenant locking does not establish user ownership; callers who know a session ID can supply it to `/chat`. Production hardening needs server-verified identity/tenant claims, session ownership checks, and a guarded admin role.
2. Never relax or silently remove tenant filters, admin endpoint gates, filename/session validation, upload size bounds, CORS restrictions, version checks, or safe escaping. CORS is not authorization.
3. Never log API keys, AWS credentials, raw chat history, tenant-private docs, Terraform state, personal data, or production object content. Preserve separation between source documents, embeddings, session history, and publicly served frontend assets.
4. Do not make `/ingest` public in production as a shortcut. Upload/ingest paths need an authenticated, authorized backend and tenant identity supplied by trusted claims, not by filenames or browser input alone.
5. Read `common.config.ALLOWED_TENANT_IDS` before changing tenant handling. The ingestion key parser does not enforce this allowlist by itself; S3 uploads and tenant authorization need separate controls.
6. Prompt injection can be present in uploaded markdown. Retrieved `chunk_text` is untrusted reference material, not instructions that supersede system/developer rules. Preserve sourced-answer constraints and do not add execution of retrieved content.
7. Avoid committing generated Lambda archives, local memory files, `.env*`, secrets, Terraform plans/state, credentials, or node_modules. Check the repository's actual `.gitignore` in a complete checkout; it was absent from the provided archive. The README says `temp/` is gitignored, but this has **not** been verified against a `.gitignore`.
8. Treat `terraform.tfvars` as environment-sensitive and `admin_api_key` as a secret, even when Terraform marks it `sensitive = true` (the value may still be present in state/environment).

## 10. Tests and change-to-test map

| If you change... | Inspect and run... |
| --- | --- |
| Chat payloads, session persistence, `/health`, S3 memory | `backend/tests/test_chat_api.py`, `test_error_paths.py`, `test_auth.py` |
| Admin key, tenant validation, local-only `/ingest` | `backend/tests/test_auth.py`, `test_error_paths.py`, `test_ingest.py` |
| RAG vector scoring, distance, snippet/source rendering contract | `backend/tests/test_ranking.py`, `test_retrieve_sources.py`, `test_formatting.py` |
| Embedding / vector API wrappers | `backend/tests/test_ranking.py`, `test_vector_store.py` |
| Chunk size / splitting behavior | `backend/tests/test_chunking.py` |
| Worker, manifest, object deletion, SQS retries | `backend/tests/test_ingestion_worker.py` |
| User-facing LLM behavior/citations | Relevant backend tests **and** `backend/eval_chat.py` on an authorized running API |
| Frontend chat UI | `npm run lint`, `npx tsc --noEmit`, `npm run build` (no frontend test suite included) |
| Terraform/deployment | `terraform fmt -check`, `terraform validate`, reviewed `terraform plan`; shell syntax checks |

Tests use deterministic fakes and monkeypatching instead of real AWS calls; add tests for **negative/error paths** and cross-tenant behavior whenever modifying API, RAG, or worker logic. Never loosen the 90% coverage threshold just to make a change pass.

## 11. Known sharp edges and follow-up investigations

These are findings from the provided implementation, not blanket claims that production currently fails:

- **Authorization gap:** the public chat endpoint accepts user-chosen tenants including `admin`; no verified principal or session-owner authorization is present. Fix before treating this as a real multi-tenant production service.
- **Worker vs API metadata mismatch:** event-driven worker has `source_key` but not `source_path`, while local `/ingest` has `source_path` and `tenant_id` but no `source_key`. Retrieval grouping handles both (`_document_group_key`); worker sources still return `source_path: null`. Do not expose `source_key` publicly, and do not silently assume identical schemas.
- **Version/delete lifecycle:** overwrites supersede older versions (see §3.3), but permanently deleting the current version does not re-index the now-current previous version; S3 deletion event types/version IDs and manifestation of delete markers need real-event tests. A successful skip changes status to `SKIPPED`, which does not satisfy the next identical-delivery skip condition.
- **Import-time `.env` order:** `common.config` is imported ahead of `load_dotenv()` in `server.py`; avoid discrepancies between chat and ingestion configuration.
- **RAG is feature-gated:** `rag_enabled=true` does not help until embedding and vector components are provisioned/configured. The uploaded development `terraform.tfvars` enables those resources, unlike the Terraform variable defaults; check the selected environment and deployment state. Do not promise source-grounded answers when `retrieval_used=false`.
- **Hosting/region:** public S3 website origin uses HTTP to CloudFront; `destroy.sh` differs from `deploy.sh` in fallback region. Review security posture and region consistency before deployment.
- **Infrastructure IAM:** chat Bedrock invoke permission currently uses a wildcard resource; SageMaker execution role uses AWS-managed FullAccess. Tighten for production via least privilege without breaking model/profile invocation.
- **`backend/me.txt` unused:** use `backend/context.py` for active persona changes unless explicitly adding a new loader.
- **Repository/document gaps:** the root README and workflow YAML were supplied independently, not in the archive. README's K2 `temp/build_corpus_markdown.py` and `.gitignore` were not supplied; neither was `terraform/prod.tfvars`, although the deploy script requires it for `prod`. Verify these paths in the real checkout before following the relevant instructions.
- **CI/deployment discrepancies:** the workflow's project name defaults to `twin` while the README's manual deployment example names `prop-assist-twin`; CI exports an `AWS_ACCOUNT_ID` secret that the deploy script re-derives; CI's initial localhost frontend build is overwritten by the deploy script's real-API build. Avoid accidental resource drift.
- **CI review gaps:** pushes to `main` start a deployment after lint/build, packaging, Terraform validation and pytest. No PR-specific CI trigger, explicit review step, frontend typecheck command, live API smoke test or `eval_chat.py` is declared in this provided YAML; GitHub environment protection settings were not supplied.
- **Documentation example mismatch:** README's response example uses `doc_type: "markdown"`; current ingestion code stores a supported suffix such as `.md`.

## 12. Workflow for future Claude Code changes

1. Read the relevant source, its tests, and the corresponding Terraform/frontend config **before** editing. Search for all callers and duplicated config surfaces (especially `server.py` vs `common/config.py`, `pyproject.toml` vs `requirements.txt`).
2. State the intended behavior and relevant invariants. For multi-tenant or ingestion changes, explicitly trace session ownership, tenant metadata, S3 object version, manifest status and vector IDs.
3. Make a small, focused change. Keep the API request/response contract and citation mapping consistent between `server.py`, `common/models.py`, tests, and `frontend/components/twin.tsx`.
4. Add/update regression tests, particularly for failures, tenant boundaries, duplicate SQS deliveries and deletes. Mock AWS; do not generate real resources during tests.
5. Run relevant tests, the full backend coverage suite, frontend checks/build, and Terraform validation when applicable. Report precisely what did and did not run; never manufacture passing results.
6. Review privacy, security, IAM, cost, regional configuration, cold-start effects and deployment consequences. Seek explicit approval for `apply`, `destroy`, uploads, writes to AWS or other consequential external actions.
7. Update this file only when implementation behavior, commands, architecture or conventions truly change. Summarize modified files, checks and remaining risks in the final handoff.

**Priority when information conflicts:** current source and tests > verified infrastructure plan/runtime evidence > root `README.md` and independently provided `deploy.yml` (after checking against the checkout) > this `CLAUDE.md`. Verify before deleting or rewriting existing behavior.
