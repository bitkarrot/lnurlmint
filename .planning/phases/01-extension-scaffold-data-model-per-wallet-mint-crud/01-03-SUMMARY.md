---
phase: 01-extension-scaffold-data-model-per-wallet-mint-crud
plan: 03
subsystem: api
tags: [lnbits, fastapi, pydantic-v1, sqlite, vue, quasar, wallet-scoped, crud, bearer-assets]

# Dependency graph
requires:
  - phase: 01-extension-scaffold-data-model-per-wallet-mint-crud
    provides: "m001 mints table + Mint/CreateMint models + POST/GET management API (Plan 01-01)"
  - phase: 01-extension-scaffold-data-model-per-wallet-mint-crud
    provides: "m002 notes/mints_records/melts tables (Plan 01-02) — delete_mint's outstanding-notes guard queries lnurlmint.notes"
provides:
  - "Complete per-wallet mint CRUD: get_mint, update_mint, delete_mint, count_outstanding_notes (all wallet-scoped)"
  - "Management API: GET/PUT/DELETE /lnurlmint/api/v1/mints/{mint_id} with require_invoice_key/require_admin_key"
  - "Outstanding-notes delete guard (409 Conflict) with atomic check-and-delete via async with db.connect()"
  - "UpdateMint pydantic v1 model for partial mint config updates (10 configurable fields, immutable fields excluded)"
  - "Vue placeholder with create-mint form (username input) and delete button per mint row"
affects: [02-mint-melt-vertical-mvp, 03-rotate-split-merge-sunset, 06-tor-frontend]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Dynamic SET clause for partial updates (update_mint builds SET from non-None field keys)"
    - "Atomic check-and-delete via async with db.connect() as conn: (delete_mint: count outstanding notes + delete in one transaction)"
    - "Wallet-scoped JOIN for tables without a wallet column (count_outstanding_notes JOINs lnurlmint.notes on lnurlmint.mints for m.wallet scoping)"
    - "Ownership pre-check before destructive operations (DELETE calls get_mint before delete_mint to enforce 404 for cross-wallet access)"
    - "LNbits.api.request('POST'/'DELETE', url, adminkey, data) — the real LNbits JS API (no .post/.delete helpers)"

key-files:
  created: []
  modified:
    - lnurlmint/crud.py
    - lnurlmint/models.py
    - lnurlmint/views_api.py
    - lnurlmint/static/js/index.vue
    - lnurlmint/static/js/index.js

key-decisions:
  - "DELETE endpoint pre-checks get_mint before delete_mint — without this, a cross-wallet DELETE returns 200 (delete_mint finds 0 outstanding notes via the wallet-scoped JOIN and deletes 0 rows, returning True); the get_mint check enforces the 404"
  - "count_outstanding_notes uses JOIN lnurlmint.notes n JOIN lnurlmint.mints m ON n.mint_id = m.id WHERE m.wallet = :wallet — the notes table has no wallet column, so wallet scoping is enforced via the JOIN"
  - "UpdateMint root_validator only checks sendable bounds when both min_sendable_msat and max_sendable_msat are explicitly provided (partial update may set only one)"
  - "Vue uses LNbits.api.request('POST'/'DELETE', ...) — same deviation as Plan 01-01: LNbits JS API has no .post/.delete helpers, only request(method, url, key, data)"

patterns-established:
  - "Ownership pre-check pattern: destructive endpoints (DELETE) call get_mint first to enforce 404 for cross-wallet access, before the CRUD function that might silently succeed on 0 rows"
  - "Atomic check-and-delete: async with db.connect() as conn: block for outstanding-notes check + delete in one transaction (LNbits db.execute opens a separate transaction per call)"
  - "Partial update via dynamic SET clause: update_mint builds SET from non-None field keys + updated_at timestamp, uses db.timestamp_placeholder('now') for cross-DB compatibility"

requirements-completed: [DATA-05]

