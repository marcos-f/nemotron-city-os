# Product Roadmap — helm

## v0.1.0 (this spec effort — SPEC ONLY)
Scoped federation slice: feature-area://helm + repo-less feature-area://signet
(carried here), UC-007/UC-008/UC-009, scenarios for shell tabs, OpenAPI
surface, composed state, ledger tail, agent-drives, agent-refusal, role
check, OIDC login, subject-in-record, provider policy. Wireframes (login,
helm shell, composed, approval detail — the four feed-tab pages live in
their own component repos), architecture slice, contracts, gate reports.
No code.

## v0.2.0 (build, per BUILD-GOAL-helm.md brief — B3+B5)
FastAPI console per wireframes; OpenAPI /docs; GitHub OAuth minimal (signet:
subject into approval records; Google designed-only; NVIDIA OIDC only
behind recorded live verification); MCP host mirroring the API; role model
signet(human)-vs-agent enforced at the approve path.

## v0.3.0 (post-event)
Google OIDC live; NVIDIA OIDC if live-verified; key rotation for any
transcript-exposed credentials (roadmap-level, carried from orchestrator
tech-review).
