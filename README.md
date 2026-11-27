# AnnSeva — Smart Procurement (Corrected v4)

## Run

```bash
pip install -r backend_requirements.txt
python server.py
```

Open **http://127.0.0.1:8000**. Do not open `index.html` directly — it's served by
FastAPI (which also serves `app.js`) so that API requests, cookies, and the
`file://` origin all line up correctly.

### Tests

```bash
pip install -r dev_requirements.txt
pytest -q
```

Tests run against a throwaway SQLite database (via `ANNSEVA_DB_PATH`) and never
touch `annseva.db`.

### Demo credentials

- Farmer: `FMR-1042` / `farmer123`
- Admin IDs: `ADM-KURNOOL`, `ADM-NANDYAL`, `ADM-ADONI` / password: `admin123`

For deployment, set `ANNSEVA_ADMIN_PASSWORD` and `ANNSEVA_FARMER_PASSWORD` to strong values. You can also set `ANNSEVA_API_KEY`, `ANNSEVA_ALLOWED_ORIGINS`, `ANNSEVA_TIMEZONE`, and `ANNSEVA_SESSION_TTL_HOURS` (default: 24).

## Corrected workflow

1. Farmer signs in and registers a standing crop offer once. Quantity, location and GPS coordinates are persisted.
2. Admin signs in and creates a requirement. Existing matching standing offers are automatically converted into requirement-specific applications. Multiple requirements can remain active; the newest active requirement is the dashboard's current requirement.
3. The system checks active authorized admins, excludes admins whose jurisdiction matches the farmer's village/town, and randomly assigns one eligible non-local admin. The assignment is stored with an audit record and remains fixed for that application.
4. Admin runs matching. Verified farmers are ranked using a transparent cold-start baseline until at least 20 real completion outcomes with more than one outcome value exist. After that, a persisted Random Forest is trained and cached.
5. Only the assigned non-local admin can approve an application. The approving admin and timestamp are recorded. Approval runs inside an immediate SQLite transaction, checks live requirement/capacity state, consumes the approved offer quantity, and assigns a token plus a 30-minute slot.
6. Scheduling now enforces **per-slot quantity capacity**, not merely one farmer per slot.
7. Admin marks a scheduled procurement as completed and enters the actual received quantity. Only then is the record added to ML training history, avoiding training on approval decisions themselves.
8. Farmer accounts can only read their own application and notification data. Admin endpoints require admin authorization.

## Fixes included

- Real authentication and role authorization instead of a frontend-only Admin/Farmer switch.
- Login sessions are persisted in SQLite, so restarting `server.py` no longer logs every browser session out; stale/expired browser tokens are automatically cleared and the sign-in dialog is shown.
- Reset endpoint is admin-only.
- Farmer API requests cannot edit another farmer's record.
- Farmer state no longer exposes every farmer/application in the database.
- API key support remains available for trusted admin integrations.
- GPS is stored consistently in both `farmers` and `crop_offers`.
- Location fallback uses normalized token overlap rather than first-word matching.
- Haversine distance is used when both locations have coordinates.
- Recommendation responses identify whether the score came from `random_forest` or `cold_start_baseline`.
- Random Forest model is persisted and retrained only when completion-history data changes.
- Recommendation outcomes are recorded at actual completion, not at approval.
- Approval is concurrency-safe with `BEGIN IMMEDIATE` and a guarded update.
- Scheduling is timezone-aware (`Asia/Kolkata` by default).
- Scheduling enforces centre slot capacity and prevents overbooking.
- Stale recommendation lists are refreshed/recomputed after approval.
- A completion workflow is included so the ML system can learn from actual procurement results.
- Multiple active requirements are retained historically rather than automatically deleting/deactivating earlier requirements.
- Existing v2/v3 SQLite databases receive schema migrations on startup.
- `app.js` is now the single copy of the frontend logic. Previously `index.html`
  inlined its own separate `<script>` (silently more up to date than the checked-in
  `app.js`, which was never actually loaded — FastAPI had no route to serve it).
  `index.html` now loads `app.js` via `<script src="app.js">`, and `server.py`
  serves it.
- `ml/requirements.txt` was a duplicate of `backend_requirements.txt` and has been
  removed; `dev_requirements.txt` adds test-only dependencies on top of it.
- Added a `pytest` suite (`tests/test_api.py`) covering login, role isolation,
  non-local admin assignment/authorization, the approve→schedule→complete
  lifecycle, double-approval rejection, and requirement-to-standing-offer matching.
- `DB_PATH` is now overridable via `ANNSEVA_DB_PATH` so tests (and any future
  staging setup) don't touch the real database.
- `annseva.db`, `__pycache__/`, and the cached model files are no longer shipped
  in the repo (see `.gitignore`) — the server recreates the database with seed
  data on first run, and the model is recreated once 20+ real completions exist.

## Production notes

The default passwords are only for the local demo. A real deployment should use government/SSO authentication, PostgreSQL, HTTPS, proper secret management, audit logs, CSRF protection where applicable, and a real identity/registry integration.

## Simple chatbot

The UI includes a lightweight AnnSeva Assistant. It is a rule-based chatbot (no external AI API required) that uses the authenticated user's permitted data to answer common questions about eligibility, application status, scheduling, and the active procurement requirement. It is intended as a simple demo feature and can later be replaced with an LLM/RAG service.


## Non-local admin assignment

Each active admin has a jurisdiction. When an application is created/matched, AnnSeva:
1. Finds active authorized admins.
2. Excludes admins whose jurisdiction overlaps the farmer's village/town/location.
3. Randomly selects one remaining admin.
4. Stores the assigned admin on the application and in `admin_assignment_history`.
5. Allows only that assigned admin to approve the application.
6. Records `reviewed_by_admin_id` and `reviewed_at` when approval happens.

For the demo, all three admin accounts use the same demo password but have different admin IDs. In production, each officer should have their own secure credentials/SSO identity.