coverage:
  - id: D1
    description: "GET /lnurlmint/api/v1/mints/{mint_id} with invoice key returns the mint (404 if wrong wallet)"
    requirement: DATA-05
    verification:
      - kind: integration
        ref: "curl GET /lnurlmint/api/v1/mints/{mint_id} as wallet A → 200; as wallet B → 404"
        status: pass
    human_judgment: false
  - id: D2
    description: "PUT /lnurlmint/api/v1/mints/{mint_id} with admin key updates mint config (404 if wrong wallet)"
    requirement: DATA-05
    verification:
      - kind: integration
        ref: "curl PUT /lnurlmint/api/v1/mints/{mint_id} {base_fee_msat:500} as wallet A → 200, base_fee_msat=500; as wallet B → 404"
        status: pass
    human_judgment: false
  - id: D3
    description: "DELETE /lnurlmint/api/v1/mints/{mint_id} with admin key deletes (409 if outstanding notes, 404 if wrong wallet)"
    requirement: DATA-05
    verification:
      - kind: integration
        ref: "curl DELETE with outstanding note → 409; after cleaning note → 200; as wrong wallet → 404"
        status: pass
    human_judgment: false
  - id: D4
    description: "Cross-wallet isolation — every CRUD function includes WHERE wallet = :wallet (or JOIN on m.wallet = :wallet)"
    requirement: DATA-05
    verification:
      - kind: automated
        ref: "grep -c 'WHERE wallet = :wallet\\|m.wallet = :wallet' crud.py → 5"
        status: pass
    human_judgment: false
  - id: D5
    description: "Vue placeholder with create-mint form and delete button wired to the management API"
    requirement: DATA-05
    verification:
      - kind: automated
        ref: "grep createMint/deleteMint index.js → matches; grep 'Create Mint'/createDialog index.vue → matches; grep delete index.vue → matches"
        status: pass
    human_judgment: true
    rationale: "Visual rendering of the Vue SPA (dialog, form, button interaction) requires a browser session; automated check confirms the wiring exists"

# Metrics
duration: 22min
completed: 2026-08-28
status: complete
---

# Plan 01-03: Per-Wallet Mint CRUD (Get/Update/Delete) + Cross-Wallet Isolation Summary

**Complete mint CRUD with GET/PUT/DELETE /{mint_id} endpoints, atomic outstanding-notes delete guard (409), cross-wallet isolation E2E-verified on all three new endpoints, and Vue create-mint form + delete button wired to the API**

## Performance

- **Duration:** ~22 min
- **Started:** 2026-08-28T18:45Z
- **Completed:** 2026-08-28T19:07Z
- **Tasks:** 4
- **Files modified:** 5

## Accomplishments
- Four new wallet-scoped CRUD functions: get_mint, update_mint, count_outstanding_notes, delete_mint — all include WHERE wallet = :wallet (or JOIN on m.wallet = :wallet for the notes query)
- delete_mint uses `async with db.connect() as conn:` for atomic check-and-delete (outstanding-notes count + delete in one transaction — the LNbits Database abstraction otherwise opens a separate transaction per call)
- Three new management API endpoints: GET /{mint_id} (invoice key), PUT /{mint_id} (admin key, partial update via UpdateMint), DELETE /{mint_id} (admin key, 409 if outstanding notes)
- UpdateMint pydantic v1 model with 10 all-Optional configurable fields, excludes immutable fields (id, wallet, mint_privkey, timestamps), root_validator checks sendable bounds only when both provided
- Cross-wallet isolation E2E verified: wallet B gets 404 on GET/PUT/DELETE of wallet A's mint; wallet A succeeds on all three
- Outstanding-notes delete guard E2E verified: DELETE with an unspent note returns 409; after cleaning the note, DELETE succeeds
- Vue placeholder updated: Create Mint q-btn opens a q-dialog with username q-form; delete q-btn per mint row; 409 error shows user-visible "Cannot delete mint with outstanding notes" message

