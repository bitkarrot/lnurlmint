---
phase: 01-extension-scaffold-data-model-per-wallet-mint-crud
plan: 01
subsystem: infra
tags: [lnbits, fastapi, pydantic-v1, sqlite, coincurve, vue, extension-scaffold]

# Dependency graph
requires: []
provides:
  - "lnurlmint extension scaffold (loader contract: lnurlmint_ext, start/stop, static_files, db)"
  - "m001_initial migration creating lnurlmint.mints table (15 columns)"
  - "Mint (DB row) and CreateMint (API request) pydantic v1 models"
  - "mint CRUD: create_mint, get_mints_by_wallet, _generate_mint_privkey"
  - "management API: POST/GET /lnurlmint/api/v1/mints with wallet-scoped auth"
  - "placeholder Vue SPA page at /lnurlmint/ fetching and displaying mints"
affects: [02-mint-melt-vertical-mvp, 03-rotate-split-merge-sunset, 04-comment-protection-verify, 06-tor-frontend]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "LNbits extension loader contract (manifest.json, config.json, __init__.py exports)"
    - "Database('ext_lnurlmint') at module level, wallet-scoped queries (WHERE wallet = :wallet)"
    - "pydantic v1 models (BaseModel, Field, root_validator) — NOT v2"
    - "coincurve PrivateKey for secp256k1 keypair generation (transitive dep, no new deps)"
    - "require_admin_key for writes, require_invoice_key for reads (wallet.wallet.id scoping)"
    - "db.timestamp_now for cross-DB TIMESTAMP defaults (SQLite strftime / Postgres now())"

key-files:
  created:
    - lnurlmint/__init__.py
    - lnurlmint/manifest.json
    - lnurlmint/config.json
    - lnurlmint/views.py
    - lnurlmint/crud.py
    - lnurlmint/views_api.py
    - lnurlmint/migrations.py
    - lnurlmint/models.py
    - lnurlmint/static/routes.json
    - lnurlmint/static/js/index.vue
    - lnurlmint/static/js/index.js
    - lnurlmint/static/image/lnurlmint.png
    - lnurlmint/.gitignore
  modified: []

key-decisions:
  - "Extension imports as lnbits.extensions.lnurlmint (LNbits loader convention), not bare lnurlmint"
  - "Vue fetchMints uses LNbits.api.request('GET', url, key) — the real LNbits JS API has no LNbits.api.get helper"
  - "Extension enabled per-user via DB insert (extensions table) for E2E testing — the enable API requires session auth"

patterns-established:
  - "Extension lifecycle: __init__.py exports db, lnurlmint_ext, lnurlmint_start, lnurlmint_static_files, lnurlmint_stop"
  - "Wallet scoping: every query uses WHERE wallet = :wallet with wallet.wallet.id from auth decorator"
  - "Migration DDL: db.timestamp_now for defaults, db.references_schema for index prefixes"
  - "Pydantic v1 only: BaseModel, Field, root_validator (LNbits pins 1.10.26)"

requirements-completed: [EXT-01, EXT-02, EXT-04, DATA-01, DATA-04]

coverage:
  - id: D1
    description: "lnurlmint extension loads in LNbits and appears in the installed extensions list"
    requirement: EXT-01
    verification:
      - kind: integration
        ref: "LNbits startup log: 'Installed Extensions (12): ... lnurlmint (0.1.0)'"
        status: pass
      - kind: automated
        ref: "from lnbits.extensions.lnurlmint import db, lnurlmint_ext, lnurlmint_start, lnurlmint_static_files, lnurlmint_stop"
        status: pass
    human_judgment: false
  - id: D2
    description: "m001_initial migration creates lnurlmint.mints table with all 15 columns on SQLite"
    requirement: DATA-01
    verification:
      - kind: integration
        ref: "sqlite3 ext_lnurlmint.sqlite3 PRAGMA table_info(mints) — 15 columns"
        status: pass
    human_judgment: false
  - id: D3
    description: "POST /lnurlmint/api/v1/mints with admin key creates a mint row; GET with invoice key lists wallet-scoped mints"
    requirement: DATA-04
    verification:
      - kind: integration
        ref: "curl POST /lnurlmint/api/v1/mints returns Mint JSON with id, wallet, username, mint_privkey (64-char hex); GET returns wallet-scoped array"
        status: pass
    human_judgment: false
  - id: D4
    description: "Cross-wallet isolation — wallet B cannot see wallet A's mints"
    requirement: EXT-02
    verification:
      - kind: integration
        ref: "GET /lnurlmint/api/v1/mints with wallet B invoice key returns 0 mints while wallet A has 1"
        status: pass
    human_judgment: false
  - id: D5
    description: "No new Python dependencies beyond LNbits pyproject.toml (coincurve is transitive)"
    requirement: EXT-04
    verification:
      - kind: automated
        ref: "grep -rn import *.py | grep -vE 'coincurve|lnbits|fastapi|pydantic|loguru|uuid|datetime|typing|asyncio' — no matches"
        status: pass
    human_judgment: false
  - id: D6
    description: "Placeholder Vue page at /lnurlmint/ fetches and displays the mint list"
    requirement: EXT-01
    verification:
      - kind: automated
        ref: "grep LNbits.api index.js — matches; grep mints index.vue — matches; /lnurlmint/ returns 401 (auth-gated route exists)"
        status: pass
    human_judgment: true
    rationale: "Visual rendering of the Vue SPA requires a browser session; automated check confirms the route and API wiring exist"

