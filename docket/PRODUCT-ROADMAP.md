# Product Roadmap — docket

## v0.1.0 (this spec effort — SPEC ONLY)
Scoped federation spec slice: feature-area://docket, use-case://docket/triage-quoted,
3 scenarios (judgment-quote, citation-check, abstention, route-ledger),
wireframe (docket.html + index.html), architecture slice, contracts (already
seeded), gate reports (phase-01/02/03 PASS), traceability to the orchestrator
spec. No runtime code.

## v0.2.0 (build goal — armed by BUILD-GOAL-docket.md)
Socrata `76t5-zqzr` snapshot ingest (offline); ~~NeMo Retriever embeddings
(`nvidia/llama-nemotron-embed-1b-v2`, dim-2048)~~ **— declared and
contract-published, NOT DELIVERED in v0.2.0; retrieval is a linear scan and
the index is registered `declared-unavailable`. Carried to v0.3.0 or
retired, but not claimed as shipped**; judgment via
`nvidia/nemotron-3-super-120b-a12b`; verbatim-quote validator (string-match
against source; reject uncited); abstention path; route effect (reversible)
via throughline. Acceptance: test://docket/quote-verbatim,
test://docket/uncited-rejected, test://docket/abstain-thin-evidence,
test://docket/route-ledgered.

## v0.3.0 (post-event, orchestrator DEMO_PLAN §7-§8)
SMC Title 23 zoning grounding (source behind an F5 bot challenge, verified
2026-08-15) — stated extension, not committed demo scope.