## Task Commits

Each task was committed atomically:

1. **Task 1: get_mint, update_mint, delete_mint, count_outstanding_notes CRUD + UpdateMint model** - `59a1e89` (feat)
2. **Task 2: GET/PUT/DELETE /{mint_id} management API endpoints** - `5bda1e7` (feat)
3. **Task 3: Cross-wallet isolation verification + DELETE ownership fix** - `8926789` (fix)
4. **Task 4: Vue placeholder with create-mint form and delete button** - `50024c1` (feat)

## Files Created/Modified
- `lnurlmint/crud.py` - Added get_mint (wallet-scoped SELECT), update_mint (dynamic SET clause, wallet-scoped), count_outstanding_notes (JOIN on mints for wallet scoping), delete_mint (atomic check-and-delete via async with db.connect()); added `import time` for updated_at timestamp
- `lnurlmint/models.py` - Added UpdateMint pydantic v1 model: 10 all-Optional configurable fields, root_validator for sendable bounds (only when both provided), excludes id/wallet/mint_privkey/timestamps
- `lnurlmint/views_api.py` - Added GET/PUT/DELETE /{mint_id} endpoints with HTTPException for 404/409; imported HTTPException, UpdateMint, get_mint/update_mint/delete_mint; DELETE pre-checks get_mint for cross-wallet 404
- `lnurlmint/static/js/index.vue` - Added Create Mint q-btn, q-dialog with username q-form, delete q-btn per mint row, q-banner for error messages
- `lnurlmint/static/js/index.js` - Added createMint() (LNbits.api.request POST + refresh), deleteMint() (LNbits.api.request DELETE + refresh, 409 handling), openCreateDialog(), createDialog/errorMessage data state

## Decisions Made
- DELETE endpoint pre-checks get_mint before delete_mint — without this, a cross-wallet DELETE returns 200 (delete_mint finds 0 outstanding notes via the wallet-scoped JOIN and deletes 0 rows, returning True). The get_mint check enforces the 404, matching GET and PUT behavior. Found during Task 3 E2E testing.
- count_outstanding_notes uses JOIN lnurlmint.notes n JOIN lnurlmint.mints m ON n.mint_id = m.id WHERE m.wallet = :wallet — the notes table has no wallet column (by design — wallet scoping is via the mints FK), so the JOIN enforces isolation
- UpdateMint root_validator only checks sendable bounds when both min and max are explicitly provided — a partial update may set only one bound, and the existing CreateMint validator already enforces the full constraint at creation time
- Vue uses LNbits.api.request('POST'/'DELETE', url, adminkey, data) — same deviation as Plan 01-01: the real LNbits JS API has no .post/.delete helpers, only request(method, url, key, data)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Bare `lnurlmint` import path in acceptance criteria**
- **Found during:** Task 1 (CRUD import verification)
- **Issue:** Plan's acceptance criteria use `from lnurlmint.crud import ...` but LNbits loads extensions as `lnbits.extensions.lnurlmint` (the bare import fails with ModuleNotFoundError) — same documentation simplification as Plans 01-01 and 01-02
- **Fix:** Verified with `from lnbits.extensions.lnurlmint.crud import ...` (the actual loader path); no code change needed
- **Files modified:** none
- **Verification:** `from lnbits.extensions.lnurlmint.crud import get_mint, update_mint, delete_mint, count_outstanding_notes` succeeds
- **Committed in:** N/A (documentation simplification in plan, no code impact)

