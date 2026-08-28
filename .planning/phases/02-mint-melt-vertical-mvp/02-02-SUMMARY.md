---
phase: 02-mint-melt-vertical-mvp
plan: 02
subsystem: mint-flow
tags: [lnbits, lnurl, lud-06, payRequest, fee-math, lazy-settlement, store-hashes-not-secrets, no-secret-logging]

# Dependency graph
requires:
  - phase: 02-mint-melt-vertical-mvp
    provides: "Note state-machine CRUD (settle_mint, get_mint_by_id, get_pending_mint_record) + LNURL wire models (Plan 02-01)"
provides:
  - "Fee math protocol contracts: _mint_fee_msat (ceil rounding, ECON-01), _min_sendable_msat (fee-aware walk, ECON-02), max_mintable_msat (ECON-03), _melt_fee_limit_msat (max 0.5%/5000/mint_fee, ECON-04)"
  - "Lazy settlement helper: _try_settle_mint (check pending record + transaction status + settle_mint compare-and-set)"
  - "LUD-06 payRequest endpoint: GET /lnurlmint/lnurlp/{mint_id} with fee-aware bounds, withdrawLink, commentAllowed"
  - "Mint callback endpoint: GET /lnurlmint/p/cb/{mint_id} — creates invoice via LNbits, records pending mint, returns {pr, disposable:false}"
  - "record_mint_record CRUD helper (INSERT OR IGNORE, stores net amount, no spendable credential)"
  - "_public_base_url helper (per-mint base_url priority, request.base_url fallback)"
  - "_CONFIRMATION_RETRY_DELAYS_SECONDS constant for Plan 04's _confirm_payment"
  - "HEX32_PATTERN regex for Plan 03's /w endpoint k1 validation"
affects: [02-mint-melt-vertical-mvp, 03-rotate-split-merge-sunset, 04-comment-protection-verify]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "LNURL error format: {status: ERROR, reason: ...} as plain dict → FastAPI JSON with HTTP 200 (not HTTPException)"
    - "Fee math as protocol contract: ceil-rounding idiom -(-x // 1000) * 1000, fee-aware minSendable walk, per-mint Mint parameter"
    - "Lazy settlement: note NOT materialized at callback time; _try_settle_mint materializes on first /w poll after settlement"
    - "Per-mint base_url override: mint.base_url takes priority over request.base_url (Tor-aware substitution deferred to Phase 6)"
    - "msat→sat conversion at LNbits API boundary only (create_invoice amount=amount//1000); all fee math stays in msat"

key-files:
  created:
    - lnurlmint/services.py
    - lnurlmint/views_lnurl.py
  modified:
    - lnurlmint/crud.py
    - lnurlmint/__init__.py

key-decisions:
  - "Fee math functions take a Mint parameter (per-mint DB columns) instead of reading global settings — the source uses settings.*, the port uses mint.base_fee_msat etc."
  - "_melt_fee_limit_msat formula preserved exactly (max(0.5%, 5000, mint_fee)) but NOT enforced at LNbits payment layer — LNbits' pay_invoice uses its own fee_reserve; documented deviation (ECON-04 formula preserved for accounting/logging)"
  - "maxSendable advertises mint.max_sendable_msat (gross amount the payer pays), NOT max_mintable_msat (net note value) — matches source behavior"
  - "text/identifier uses {mint.username}@{host} where host is derived from the public base URL's netloc — informational metadata, not a real LUD-16 Lightning Address (deferred to v2)"
  - "LNURL errors returned as plain dicts (HTTP 200) not HTTPException — LUD-06 protocol compliance; the source uses HTTPException which is a different JSON shape"
  - "Task execution order adjusted to dependency order: Task 1 (services.py) → Task 4 (record_mint_record) → Task 2 (views_lnurl.py) → Task 3 (register router) — Task 2 imports from both services.py and record_mint_record"
  - "logger.debug in callback logs only mint_id (not payment_hash, pr, or query params) — SEC-05 no-secret-logging"

patterns-established:
  - "LNURL error dict pattern: return {status: ERROR, reason: ...} for all protocol-level rejections (unknown mint, sunset, amount validation)"
  - "Fee-aware bounds advertisement: minSendable walks up until net >= min_mint_msat; maxSendable is the gross max_sendable_msat"
  - "Lazy materialization: callback records pending mint (minted=0), note materializes on first poll after settlement via _try_settle_mint"

requirements-completed: [EXT-03, MINT-01, MINT-02, MINT-03, MINT-04, MINT-05, ECON-01, ECON-02, ECON-03, ECON-04, SEC-02]

