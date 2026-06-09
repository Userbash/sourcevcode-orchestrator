# API Contracts

## 1. Purpose
Defines API contracts for the SourceVCode Orchestrator and related services.

## 2. Standard Error Model
```json
{
  "success": false,
  "message": "Human-readable error description",
  "code": "OPTIONAL_MACHINE_CODE",
  "details": {}
}
```

## 3. Status Code Conventions
- `200 OK`: successful read/update.
- `201 Created`: successful creation.
- `400 Bad Request`: validation failure.
- `401 Unauthorized`: missing/invalid auth context.
- `403 Forbidden`: insufficient permissions.
- `404 Not Found`: resource missing.
- `422 Unprocessable Entity`: semantic failure.
- `429 Too Many Requests`: rate limiter triggered.
- `500 Internal Server Error`: unhandled error.

## 4. Versioning Policy
- `MAJOR`: breaking API/contract changes.
- `MINOR`: backward-compatible feature additions.
- `PATCH`: backward-compatible fixes.
- Breaking changes require a versioned path (e.g., `/api/v2/*`).

## 5. Implementation Rules
1. OpenAPI spec must be updated in the same PR as contract changes.
2. Breaking changes must be flagged in `CHANGELOG.md`.

*See `docs/API/openapi.yaml` for the primary reference.*
