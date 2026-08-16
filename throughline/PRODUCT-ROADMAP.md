# Product Roadmap — throughline

## v0.1.0 (this spec effort — SPEC ONLY)
Scoped federation slice: 1 feature area, 2 UCs (verify-chain,
refuse-config), 7 scenarios, one composed wireframe page, architecture
slice, 3 gate reports, contracts already seeded. No code.

## v0.2.0 (build goal — armed by brief)
throughline builds FIRST (per orchestrator sequencing: throughline before
docket/breaker, before helm, before siren, before blindspot). Scope: signal
envelope; append-only JSONL ledger `{seq, prev_hash, sha256}`;
reversibility-typed effect registry; gate derived from class; durable
approval queue (blocks indefinitely, no timeout path); config loader +
refusal; OpenAPI per `contracts/openapi.yaml`; NeMo Relay 0.7.3 gate mirror
where effects route through Python tool calls. Brief:
`.viper-context/plan/web/v0.1.0/briefs/BUILD-GOAL-throughline.md`.

## v0.3.0 (post-event, from DEMO_PLAN §7-§8 + spec 35 §16, carried)
Local Nemotron NIM path if hardware materializes; key rotation for the
transcript-exposed NVIDIA_API_KEY; any throughline-side changes needed to
support Google/NVIDIA OIDC identity work landing in signet/helm.