coverage:
  - id: D1
    description: "_mint_fee_msat rounds UP to nearest whole sat via -(-fee_msat // 1000) * 1000 (ECON-01) — never short a sat"
    requirement: ECON-01
    verification:
      - kind: automated
        ref: "assert _mint_fee_msat(100000, mint_with_1000base_10000ppm) == 2000; assert _mint_fee_msat(100000, mint_with_1001base) == 3000 (ceil rounding); grep '-(-.*// 1000) \\* 1000' services.py → 2 matches"
        status: pass
    human_judgment: false
  - id: D2
    description: "_min_sendable_msat walks up from max(min_sendable, min_mint) until net >= min_mint_msat (ECON-02)"
    requirement: ECON-02
    verification:
      - kind: automated
        ref: "assert _min_sendable_msat(mint_1000base_10000ppm) == 12000 (walk: 10000→net 8000, 11000→net 9000, 12000→net 10000); assert _min_sendable_msat(zero_fee_mint) == 10000"
        status: pass
    human_judgment: false
  - id: D3
    description: "max_mintable_msat = max_sendable_msat - _mint_fee_msat(max_sendable_msat) (ECON-03); _melt_fee_limit_msat = max(0.5%, 5000, mint_fee) (ECON-04)"
    requirement: ECON-03
    verification:
      - kind: automated
        ref: "assert max_mintable_msat(m) == 989999000; assert _melt_fee_limit_msat(100000, m) == 5000; assert _melt_fee_limit_msat(10_000_000, m) == 101000; grep 'max(round.*0.005.*5000' services.py → 1 match"
        status: pass
    human_judgment: false
  - id: D4
    description: "GET /lnurlmint/lnurlp/{mint_id} returns LUD-06 payRequest with fee-aware bounds, withdrawLink, commentAllowed (MINT-01)"
    requirement: MINT-01
    verification:
      - kind: automated
        ref: "lnurlmint_lnurl_router routes include /lnurlp/{mint_id} and /p/cb/{mint_id}; grep 'withdrawLink' views_lnurl.py → 2 matches; grep 'commentAllowed' views_lnurl.py → 1 match"
        status: pass
    human_judgment: false
  - id: D5
    description: "GET /lnurlmint/p/cb/{mint_id} creates invoice via LNbits, records pending mint, returns {pr, disposable:false} (MINT-02, MINT-05)"
    requirement: MINT-02
    verification:
      - kind: automated
        ref: "grep 'lnbits_create_invoice' views_lnurl.py → 2 matches; grep 'disposable.*False' views_lnurl.py → 1 match; grep 'record_mint_record' views_lnurl.py → 1 match"
        status: pass
    human_judgment: false
  - id: D6
    description: "Note NOT materialized at callback time — lazy settlement via _try_settle_mint on first /w poll (MINT-03)"
    requirement: MINT-03
    verification:
      - kind: automated
        ref: "views_lnurl.py callback calls record_mint_record (minted=0) but NOT settle_mint; _try_settle_mint in services.py calls settle_mint only after check_transaction_status success"
        status: pass
    human_judgment: false
  - id: D7
    description: "Callback rejects amount < min_sendable, > max_sendable, and net-of-fee < min_mint_msat (MINT-04)"
    requirement: MINT-04
    verification:
      - kind: automated
        ref: "grep 'min_mint_msat' views_lnurl.py → 2 matches; grep 'Amount too low' views_lnurl.py → 1 match; grep 'Amount too high' views_lnurl.py → 1 match"
        status: pass
    human_judgment: false
  - id: D8
    description: "Sunset mode rejects both payRequest and callback with LNURL error (MINT-01/MINT-02)"
    requirement: MINT-01
    verification:
      - kind: automated
        ref: "grep -c 'sunset' views_lnurl.py → 5; grep -c 'status.*ERROR' views_lnurl.py → 8 (both endpoints + amount validation)"
        status: pass
    human_judgment: false
  - id: D9
    description: "Store-hashes-not-secrets: only payment_hash and pr stored in mints_records, no spendable credential (SEC-02)"
    requirement: SEC-02
    verification:
      - kind: automated
        ref: "record_mint_record INSERT columns: payment_hash, mint_id, pr, amount_msat, minted, comment_hash — no preimage/secret column; grep -i 'preimage|secret|raw_k1' views_lnurl.py → 0 matches"
        status: pass
    human_judgment: false
  - id: D10
    description: "No-secret-logging: no logger call includes k1, spendable credential, or full request URL (SEC-05)"
    requirement: SEC-05
    verification:
      - kind: automated
        ref: "grep -i 'request\\.url\\b' views_lnurl.py → 0 matches; logger.debug in callback logs only mint_id"
        status: pass
    human_judgment: false