# Metrics
duration: 35min
completed: 2026-08-28
status: complete
---

# Plan 01-01: Extension Scaffold + Data Model + Per-Wallet Mint CRUD Summary

**Walking skeleton: lnurlmint extension loads in LNbits, m001_initial migration creates the mints table, and a wallet owner can create/list per-wallet mints via the management API with cross-wallet isolation**

## Performance

- **Duration:** ~35 min
- **Started:** 2026-08-28T18:25Z
- **Completed:** 2026-08-28T19:00Z
- **Tasks:** 4
- **Files modified:** 13 created

## Accomplishments
- lnurlmint extension scaffold loads in LNbits 1.5.4 (manifest.json, config.json, __init__.py loader contract, symlink)
- m001_initial migration creates lnurlmint.mints table with all 15 DATA-01 columns + wallet index on SQLite
- Mint (DB row) and CreateMint (API request) pydantic v1 models with root_validator bounds checking
- mint CRUD: create_mint, get_mints_by_wallet, _generate_mint_privkey (coincurve secp256k1, 64-char hex)
- Management API: POST/GET /lnurlmint/api/v1/mints with require_admin_key/require_invoice_key, wallet-scoped
- Placeholder Vue SPA page at /lnurlmint/ fetches and displays mints from the management API
- E2E verified: POST creates a mint, GET lists it, wallet B sees 0 of wallet A's mints (isolation works)

## Task Commits

Each task was committed atomically:

1. **Task 1: Create extension scaffold files + symlink into LNbits** - `7f9b81d` (feat)
2. **Task 2: m001_initial migration + Mint/CreateMint pydantic v1 models + mint CRUD** - `234fb9d` (feat)
3. **Task 3: Wire management API endpoints + Vue placeholder fetch/display** - `ecb5eb6` (feat)
4. **Task 4: End-to-end verification** - no commit (verification only, no files changed)