**2. [Rule 1 - Bug] Route path assertion in acceptance criteria**
- **Found during:** Task 2 (API endpoint verification)
- **Issue:** Plan's acceptance criteria assert `'/{mint_id}' in paths` but FastAPI resolves route paths with the router prefix applied, so `r.path` returns `/api/v1/mints/{mint_id}` not `/{mint_id}` — same as Plan 01-01
- **Fix:** Verified with `any('{mint_id}' in p for p, _ in routes)` (the real check); no code change needed
- **Files modified:** none
- **Verification:** `lnurlmint_api_router.routes` contains 5 routes including `('/api/v1/mints/{mint_id}', ['GET'])`, `('/api/v1/mints/{mint_id}', ['PUT'])`, `('/api/v1/mints/{mint_id}', ['DELETE'])`
- **Committed in:** N/A (documentation simplification in plan, no code impact)

**3. [Rule 2 - Missing Critical] Vue API call method**
- **Found during:** Task 4 (Vue create/delete wiring)
- **Issue:** Plan's acceptance criteria specify `LNbits.api.post.*mints` and `LNbits.api.delete.*mints` but LNbits' JS API has no `post`/`delete` helpers — only `request(method, url, apiKey, data)` — same deviation as Plan 01-01
- **Fix:** Used `LNbits.api.request('POST', '/lnurlmint/api/v1/mints', wallet.adminkey, data)` and `LNbits.api.request('DELETE', '/lnurlmint/api/v1/mints/' + mint_id, wallet.adminkey)` following the giftcards convention
- **Files modified:** lnurlmint/static/js/index.js
- **Verification:** `grep "LNbits.api" index.js` returns 3 matches; createMint and deleteMint use the correct API
- **Committed in:** 50024c1 (Task 4 commit)

**4. [Rule 1 - Bug] `WHERE wallet = :wallet` grep pattern in acceptance criteria**
- **Found during:** Task 1 (CRUD wallet-scoping verification)
- **Issue:** Plan's Task 1 acceptance criterion `grep "WHERE wallet = :wallet"` expects at least 4 matches, but the new SQL queries (get_mint, update_mint, delete_mint) use `WHERE id = :id AND wallet = :wallet` (compound clause) — the standalone `WHERE wallet = :wallet` pattern only matches get_mints_by_wallet's SQL + 2 docstring mentions = 3. The actual security property (all queries wallet-scoped) is satisfied: `grep -c "wallet = :wallet"` returns 8.
- **Fix:** Verified with the Task 3 criterion `grep -c "WHERE wallet = :wallet\|m.wallet = :wallet"` which returns 5 (≥5 ✓); no code change needed — the compound WHERE clauses correctly scope by wallet
- **Files modified:** none
- **Verification:** `grep -c "WHERE wallet = :wallet\|m.wallet = :wallet" crud.py` → 5; `grep -c "wallet = :wallet" crud.py` → 8
- **Committed in:** N/A (documentation simplification in plan, no code impact)

