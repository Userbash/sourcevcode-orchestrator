# Governance, Process, and ADRs

## 1. Documentation Governance
- Standardized docs structure: `SYSTEM_OVERVIEW`, `API_CONTRACTS`, `SECURITY`, `GOVERNANCE`, `OPERATIONS`.
- Release and semantic versioning policy enforced.
- Changelog discipline mandatory for all changes.
- CI checks for local markdown links and API route-doc coverage.

## 2. Versioning Policy
The repository follows `MAJOR.MINOR.PATCH` semantic versioning.
- `MAJOR`: breaking API/contract changes.
- `MINOR`: backward-compatible feature additions.
- `PATCH`: backward-compatible fixes/hardening.

Breaking changes require a versioned path or compatibility layer and migration notes in release manifest.

## 3. Traceability Policy
Every material change must be traceable through:
`Issue -> Pull Request -> Commit(s) -> Changelog -> Runbook/Docs Update`

## 4. Environment Versioning
1. Pin dependencies via lockfiles.
2. Use explicit container image tags.
3. For every release, capture compose file revision, image tags, env snapshot, and migration list.
4. Keep environment drift visible.

## 5. Architecture Decision Records (ADR) Index

| ID | Title | Status |
|---|---|---|
| 0001 | Documentation Governance & Versioning | Accepted |
| 0002 | AI Bridge Runtime Hardening Roadmap | Proposed |
| 0003 | AI Bridge Session Memory | Proposed |

*See `docs/ADR/` for detailed ADR templates and history.*

## 6. DB Migration Playbook
1. One logical change per migration file.
2. Incremental numbering.
3. Include purpose, impact, rollback strategy.
4. CI dry-run validation required.