# Metrics
duration: 12min
completed: 2026-08-28
status: complete
---

# Plan 02-02: Mint Flow (LUD-06 payRequest + Callback) Summary

**Fee math protocol contracts (ECON-01..04), LUD-06 payRequest advertisement with fee-aware bounds, mint callback with LNbits invoice creation + pending mint record, and lazy settlement helper — the note materializes on first poll after settlement, not at callback time.**

## Performance

- **Duration:** ~12 min
- **Tasks:** 4
- **Files created:** 2 (services.py, views_lnurl.py)
- **Files modified:** 2 (crud.py, __init__.py)

## Accomplishments
- services.py: 6 fee math + settlement functions (_mint_fee_msat with ceil rounding, _min_sendable_msat with fee-aware walk, max_mintable_msat, _melt_fee_limit_msat, _public_base_url, _try_settle_mint) + _CONFIRMATION_RETRY_DELAYS_SECONDS constant
- views_lnurl.py: 2 LNURL endpoints (GET /lnurlp/{mint_id} payRequest, GET /p/cb/{mint_id} mint callback) + HEX32_PATTERN for Plan 03
- crud.py: record_mint_record helper (INSERT OR IGNORE, stores net amount, no spendable credential)
- __init__.py: lnurlmint_lnurl_router registered in lnurlmint_ext
- Fee math verified: _mint_fee_msat rounds UP (2000 for even, 3000 for 2001 msat), _min_sendable_msat walks up (12000 for 1000base/10000ppm), _melt_fee_limit_msat = max(0.5%, 5000, mint_fee)
- All LNURL errors return {status: ERROR, reason: ...} as plain dict (HTTP 200, not HTTPException)
- Sunset mode rejects both endpoints with "This mint is sunsetting - minting is disabled."
- No logger call includes k1, spendable credentials, or full request URL (SEC-05)

## Task Commits

Each task was committed atomically (dependency-ordered):

1. **Task 1: Create services.py with fee math and lazy settlement helpers** - `f752aeb` (feat)
2. **Task 4: Add record_mint_record CRUD helper to crud.py** - `0a17ebe` (feat)
3. **Task 2: Create views_lnurl.py with payRequest and mint callback endpoints** - `4844bc1` (feat)
4. **Task 3: Register views_lnurl router in __init__.py** - `ebbb895` (feat)

## Files Created/Modified
- `lnurlmint/services.py` (created) — fee math (_mint_fee_msat, _min_sendable_msat, max_mintable_msat, _melt_fee_limit_msat), _public_base_url, _try_settle_mint, _CONFIRMATION_RETRY_DELAYS_SECONDS; imports lnbits_create_invoice, check_transaction_status, settle_mint, get_pending_mint_record
- `lnurlmint/views_lnurl.py` (created) — lnurlmint_lnurl_router with GET /lnurlp/{mint_id} (payRequest) and GET /p/cb/{mint_id} (mint callback); HEX32_PATTERN for Plan 03
- `lnurlmint/crud.py` (modified) — added record_mint_record (INSERT OR IGNORE into mints_records, stores net amount, minted=0)
- `lnurlmint/__init__.py` (modified) — imported and registered lnurlmint_lnurl_router in lnurlmint_ext

## Decisions Made
- Fee math functions take a Mint parameter (per-mint DB columns) instead of reading global settings — the source uses settings.*, the port uses mint.base_fee_msat etc.
- _melt_fee_limit_msat formula preserved exactly (max(0.5%, 5000, mint_fee)) but NOT enforced at LNbits payment layer — LNbits' pay_invoice uses its own fee_reserve; documented deviation (ECON-04 formula preserved for accounting/logging)
- maxSendable advertises mint.max_sendable_msat (gross amount the payer pays), NOT max_mintable_msat (net note value) — matches source behavior
- text/identifier uses {mint.username}@{host} where host is derived from the public base URL's netloc — informational metadata, not a real LUD-16 Lightning Address (deferred to v2)
- LNURL errors returned as plain dicts (HTTP 200) not HTTPException — LUD-06 protocol compliance; the source uses HTTPException which is a different JSON shape
- Task execution order adjusted to dependency order: Task 1 → Task 4 → Task 2 → Task 3 (Task 2 imports from both services.py and record_mint_record)
- logger.debug in callback logs only mint_id (not payment_hash, pr, or query params) — SEC-05 no-secret-logging