**5. [Rule 1 - Bug] Cross-wallet DELETE returns 200 instead of 404**
- **Found during:** Task 3 (cross-wallet isolation E2E testing)
- **Issue:** DELETE /{mint_id} as wallet B (wrong wallet) returned 200 instead of 404 — delete_mint finds 0 outstanding notes via the wallet-scoped JOIN (no rows match wallet B) and deletes 0 rows (WHERE wallet = :wallet doesn't match), returning True. The endpoint then returns {"success": true} with 200.
- **Fix:** Added a get_mint ownership pre-check before delete_mint in the DELETE endpoint — if get_mint returns None (mint doesn't exist or belongs to another wallet), raise HTTPException(404). This matches the GET and PUT endpoints' behavior.
- **Files modified:** lnurlmint/views_api.py
- **Verification:** E2E: DELETE as wallet B → 404; DELETE as wallet A → 200 (success); DELETE with outstanding notes → 409
- **Committed in:** 8926789 (Task 3 fix commit)

---

**Total deviations:** 5 auto-fixed (3 documentation simplifications in plan acceptance criteria, 1 missing critical Vue API method, 1 cross-wallet DELETE 404 fix)
**Impact on plan:** All deviations are plan-text simplifications vs actual LNbits/SQLite conventions, plus one necessary fix found during E2E testing (DELETE ownership pre-check). No scope creep. The implementation follows the established giftcards pattern and the actual LNbits loader/API contract.

## Issues Encountered
- LNbits was running with the old code (before the new endpoints were added); needed a restart to pick up the new routes. Killed the stale process and restarted.
- The `lnurlmint.notes` table is not accessible via the `lnurlmint.notes` schema prefix in the sqlite3 CLI (SQLite stores attached-DB tables without the schema prefix) — same behavior as Plans 01-01 and 01-02. Used the bare table name `notes` for the outstanding-notes test fixture insert.

## User Setup Required
None - no external service configuration required. The extension is auto-discovered via the symlink at `lnbits/extensions/lnurlmint`. Users enable it through the LNbits UI (Extensions page).

## Next Phase Readiness
- Phase 1 is complete: the full per-wallet mint CRUD vertical slice is delivered (create, list, get, update, delete) with cross-wallet isolation E2E-verified, the outstanding-notes delete guard (409), the complete data model (all four tables), and a Vue placeholder with create/delete interactivity
- The `async with db.connect() as conn:` transaction discipline (delete_mint's atomic check-and-delete) is now exercised — Phase 2's note CRUD (settle_mint, mark_pending, finalize_melt, restore) will follow the same pattern for multi-statement atomicity
- The ownership pre-check pattern (get_mint before destructive operations) is established for Phase 2's note operations, which will also need wallet-scoped ownership checks before mutating note state
- Phase 2 can proceed: note CRUD, the confirm-before-burn state machine, mint/melt LNURL endpoints, in-flight tracking, background reconciliation, and the five critical PoC tests

## Self-Check: PASSED

All acceptance criteria from all 4 tasks verified:
- Task 1: CRUD functions importable (`from lnbits.extensions.lnurlmint.crud import get_mint, update_mint, delete_mint, count_outstanding_notes`); UpdateMint allows empty partial update; UpdateMint(username='new').username == 'new'; immutable fields excluded (id, wallet, mint_privkey not in __fields__); `grep -c "WHERE wallet = :wallet\|m.wallet = :wallet" crud.py` → 5; `grep "async with db.connect" crud.py` → 2 matches; `grep "JOIN lnurlmint.mints m ON" crud.py` → 2 matches
- Task 2: /{mint_id} routes present (GET, PUT, DELETE); `grep "require_invoice_key"` → 3; `grep "require_admin_key"` → 4; `grep "409\|outstanding"` → 5; `grep "404\|not found"` → 6; `grep "wallet.wallet.id"` → 5
- Task 3: E2E cross-wallet isolation — GET/PUT/DELETE as wrong wallet → 404; GET/PUT/DELETE as owner → 200/success; DELETE with outstanding notes → 409; DELETE after cleaning → 200; `grep -c "WHERE wallet = :wallet\|m.wallet = :wallet"` → 5; `grep "JOIN lnurlmint.mints m ON"` → 2
- Task 4: `grep "createMint" index.js` → match; `grep "deleteMint" index.js` → match; POST/DELETE wired via LNbits.api.request; `grep "409\|outstanding" index.js` → match; `grep "Create Mint\|createDialog" index.vue` → matches; `grep "delete\|Delete" index.vue` → match

Plan-level verification:
1. GET by id: 200 (owner), 404 (wrong wallet) ✓
2. PUT update: 200, base_fee_msat updated to 500 ✓
3. DELETE with guard: 409 (outstanding notes), 200 (clean) ✓
4. Cross-wallet isolation: 5 wallet-scoped queries, E2E 404 on all cross-wallet access ✓
5. Atomic delete: async with db.connect() used (2 matches) ✓
6. UI: createMint + deleteMint wired to API ✓

---
*Phase: 01-extension-scaffold-data-model-per-wallet-mint-crud*
*Completed: 2026-08-28*
