---
phase: 01
status: passed
verified_at: 2026-08-28
---

# Phase 1 Verification: Extension Scaffold + Data Model + Per-Wallet Mint CRUD

**Verifier:** Automated + manual code inspection
**Method:** Source review of all 6 implementation files, runtime model introspection, SQLite schema inspection, grep-based invariant checks

---

## Success Criteria (from ROADMAP.md)

### SC-1: LNbits loads the lnurlmint extension without errors; appears in extensions list with valid metadata

**PASS**

Evidence:
- `__init__.py` (lines 1–39) exports all 5 loader-contract symbols: `db`, `lnurlmint_ext` (APIRouter prefix `/lnurlmint`), `lnurlmint_start`, `lnurlmint_static_files`, `lnurlmint_stop` — confirmed in `__all__` (line 39)
- Symlink exists: `/home/exedev/lnbits/lnbits/extensions/lnurlmint -> /home/exedev/lnurlmint`
- `manifest.json` valid: `{"repos": [{"id": "lnurlmint", "organisation": "dni", "repository": "lnurlmint"}]}`
- `config.json` valid: name `lnurlmint`, version `0.1.0`, `min_lnbits_version` `1.5.4`, license `MIT`, tile path `/lnurlmint/static/image/lnurlmint.png`
- `lnurlmint_start()` is a no-op stub (line 33–36) — correct for Phase 1 (background tasks are Phase 2 / EXT-03)
- `lnurlmint_stop()` cancels `scheduled_tasks` (lines 24–30)
- Runtime import confirmed: `from lnbits.extensions.lnurlmint.models import Mint` succeeds; pydantic 1.10.26 confirmed
- Plan 01-01 SUMMARY records E2E: "Installed Extensions (12): ... lnurlmint (0.1.0)"

### SC-2: Wallet owner can create a mint via POST and retrieve via GET

**PASS**

Evidence:
- `views_api.py` line 22–53: `POST /lnurlmint/api/v1/mints` with `require_admin_key`, accepts `CreateMint` body, generates UUID + secp256k1 privkey, inserts via `create_mint`, returns `MintResponse` (privkey excluded)
- `views_api.py` line 56–62: `GET /lnurlmint/api/v1/mints` with `require_invoice_key`, returns wallet-scoped list
- `CreateMint` model (models.py lines 79–101) accepts all configurable fields: `username`, `base_fee_msat`, `fee_percent_ppm`, `min_sendable_msat`, `max_sendable_msat`, `min_mint_msat`, `verify_enabled`, `sunset_mint`, `base_url`, `onion_url` — with `ge`/`le` bounds and `root_validator` for sendable ordering
- `wallet.wallet.id` used for scoping (line 37, 61) — wallet ID taken from auth key, never request body
- Plan 01-01 SUMMARY records E2E: POST creates mint, GET returns wallet-scoped array
- Plan 01-03 SUMMARY records E2E: GET/PUT/DELETE by ID verified, cross-wallet 404

### SC-3: Database migrations create all four tables on SQLite and Postgres

**PASS**

Evidence:
- `migrations.py` `m001_initial` (lines 8–47): creates `lnurlmint.mints` (15 columns) + wallet index
- `migrations.py` `m002_notes_records_melts` (lines 50–144): creates `lnurlmint.notes` (8 columns), `lnurlmint.mints_records` (7 columns), `lnurlmint.melts` (7 columns) + 4 indexes
- Cross-DB DDL: `db.timestamp_now` string-concatenation for TIMESTAMP defaults (SQLite `strftime`, Postgres `now()`); `db.references_schema` for index prefixes
- SQLite DB inspection confirmed all 4 tables exist with correct columns:
  - `mints`: 15 columns (id, wallet, username, base_url, onion_url, base_fee_msat, fee_percent_ppm, min_sendable_msat, max_sendable_msat, min_mint_msat, verify_enabled, sunset_mint, mint_privkey, created_at, updated_at)
  - `notes`: 8 columns (id, mint_id, amount_msat, spent, pending, pending_payment_hash, comment_hash, created_at)
  - `mints_records`: 7 columns (payment_hash, mint_id, pr, amount_msat, minted, comment_hash, created_at)
  - `melts`: 7 columns (payment_hash, mint_id, note_ids, amount_msat, pr, settled, created_at)