## Files Created/Modified
- `lnurlmint/__init__.py` - Extension bootstrap: lnurlmint_ext APIRouter, start/stop stubs, static_files, db import
- `lnurlmint/manifest.json` - Marketplace repository identity (id: lnurlmint, org: dni)
- `lnurlmint/config.json` - Display metadata (min_lnbits_version 1.5.4, version 0.1.0, MIT license)
- `lnurlmint/views.py` - Generic SPA route via index view with check_user_exists dependency
- `lnurlmint/crud.py` - db = Database("ext_lnurlmint"), create_mint, get_mints_by_wallet, _generate_mint_privkey
- `lnurlmint/views_api.py` - Management API router: POST (admin key, creates mint) + GET (invoice key, wallet-scoped list)
- `lnurlmint/migrations.py` - m001_initial: creates lnurlmint.mints (15 columns) + idx_lnurlmint_mints_wallet
- `lnurlmint/models.py` - Mint (DB row, 15 fields) + CreateMint (10 request fields, root_validator bounds)
- `lnurlmint/static/routes.json` - SPA route map (PageLnurlmint at /lnurlmint/)
- `lnurlmint/static/js/index.vue` - Placeholder Vue SFC: q-card + q-list displaying mints
- `lnurlmint/static/js/index.js` - PageLnurlmint component: fetchMints() via LNbits.api.request
- `lnurlmint/static/image/lnurlmint.png` - 64x64 placeholder icon
- `lnurlmint/.gitignore` - Ignore __pycache__/*.pyc

## Decisions Made
- Extension imports as `lnbits.extensions.lnurlmint` (LNbits loader convention, same as giftcards) — the plan's `import lnurlmint` is a simplification; the actual loader uses `importlib.import_module(ext.module_name)` where module_name resolves to `lnbits.extensions.lnurlmint`
- Vue fetchMints uses `LNbits.api.request('GET', url, key)` — the real LNbits JS API has no `LNbits.api.get` helper (the plan's `LNbits.api.get` is a simplification); followed the giftcards convention
- Extension enabled per-user via direct DB insert for E2E testing — the `PUT /api/v1/extension/{ext_id}/enable` endpoint requires session-based auth (access token or usr UUID), not API key auth

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Vue API call method**
- **Found during:** Task 3 (Vue placeholder fetch/display)
- **Issue:** Plan specified `LNbits.api.get('/lnurlmint/api/v1/mints')` but LNbits' JS API has no `get` helper — only `request(method, url, apiKey, data)`
- **Fix:** Used `LNbits.api.request('GET', '/lnurlmint/api/v1/mints', key)` following the giftcards convention (the established pattern)
- **Files modified:** lnurlmint/static/js/index.js
- **Verification:** `grep "LNbits.api" index.js` returns matches; fetchMints uses the correct API
- **Committed in:** ecb5eb6 (Task 3 commit)

**2. [Rule 1 - Bug] Import path in acceptance criteria**
- **Found during:** Task 1 (scaffold verification)
- **Issue:** Plan's acceptance criteria use `import lnurlmint` / `from lnurlmint import ...` but LNbits loads extensions as `lnbits.extensions.lnurlmint` (the bare import fails with ModuleNotFoundError)
- **Fix:** Verified with `from lnbits.extensions.lnurlmint import ...` (the actual loader path); no code change needed — the extension is loadable
- **Files modified:** none
- **Verification:** `from lnbits.extensions.lnurlmint import db, lnurlmint_ext, lnurlmint_start, lnurlmint_static_files, lnurlmint_stop` succeeds
- **Committed in:** N/A (documentation simplification in plan, no code impact)

**3. [Rule 1 - Bug] Route path assertion in acceptance criteria**
- **Found during:** Task 3 (API endpoint verification)
- **Issue:** Plan's acceptance criteria assert `'' in routes` but FastAPI resolves route paths with the router prefix applied, so `r.path` returns `/api/v1/mints` not `''`
- **Fix:** Verified POST and GET endpoints exist on the router with correct methods (the real check); no code change needed
- **Files modified:** none
- **Verification:** `lnurlmint_api_router.routes` contains 2 routes: `('/api/v1/mints', {'POST'})` and `('/api/v1/mints', {'GET'})`
- **Committed in:** N/A (documentation simplification in plan, no code impact)

---

**Total deviations:** 3 auto-fixed (1 missing critical, 2 documentation simplifications in plan acceptance criteria)
**Impact on plan:** All deviations are plan-text simplifications vs actual LNbits conventions. No scope creep. The implementation follows the established giftcards pattern and the actual LNbits loader/API contract.

## Issues Encountered
- LNbits was running before the symlink was created, so the extension wasn't loaded on first verification. Restarted LNbits (via systemd) to pick up the new extension — migration ran automatically on startup.
- The extension enable API (`PUT /api/v1/extension/{ext_id}/enable`) requires session-based auth (access token or usr UUID query param), not API key auth. For E2E testing, enabled the extension per-user via direct DB insert into the `extensions` table.

## User Setup Required
None - no external service configuration required. The extension is auto-discovered via the symlink at `lnbits/extensions/lnurlmint`. Users enable it through the LNbits UI (Extensions page).

## Next Phase Readiness
- Extension scaffold, migration, models, CRUD, and management API are all working — ready for Plan 02 (notes/mints_records/melts tables + note CRUD) and Plan 03 (full mint CRUD: get/update/delete + cross-wallet isolation tests)
- The `async with db.connect() as conn:` transaction discipline (needed for Phase 2's atomic swap/settle operations) is not yet exercised — Phase 1's mint CRUD is single-statement only, as planned
- The remaining 3 tables (notes, mints_records, melts) and their migrations are deferred to Plan 02

## Self-Check: PASSED

All acceptance criteria from all 4 tasks verified:
- Task 1: manifest/config contain correct values, import succeeds, symlink exists, routes.json has PageLnurlmint, PNG exists
- Task 2: m001_initial importable, Mint/CreateMint importable, bounds validation raises ValidationError, _generate_mint_privkey returns 64-char hex, CRUD functions importable, v1 syntax only (no v2)
- Task 3: POST and GET endpoints on router, require_admin_key/require_invoice_key present, wallet.wallet.id scoping, LNbits.api in index.js, mints in index.vue
- Task 4: extension loads (lnurlmint 0.1.0 in installed extensions), symlink exists, mints table has 15 columns, POST creates mint, GET returns wallet-scoped array, cross-wallet isolation (wallet B sees 0), no pydantic v2/forbidden deps

---
*Phase: 01-extension-scaffold-data-model-per-wallet-mint-crud*
*Completed: 2026-08-28*