## Deviations from Plan

### Execution Order Adjustment

**Task ordering changed from plan's 1→2→3→4 to 1→4→2→3.**
- **Reason:** Task 2 (views_lnurl.py) imports from services.py (Task 1) and calls record_mint_record (Task 4). Executing in plan order would require Task 2 to reference a CRUD function not yet committed. Dependency-ordered execution ensures each task's imports resolve at commit time.
- **Impact:** None — all 4 tasks complete with identical content; only the commit order changed.

### Auto-fixed Issues

**1. [Documentation] Bare `lnurlmint` import path in acceptance criteria**
- **Found during:** Task 1 verification
- **Issue:** Plan's acceptance criteria use `from lnurlmint.services import ...` but LNbits loads extensions as `lnbits.extensions.lnurlmint` (bare import fails with ModuleNotFoundError) — same documentation simplification as Plans 01-01 through 02-01
- **Fix:** Verified with `from lnbits.extensions.lnurlmint.services import ...` (the actual loader path); no code change needed
- **Committed in:** N/A (documentation simplification in plan, no code impact)

---

**Total deviations:** 1 execution order adjustment + 1 documentation simplification
**Impact on plan:** All acceptance criteria met. No scope creep.

## Issues Encountered
None — all fee math verified on first attempt (after correcting test expectations for _melt_fee_limit_msat where the mint_fee component exceeds 0.5% at high amounts). All routes registered correctly.

## User Setup Required
None — no external service configuration required. The endpoints use LNbits' create_invoice which works with any configured funding source (FakeWallet in tests, real backends in production).

## Next Phase Readiness
- The mint flow (payRequest + callback) is ready for Plan 02-03 (melt flow: informational /w + melt callback) — the withdrawLink in the payRequest points to /lnurlmint/w/{mint_id} which Plan 03 implements
- _try_settle_mint is ready for Plan 02-03's /w endpoint to call on first poll (lazy settlement materialization)
- The fee math functions are ready for Plan 02-03's melt callback (amount validation) and Plan 02-04's _melt_pay (fee limit accounting)
- _CONFIRMATION_RETRY_DELAYS_SECONDS is ready for Plan 02-04's _confirm_payment retry-with-backoff
- HEX32_PATTERN is ready for Plan 02-03's /w endpoint k1 validation
- record_mint_record is ready for Phase 4's comment-protected mint callback (comment_hash parameter)

## Self-Check: PASSED

All acceptance criteria from all 4 tasks verified:
- Task 1: All 6 service functions + constant importable; ceil idiom present (2 grep matches); melt fee limit formula present; _CONFIRMATION_RETRY_DELAYS_SECONDS present; lnbits_create_invoice + check_transaction_status imports present; fee math verified (_mint_fee_msat ceil rounding, _min_sendable_msat walk, max_mintable_msat, _melt_fee_limit_msat)
- Task 4: record_mint_record importable; INSERT OR IGNORE INTO lnurlmint.mints_records present
- Task 2: lnurlmint_lnurl_router has /lnurlp/{mint_id} and /p/cb/{mint_id}; withdrawLink present (2 matches); disposable:False present; sunset present (5 matches); min_mint_msat present (2 matches); lnbits_create_invoice present (2 matches); status:ERROR present (8 matches); no request.url matches (SEC-05)
- Task 3: lnurlmint_ext routes include /lnurlmint/lnurlp/{mint_id} and /lnurlmint/p/cb/{mint_id}; lnurlmint_lnurl_router in __init__.py (2 grep matches: import + include_router)

Plan-level verification:
1. Fee math: _mint_fee_msat rounds UP ✓, _min_sendable_msat walks up ✓, max_mintable_msat nets fee ✓, _melt_fee_limit_msat = max(0.5%, 5000, mint_fee) ✓
2. payRequest: LUD-06 with fee-aware bounds, withdrawLink, commentAllowed ✓
3. Mint callback: creates invoice via LNbits, records pending mint, returns {pr, disposable:false} ✓
4. Lazy settlement: note NOT materialized at callback; _try_settle_mint materializes on poll ✓
5. Amount validation: rejects below min, above max, net-of-fee < min_mint ✓
6. Sunset rejection: both endpoints reject with sunset error ✓
7. Store-hashes-not-secrets: only payment_hash + pr stored, no spendable credential ✓
8. No-secret-logging: no request.url/k1/credential in any logger call ✓

---
*Phase: 02-mint-melt-vertical-mvp*
*Completed: 2026-08-28*