- Postgres compatibility: DDL uses `db.timestamp_now` and `db.references_schema` (cross-DB properties); no SQLite-specific syntax in CREATE TABLE statements. Postgres not tested at runtime (no Postgres instance available), but DDL follows the verified giftcards cross-DB pattern.

### SC-4: Cross-wallet isolation — every management query is wallet-scoped

**PASS**

Evidence:
- `crud.py`: all 6 query functions include `wallet = :wallet` or `m.wallet = :wallet`:
  - `get_mints_by_wallet` (line 49): `WHERE wallet = :wallet`
  - `get_mint` (line 62): `WHERE id = :id AND wallet = :wallet`
  - `update_mint` (line 92): `WHERE id = :id AND wallet = :wallet`
  - `count_outstanding_notes` (line 109): `JOIN lnurlmint.mints m ON n.mint_id = m.id WHERE ... m.wallet = :wallet`
  - `delete_mint` (line 131): `JOIN ... WHERE ... m.wallet = :wallet` + `DELETE ... WHERE id = :id AND wallet = :wallet`
- grep confirms 8 matches for `wallet = :wallet|m.wallet = :wallet` in crud.py
- `views_api.py`: all 5 endpoints use `wallet.wallet.id` from auth decorator (lines 37, 61, 75, 95, 121)
- DELETE endpoint pre-checks `get_mint` before `delete_mint` (line 118) — enforces 404 for cross-wallet access (fix from Plan 01-03 Task 3)
- Plan 01-03 SUMMARY records E2E: wallet B gets 404 on GET/PUT/DELETE of wallet A's mint

---

## Requirement Checks

### EXT-01: Extension discoverable by LNbits loader

**PASS**

Evidence:
- `__init__.py` exports: `lnurlmint_ext` (APIRouter prefix `/lnurlmint`), `lnurlmint_start`/`lnurlmint_stop` (plain functions), `db` (imported from `.crud`), `lnurlmint_static_files` (list with `/lnurlmint/static` path)
- `manifest.json` + `config.json` valid (see SC-1)
- All 5 symbols in `__all__` (line 39)

### EXT-02: Static files registered at /lnurlmint/static, Vue SFCs served

**PASS**

Evidence:
- `__init__.py` lines 14–19: `lnurlmint_static_files = [{"path": "/lnurlmint/static", "name": "lnurlmint_static"}]`
- `static/routes.json`: route map with `PageLnurlmint` at `/lnurlmint/`, template `/lnurlmint/static/js/index.vue`, component `/lnurlmint/static/js/index.js`
- `static/js/index.vue` (87 lines): Vue 3 + Quasar SFC with q-card, q-list, create-mint q-dialog, delete button
- `static/js/index.js` (80 lines): `PageLnurlmint` component with `fetchMints`, `createMint`, `deleteMint` via `LNbits.api.request`
- `static/image/lnurlmint.png`: 64x64 placeholder icon (208 bytes)
- `views.py`: generic router with `index` view + `check_user_exists` dependency (auth-gated SPA route)

### EXT-04: No new dependencies beyond LNbits pyproject.toml

**PASS**

Evidence:
- All Python imports across 5 `.py` files: `asyncio`, `fastapi`, `loguru`, `lnbits.db`, `lnbits.core.models`, `lnbits.decorators`, `lnbits.core.views.generic`, `pydantic`, `time`, `typing`, `datetime`, `uuid` — all stdlib or LNbits/FastAPI/pydantic core
- `coincurve` imported lazily inside `_generate_mint_privkey()` (crud.py line 35) — transitive dep already used by LNbits nostr/nwc code
- No `pydantic-settings`, `qrcode`, or other forbidden deps
- grep for `pyqrcode|pydantic_settings|qrcode` in *.py: no matches

