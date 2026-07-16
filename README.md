# ApexTech
Workflow technician

## Scout — Phase 1: Data Logging Foundation

Scout is a personal business agent built on the existing ApexFlow
infrastructure (Flask, SQLite, Docker, Caddy, Hetzner), targeting the
`clients/apexdigitalpicks` deployment. Phase 1 only logs data — no pattern
detection, alerts, or voice yet. Full spec: see the Phase 1 build spec doc.

| File | Purpose |
|---|---|
| `scout_persona.yaml` / `scout_identity.py` | Scout's name, tone, and no-branding rules — a config file, not hardcoded in route logic. |
| `scout_migrate.py` | Schema migration. Dry-run by default (`python scout_migrate.py <db>`), apply with `--apply`. Extends the existing `contacts` table (email_address, business_name, last_activity_at, merged_into) instead of creating a colliding one, and adds `activity_log` + `notes`. |
| `scout_contacts.py` | Shared, NOT-NULL-safe contact upsert used by both ingestion paths. |
| `scout_whatsapp_hook.py` | `log_whatsapp_activity(...)` — call from inside the existing WhatsApp webhook handler per message. Not wired in automatically; see its INTEGRATION docstring. |
| `scout_gmail_ingest.py` | Gmail OAuth ingestion: one-time `--authorize`, chunked-by-month `--backfill`, `poll_recent()` / Pub/Sub push (`gmail_push_bp`) for ongoing sync. See its module docstring for setup steps. |
| `scout_verify.py` | `/scout/api/verify/summary` and `/scout/api/verify/recent` — minimal count/query views for the manual review period, same access-key pattern as the dashboard. |
| `requirements-scout.txt` | Added Python deps (PyYAML, Gmail API client libs). |
| `deploy_scout.sh` | Deploy script for the schema + WhatsApp hook + verify view, mirroring `deploy.sh`'s safe, re-runnable, dry-run-then-confirm pattern. Gmail ingestion is deployed separately once OAuth is set up locally. |

Rollout order per the spec: (1) schema + WhatsApp hook, verified against
live data; (2) Gmail ingestion, tested on a small date range first; (3) a
week of manual review of `activity_log` / `contacts`; (4) only then, Phase 2.
