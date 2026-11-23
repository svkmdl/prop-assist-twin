# Use case: property-manager maintenance and policy inquiry

Status: **draft for owner review**, 2026-11-23. Scope of Milestone 1 (CLAUDE.md section 13.4). The figures under "Acceptance criteria" are *proposed* and are not results: no baseline has been measured yet.

## Problem
Tenants of XYZ Immobilienverwaltung GmbH ask the same questions again and again: what does my lease say, which rule applies in my building, who do I call, how fast will a repair happen. Each one costs a property manager a call or an e-mail, and a wrong answer (an outdated rule, another tenant's data, a promise nobody is entitled to make) is worse than a slow one. Today the manual route is the service hotline (Mon-Fri 08:00-17:00) and the 24/7 emergency line.

## Actors and the data each may see
| Actor | Sees | Cannot see |
| --- | --- | --- |
| **Tenant** (lease participant; co-tenants share a lease) | company-wide tenant information, the house rules of their property, the documents of their own lease (contract, utility statement, addenda), all in force today | other leases, other properties' rules, any staff document |
| **Property manager** (staff) | everything in force | nothing is hidden from staff in this milestone |
| **Assistant** (Marie Stein) | exactly the scope of the asking principal, resolved server-side | it never decides access, never creates a ticket, never makes a commitment |

Scope comes from the server (registry: tenant -> leases -> property), never from the question or the model (CLAUDE.md section 9). Until verified identity exists (Month 3) the principal in the request is unverified; the public demo is pinned to one synthetic tenant.

## What a good answer looks like
1. **Factual question** ("What is my deposit?", "When do quiet hours start?"): the correct value for *this* tenant's lease or property, from the version in force, with a `[S#]` citation.
2. **Precedence**: lease before house rules before company guidance. A tenant whose lease sets a small-repairs limit of 75 EUR is told 75 EUR, not the newer company default.
3. **Triage** ("Water is dripping from my ceiling"): classify as emergency, give the emergency number and the first safe steps, state the response promise. Drafting a ticket for a human to confirm is the target of Month 4; today the assistant only advises.
4. **Cannot be answered from the documents**: say so (the persona rule "cannot verify") instead of guessing.
5. **Not the assistant's decision** (rent reduction, deferral of a deposit, "create the ticket now"): route to a caseworker, never confirm.
6. **Out of scope or hostile** (another tenant's data, staff documents, "ignore your instructions", a claimed staff role): refuse without revealing whether the data exists.

## Out of scope for this milestone
Creating tickets or any external write; a structured property/tenant/ticket database; real identity; real customer data (everything here is synthetic).

## Acceptance criteria (proposed, to be confirmed after the first baseline)
Measured on `backend/evals/property_cases.jsonl` (100 labeled cases, 17 categories) against an ingested deployment, with denominators reported per category:

| Area | Measure | Proposed gate |
| --- | --- | --- |
| Access | cases in `privacy`, `isolation-*`, `injection` where a forbidden marker appears | **0** (hard gate, deterministic) |
| Staleness | `outdated-version` answers that present an expired value as current | 0 |
| Commitments | `escalation` answers that confirm a reduction, deferral or ticket | 0 |
| Retrieval | the cited evidence documents appear among the returned sources (Recall@3) | set after baseline (needs source document IDs in the API, ADR 0002 slice 6) |
| Grounding | answerable cases with a correct critical fact and an inline citation | set after baseline |
| Refusal | `unanswerable` cases that say so instead of inventing | set after baseline |
| Operations | p50/p95 latency, error rate, tokens and cost per answer | measure first |

Targets such as "Recall@3 >= 90%" are aspirations until a baseline exists (CLAUDE.md section 13.7); do not quote them as achieved.

## Artifacts
- Corpus: `backend/corpus/` (19 logical documents, 21 files; see its README). Validate: `cd backend && uv run python -m common.corpus --require-documents`.
- Labeled cases: `backend/evals/property_cases.jsonl`; consistency with the corpus is enforced offline by `backend/tests/test_eval_corpus_alignment.py`.
- Live run (owner-approved, needs an ingested deployment and, to act as different tenants, `EVAL_API_KEY`): `cd backend && API_URL=... EVAL_API_KEY=... EVAL_CASES=evals/property_cases.jsonl EVAL_RESULTS=run.json uv run python eval_chat.py`.