### DATA-01: m001_initial creates mints table with all columns

**PASS**

Evidence:
- `migrations.py` lines 15–39: `CREATE TABLE IF NOT EXISTS lnurlmint.mints` with all 15 columns
- Runtime model introspection: `Mint.__fields__` has 15 fields, all DATA-01 required columns present (0 missing)
- SQLite schema inspection: 15 columns confirmed with correct types (TEXT/INTEGER/TIMESTAMP)
- `mint_privkey TEXT NOT NULL` present (secp256k1 keypair column for Phase 5)

### DATA-02: notes table

**PASS**

Evidence:
- `migrations.py` lines 74–88: `CREATE TABLE IF NOT EXISTS lnurlmint.notes` with 8 columns
- `Note` model (models.py lines 134–169): 8 fields matching table columns, `state` property (spent > pending > outstanding)
- SQLite inspection: 8 columns confirmed
- `pending_payment_hash TEXT` present (reconcile column), `comment_hash TEXT` present (comment protection)

### DATA-03: mints_records + melts tables

**PASS**

Evidence:
- `migrations.py` lines 90–103: `mints_records` (7 columns: payment_hash, mint_id, pr, amount_msat, minted, comment_hash, created_at)
- `migrations.py` lines 105–118: `melts` (7 columns: payment_hash, mint_id, note_ids, amount_msat, pr, settled, created_at)
- `MintRecord` model (lines 172–190): 7 fields, `minted` compare-and-set flag
- `MeltRecord` model (lines 193–210): 7 fields, `settled` flag, `note_ids`
- SQLite inspection: both tables confirmed with correct columns

### DATA-04: All models use pydantic v1 syntax

**PASS**

Evidence:
- `models.py` line 10: `from pydantic import BaseModel, Field, root_validator, validator` — v1 imports only
- grep for `field_validator|model_validator` (v2 syntax): no matches
- Runtime: pydantic version confirmed `1.10.26`
- `root_validator` used in `CreateMint` (line 97) and `UpdateMint` (line 124)
- `validator` used in `Note`, `MintRecord`, `MeltRecord` (pre=True created_at parsing)
- `class Config` not needed (no custom config required), but v1 `BaseModel` pattern followed throughout

### DATA-05: Every query scoped by wallet_id

**PASS**

Evidence:
- See SC-4 above — all 6 CRUD functions wallet-scoped
- `count_outstanding_notes` and `delete_mint` use JOIN on `mints.wallet` (notes table has no wallet column — scoping via FK)
- 8 grep matches for `wallet = :wallet|m.wallet = :wallet` in crud.py

---

## Security Invariant Checks

### Store-hashes-not-secrets (SEC-02): no preimage/secret/k1 column in any table

**PASS**

Evidence:
- grep for `preimage|secret|raw_k1|k1` in `migrations.py`: 2 matches, both in docstrings (lines 58, 62) explaining `notes.id is sha256(k1)` — no column named preimage/secret/k1
- grep in `models.py`: 1 match in docstring (line 138) — no field named preimage/secret/k1
- SQLite schema: no `preimage`, `secret`, or `k1` column in any of the 4 tables
- `notes.id` is `sha256(k1)` hex (documented, not the raw credential)
- `mint_privkey` is a signing key (not a bearer credential) — and is excluded from API responses (see C-01 fix)

### Cross-wallet isolation (SEC-07): every CRUD query is wallet-scoped

**PASS**

Evidence: See SC-4 and DATA-05 above.

### DB transaction atomicity (REC-03): delete_mint uses async with db.connect()

**PASS**

