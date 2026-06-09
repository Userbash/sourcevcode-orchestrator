# Security & RBAC

## 1. RBAC Matrix

| Role | Intent | Typical Scope |
| --- | --- | --- |
| `root` | Full platform control | All resources |
| `platform_admin` | Operational administration | Admin APIs and platform controls |
| `security_admin` | Security governance | Audit, access, policy review |
| `content_admin` | Content moderation | Publications and content workflow |
| `support` | User support operations | User assistance with limited mutations |

### Guardrail Principles
- Least privilege by default.
- System roles are protected from casual mutation.
- Sensitive role changes require explicit audit context.
- Admin endpoints require both role gate and permission gate.

## 2. Security Changelog

- **2026-05-24:** Hardened backend middleware, administrative access controls, RBAC enforcement, audit/telemetry integration, secret scanning workflow.
- **2026-05-23:** Consolidated admin safety controls, AI Bridge routing controls, and risk-aware model selection.
- **2026-05-22:** Stabilized admin telemetry/rate-limiting, extended system-wide audit coverage, and admin boundaries.
- **2026-05-21:** Strengthened registration security, email domain policies, CORS handling, baseline auth/session hardening.
