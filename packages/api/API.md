# Lumin API — curl reference

Operator-facing reference for the most common API surfaces. Auto-generated machine-readable spec lives at [`openapi.json`](openapi.json) — run `python scripts/export_openapi.py --out openapi.json` (from `packages/api/`) to regenerate, or load the spec into Swagger UI / Postman / OpenAPI Generator.

The full set of endpoints is in `openapi.json`; this doc covers the surfaces operators hit most.

## Auth

When `LUMIN_API_TOKEN` is set on the server (Slice 5B), every request below requires:

```bash
-H "Authorization: Bearer ${LUMIN_API_TOKEN}"
```

When unset, drop the header — the API is open. `/health`, `/openapi.json`, `/docs` and `/redoc` are always reachable.

For brevity, examples below assume `LUMIN_API_TOKEN` is unset. Add the header on a production instance.

---

## Health

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Per-component status:

```bash
curl http://localhost:8000/v1/admin/health | jq
```

---

## Ingest a trace (Python SDK does this for you)

```bash
curl -X POST http://localhost:8000/v1/traces \
  -H "Content-Type: application/json" \
  -d '{
    "id": "demo-trace-1",
    "name": "demo_agent",
    "input": "What is the capital of France?",
    "output": "Paris.",
    "started_at": "2026-05-08T12:00:00Z",
    "ended_at":   "2026-05-08T12:00:01Z",
    "session_id": "sess-1",
    "user_id": "alice"
  }'
```

Spans are similar — POST to `/v1/spans` with a list under the `spans:` key. See `openapi.json` for the full schema.

---

## List traces

```bash
# 100 most recent
curl http://localhost:8000/v1/traces

# Multi-tenant — only the prod project (Slice 6B)
curl "http://localhost:8000/v1/traces?project=prod"

# Pagination
curl "http://localhost:8000/v1/traces?limit=20&offset=40"
```

---

## Firewall — decide synchronously

The hot path. Agent integrations call this on every lifecycle hook:

```bash
curl -X POST http://localhost:8000/v1/policy/decide \
  -H "Content-Type: application/json" \
  -d '{
    "lifecycle": "before_tool_call",
    "tool_name": "shell",
    "params": { "command": "rm -rf /tmp/cache/*" },
    "session_id": "sess-1",
    "agent": "demo_agent"
  }'
```

Response:

```json
{
  "decision": "block",
  "policy_name": "owasp_destructive_shell",
  "decision_id": "dec_a8c1...",
  "reason": "is_shell_tool(tool_name) and regex_match(...)",
  "mode_at_decision": "enforce",
  "duration_ms": 3
}
```

Per Rule 7 this endpoint **never returns 5xx** — internal errors yield a permissive `{"decision": "allow", "reason": "internal_error"}`.

---

## Firewall — list decisions

```bash
# Recent
curl "http://localhost:8000/v1/decisions?limit=20"

# Filtered
curl "http://localhost:8000/v1/decisions?project=prod&decision=block&since=2026-05-01T00:00:00"
```

---

## Firewall — list / import starter packs (Slice 4 §13)

```bash
# Browse available packs
curl http://localhost:8000/v1/firewall/library | jq '.packs[].pack_id'

# Preview a pack before importing
curl http://localhost:8000/v1/firewall/library/compliance_gdpr | jq '.policies[].name'

# Install
curl -X POST http://localhost:8000/v1/firewall/library/compliance_gdpr/import
# {"pack_id":"compliance_gdpr","imported":6,"skipped_duplicates":0,"failed":0,...}
```

---

## Firewall — webhooks (Slice 4 §9.10)

```bash
# Add a Slack alerting webhook
curl -X POST http://localhost:8000/v1/firewall/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ops-alerts",
    "kind": "slack",
    "config": { "webhook_url": "https://hooks.slack.com/services/..." },
    "severity_min": "high"
  }'

# List (secrets masked)
curl http://localhost:8000/v1/firewall/webhooks | jq

# Delivery DLQ — what failed and why
curl http://localhost:8000/v1/firewall/webhooks/failures | jq

# Generic + HMAC for an internal SIEM
curl -X POST http://localhost:8000/v1/firewall/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "siem",
    "kind": "generic",
    "config": {
      "url": "https://siem.internal/lumin",
      "hmac_secret": "<long random string>"
    }
  }'
```

Receivers verify the `X-Lumin-Signature: sha256=...` header against the request body.

---

## Policies — CRUD + version history (Slice 6C)

```bash
# List active policies
curl http://localhost:8000/v1/policies | jq '.policies[].name'

# Get one
curl http://localhost:8000/v1/policies/owasp_destructive_shell | jq

# Promote shadow → enforce (returns FP forecast)
curl -X POST http://localhost:8000/v1/policies/owasp_destructive_shell/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "enforce"}'

# Version history
curl http://localhost:8000/v1/policies/owasp_destructive_shell/versions | jq

# Roll back to v3
curl -X POST http://localhost:8000/v1/policies/owasp_destructive_shell/rollback \
  -H "Content-Type: application/json" \
  -H "X-Lumin-Actor: alice@example.com" \
  -d '{"version_number": 3}'
```

---

## Cross-session vault (Slice 6A)

```bash
# Inspect what's been recorded
curl "http://localhost:8000/v1/firewall/vault?user_id=alice" | jq

# Aggregate
curl http://localhost:8000/v1/firewall/vault/stats | jq

# GDPR Art. 17 — erase a single fact
curl -X DELETE http://localhost:8000/v1/firewall/vault/<entry_id>
```

The leak detector uses this table at every `after_proxy_call` decision. Empty user-id means a fact was recorded but isn't tied to a known user.

---

## Admin — backup / restore (Slice 5C)

```bash
# Snapshot (default name = snap_<UTC ts>)
curl -X POST http://localhost:8000/v1/admin/backups -d '{}'
# {"name":"snap_20260508T120000Z","path":"/data/backups/snap_...","size_bytes":...}

# List
curl http://localhost:8000/v1/admin/backups | jq

# Restore — destructive, requires confirm
curl -X POST http://localhost:8000/v1/admin/backups/snap_20260508T120000Z/restore \
  -H "Content-Type: application/json" \
  -d '{"confirm": true}'

# Delete a snapshot
curl -X DELETE http://localhost:8000/v1/admin/backups/<name>
```

---

## Panic kill switch

If a policy is misfiring and you can't immediately diagnose:

```bash
# Flip every policy in the project to mode=shadow with one call
curl -X POST http://localhost:8000/v1/firewall/panic_disable \
  -H "Content-Type: application/json" \
  -d '{"disabled": true, "reason": "false-positive on customer_pii_v2"}'

# Reverse within an hour
curl -X POST http://localhost:8000/v1/firewall/panic_disable/undo
```

---

## Approvals (`require_approval` decisions)

```bash
# Inbox
curl http://localhost:8000/v1/approvals?state=pending | jq

# Resolve
curl -X POST http://localhost:8000/v1/approvals/apv_a8c1.../resolve \
  -H "Content-Type: application/json" \
  -d '{"resolution": "allow", "reason": "verified with the customer"}'
```

---

## Generating typed clients

The OpenAPI spec at [`openapi.json`](openapi.json) is committed and CI-verified to match the live API.

```bash
# TypeScript
npx openapi-typescript packages/api/openapi.json -o my-app/src/lumin-api.ts

# Python (httpx)
pipx run openapi-python-client generate --path packages/api/openapi.json
```

Both keep in lockstep with Lumin via the export script.