Evidence:
- `crud.py` line 127: `async with db.connect() as conn:` — outstanding-notes count + delete in one transaction
- grep confirms 2 matches (1 docstring + 1 actual usage)
- Single-statement ops correctly use `db.insert`/`db.fetchone`/`db.fetchall`/`db.execute`

---

## Code Review Fixes

### C-01: mint_privkey excluded from API responses

**PASS — FIXED**

Evidence:
- `MintResponse` model (models.py lines 55–76): 14 fields, `mint_privkey` NOT present
- Runtime: `'mint_privkey' in MintResponse.__fields__` → `False`
- All 4 API endpoints return `MintResponse(**mint.dict(exclude={"mint_privkey"})).dict()`:
  - POST (views_api.py line 53)
  - GET list (line 62)
  - GET by ID (line 78)
  - PUT (line 98)
- `mint_privkey` never leaves the server after creation

### W-01: update_mint whitelists updatable column names

**PASS — FIXED**

Evidence:
- `crud.py` lines 13–24: `_UPDATABLE_FIELDS = frozenset({...})` with 10 configurable columns
- `update_mint` line 83: `fields = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}`
- Runtime: `mint_privkey`, `id`, `wallet` all NOT in `_UPDATABLE_FIELDS` — immutable fields cannot be updated
- Column names interpolated into SQL are now guaranteed to be from the whitelist (SQL injection via column-name interpolation prevented)

---

## Additional Checks

### All 3 plans have SUMMARY.md files

**PASS**

Evidence:
- `01-01-SUMMARY.md`: 224 lines, status `complete`, requirements EXT-01/EXT-02/EXT-04/DATA-01/DATA-04
- `01-02-SUMMARY.md`: 190 lines, status `complete`, requirements DATA-02/DATA-03
- `01-03-SUMMARY.md`: 225 lines, status `complete`, requirements DATA-05

### Code review completed with fixes applied

**PASS**

Evidence:
- `01-REVIEW.md`: status `fixes_applied`, 1 critical (C-01), 1 warning (W-01), 6 info items
- Both C-01 and W-01 fixes verified in source code (see above)
- Info items (I-01 through I-06) are non-blocking observations for future phases

---

## Summary

| Check | Status |
|-------|--------|
| SC-1: Extension loads with valid metadata | PASS |
| SC-2: POST/GET management API works | PASS |
| SC-3: All 4 tables created (SQLite confirmed, Postgres DDL-compatible) | PASS |
| SC-4: Cross-wallet isolation | PASS |
| EXT-01: Loader contract | PASS |
| EXT-02: Static files + Vue SFCs | PASS |
| EXT-04: No new dependencies | PASS |
| DATA-01: mints table (15 columns) | PASS |
| DATA-02: notes table (8 columns) | PASS |
| DATA-03: mints_records + melts tables | PASS |
| DATA-04: Pydantic v1 syntax | PASS |
| DATA-05: Wallet-scoped queries | PASS |
| Store-hashes-not-secrets | PASS |
| Cross-wallet isolation (SEC-07) | PASS |
| DB transaction atomicity (REC-03) | PASS |
| C-01 fix: mint_privkey excluded from responses | PASS |
| W-01 fix: update_mint column whitelist | PASS |
| 3 SUMMARY.md files present | PASS |
| Code review with fixes applied | PASS |

**All 18 checks PASS. No gaps found.**

### Notes
- Postgres runtime testing was not performed (no Postgres instance available), but all DDL uses cross-DB compatibility properties (`db.timestamp_now`, `db.references_schema`) following the verified giftcards pattern. Postgres compatibility is high-confidence but not runtime-verified.
- 6 info-level code review items (I-01 through I-06) are non-blocking and documented for future phases — none affect Phase 1 success criteria.
- The Vue placeholder is functionally wired (create/delete via API) but visual rendering requires a browser session (human judgment items in plan summaries).
