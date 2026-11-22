# ADR 0001: Supersede stale vector versions when a source document is overwritten

- **Status:** Accepted. Implemented and covered by unit tests with fake AWS clients; **not yet verified against real S3/SQS events** (see "Verification plan").
- **Date:** 2026-11-22
- **Roadmap position:** Month 2, "document lifecycle" (CLAUDE.md §13.4), implemented early at the owner's request. It does not advance the active Milestone 1 (P0 containment, evaluation baseline).
- **Code:** `backend/ingestion/ingest_document.py` (`_supersede_stale_versions`, `_current_s3_version`), `backend/ingestion/manifest.py` (`list_versions`, `mark_status`, `SUPERSEDED`), `terraform/ingestion.tf` (`dynamodb:Query`).

## Context

The RAG source bucket is versioned. Every S3 overwrite produces a new `VersionId`, and the worker writes vectors under `{tenant_id}/{doc_id}/{source_version}/{chunk_index}`. So a new version gets **new** vector IDs. Before this change, the old version's vectors were never removed:

- outdated chunks stayed searchable and competed with the current content for the `MAX_CHUNKS_PER_DOC` slots, so answers could cite superseded text;
- the index and the manifest grew with every overwrite.

Constraints:

- SQS does not guarantee order, and worker concurrency is 3, so version events can be processed in any order and in parallel.
- At-least-once delivery means any step may run more than once.
- Validation failures are permanent (acknowledged); everything else must be safe to retry.
- Source documents remain in S3 version history, so deleted vectors can be rebuilt by re-ingesting.

## Options considered

| Option | Summary | Why not chosen / chosen |
| --- | --- | --- |
| A. Do nothing | Keep all versions indexed | Stale evidence in answers; unbounded growth |
| B. "Last processed wins" | After indexing version V, delete every other version | Wrong under reordering: a late event for an old version would delete the current version's vectors |
| C. Order by S3 event `sequencer` | Store the highest `sequencer` per key in the manifest; only the newest event may clean up | Correct, but needs conditional writes to handle concurrent workers, sequencer comparison rules, and extra state; more moving parts |
| D. Periodic sweep | Scheduled job compares manifest to S3 current versions | Stale window of hours; new scheduled infrastructure |
| **E. Compare with S3's current version (chosen)** | After a successful or skipped ingest, `HeadObject` gives the key's current `VersionId`; delete vectors of every other `SUCCEEDED`/`SKIPPED` version and mark them `SUPERSEDED` | Correct regardless of processing order: whichever worker finishes last consults S3, which is strongly consistent. A stale version that finishes last removes its own vectors. Cost is one `HeadObject` and one DynamoDB `Query` per ingest |

## Decision

Option E, with these rules:

1. Cleanup runs after `SUCCEEDED` **and** on the duplicate-delivery `SKIPPED` path, so a cleanup failure (which raises and triggers SQS retry) is healed by the redelivery.
2. Only records with the **exact** `source_key` are considered (a `begins_with` query alone would also match keys such as `faq.md#copy.md`).
3. Only `SUCCEEDED`/`SKIPPED` versions are cleaned; `RUNNING`, `FAILED`, `DELETED` and `SUPERSEDED` are left alone.
4. Cleanup is **skipped** when S3 has no knowable current version: object gone (404 or delete marker), unversioned bucket, or versioning suspended (`null`). Other `HeadObject` errors propagate as transient.

## Consequences

- **Positive:** retrieval only returns the current version of a document once processing settles; index and manifest stop accumulating dead versions.
- **Transient windows:** between a new version's `SUCCEEDED` and its cleanup, both versions are searchable. If an old version's late event finishes after the current one, its vectors exist briefly until it removes them.
- **Invalid current version:** if the newest version fails validation (permanent `FAILED`), cleanup does not run, so the previous version stays searchable although it is no longer current. If an older version's event is later redelivered, its own cleanup removes it and the document ends up unindexed. Both outcomes are visible in the manifest (`FAILED` on the current version).
- **Wasted work on redelivery:** a redelivered event for an already superseded version re-embeds it and then removes it again (`SUPERSEDED` is not a skip status).
- **IAM:** the worker needs `dynamodb:Query` on the manifest table. Deploy the Terraform change together with the code; without it every ingest raises after indexing and its message ends in the DLQ.
- **Not handled:** permanently deleting the *current* version makes S3 serve the previous version again, whose vectors were already superseded; the document then has no vectors until it is re-uploaded. Delete markers (`DeleteMarkerCreated`) are not subscribed, so a plain `aws s3 rm` leaves the current version's vectors in place (pre-existing behavior).
- **Existing deployed stale versions** are only cleaned the next time that same key is ingested; there is no backfill.

## Verification plan (dev, requires owner approval)

Merging to `main` deploys to `dev` automatically (CLAUDE.md §8.4), including the IAM change. After an approved deployment, use synthetic documents only:

1. Upload `incoming/Tenants/T001/adr-0001-probe.md`; expect manifest `SUCCEEDED` and its vectors present (`GetVectors`).
2. Overwrite it; expect the new version `SUCCEEDED`, the old one `SUPERSEDED`, and the old vector IDs absent.
3. Out-of-order: re-send the **old** version's S3 event body to the ingest queue; expect the old version to be re-indexed, then marked `SUPERSEDED` with its vectors removed, and the current version untouched.
4. `aws s3 rm` (delete marker): record the actual behavior (expected: no event, vectors remain).
5. Permanent delete of the current version (`delete-object --version-id`): record the actual behavior (expected: current vectors deleted, previous version unindexed).
6. Throughout: DLQ empty, no worker-error alarm, CloudWatch logs contain `Superseded ...` lines and no message content.

Record the results and the date in this ADR. Change the status to "Verified" only when steps 1–3 behave as expected.

## Follow-ups

- **Evaluation (CLAUDE.md §13.4, step 5):** add labeled cases where a document was overwritten and the expected evidence is the current version only (and the stale fact must not be cited). Requires the synthetic corpus from Milestone 1.
- **Current-version deletion:** decide whether a permanent delete of the current version should re-index the version that becomes current again.
