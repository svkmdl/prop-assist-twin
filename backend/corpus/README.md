# Synthetic document corpus

Fictional documents only (every file ends with a "synthetisches Testdokument" note). This tree
mirrors the RAG source bucket (see `docs/adr/0002-document-key-layout-and-access-scopes.md`), so a
validated tree can be uploaded as is. It lives outside `data/` on purpose: `deploy.py` copies all of
`data/` into the Lambda ZIP, and staff-only documents must not ship in a deployment package.

```text
incoming/{staff|renters}/company/{doc_type}/{doc_id}/v{n}.md
incoming/{staff|renters}/properties/{property_id}/{doc_type}/{doc_id}/v{n}.md
incoming/renters/leases/{lease_id}/{doc_type}/{doc_id}/v{n}.md
```

Each file starts with a front-matter block (`title`, `language`, `effective_from`, optional
`effective_to`). IDs must exist in `data/registry.json`.

## The scenario
XYZ Immobilienverwaltung GmbH (fictional) manages two properties: **P-001 Wohnanlage Lindenhof**
(gas heating, 24 units) and **P-002 Residenz am Park** (district heating, lift, 12 units). Three
leases: **L-0001** (P-001, tenant T001), **L-0002** (P-002, tenant T002, with a parking addendum) and
**L-0003** (P-001, co-tenants T003 and T004). `admin` is staff. 19 logical documents in 21 files:
company-wide tenant information (terms, privacy, repair policy v1/v2, heating and ventilation),
two house rules, lease contracts, utility statements, and the staff-only SOPs, contractor and ticket
policies and the two property vendor SOPs.

## Rules the documents follow (and the tests enforce)
- **A version is a complete document.** An expired version is not retrievable, so an amendment
  restates the whole contract (`lease-contract/v2` repeats every clause), never just the change.
- **Dates never depend on the calendar.** Every document is open-ended or already expired on
  2026-11-23 (the day the corpus was written); none starts or ends in the future. Evaluation results
  therefore do not change with the date. Add future-dated documents only together with an "as-of"
  parameter on the API.
- **The three precedence layers are deliberate:** lease before house rules before company guidance
  (`general-terms`). Examples: L-0001 caps small repairs at 75 EUR while the 2026 company policy says
  100 EUR for new leases; P-002's district-heating season (from 15 September) overrides the
  company-wide 1 October.
- **Numbers are unique per lease and property** so a leak is detectable by value (rents, deposits,
  caretakers, vendor names differ everywhere).
- **Old values stay out of current documents** except where a statement legitimately mentions them
  (the 2025 statement quotes the 2025 prepayment), which the outdated-version cases avoid.

## Checks
```bash
cd backend
uv run python -m common.corpus --require-documents   # key grammar, front matter, registry, version windows
uv run pytest tests/test_eval_corpus_alignment.py     # labeled cases agree with the corpus and the access rules
```
`evals/property_cases.jsonl` holds the 100 labeled cases. The alignment tests prove, offline, that every
required fact exists in the cited evidence, every cited document is visible to the asking tenant, and
every leak marker exists only outside that tenant's scope. They say nothing about answer quality:
that needs a live run against an ingested deployment.

Uploading to S3 changes live AWS state and needs the owner's explicit approval.
