---
phase: 02-mint-melt-vertical-mvp
plan: 05
subsystem: testing
tags: [pytest, anyio, bolt11, tristate, confirm-before-burn, lnurl, security-poc, fake-node, in-memory-sqlite]

# Dependency graph
requires:
  - phase: 02-mint-melt-vertical-mvp
    provides: "Plans 02-01..02-04: note CRUD state machine (settle_mint, mark_pending, finalize_melt, restore, pending_melts), services.py (_melt_pay tristate, _confirm_payment, reconcile_pending_melts, in-flight registry), views_lnurl.py (/w, /w/cb, /p/cb endpoints), migrations (m001/m002)"
provides:
  - "tests/conftest.py: FakeNode/HodlNode/InFlightNode test fixtures that monkeypatch services.py + views_lnurl.py module-level payment imports with controllable tristate behaviour"
  - "tests/test_poc_duplicate_melt.py: TEST-01 — double-melt rejection (pending state prevents second melt)"
  - "tests/test_poc_a2_settle_race.py: TEST-02 — compare-and-set settle_mint atomicity (no double-mint)"
  - "tests/test_melt_restore_double_payout_poc.py: TEST-03 — tristate settlement (paid=None leaves pending, paid=True finalizes, paid=False restores)"
  - "tests/test_poc_reconcile_inflight_race.py: TEST-04 — reconcile skips in-flight melts (no double-spend from restore-during-live-payment)"
  - "tests/test_poc_f2_pending_info_leak.py: TEST-05 — /w rejects pending notes with 'pending' reason (no sell-during-melt scam)"
  - "fake_invoice helper + mint_note helper (real lazy-settlement path via _try_settle_mint)"
affects: [03-rotate-split-merge-sunset, 04-comment-protection-verify, 07-full-test-suite-port]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "FakeNode monkeypatching: patch module-level imports in services.py AND views_lnurl.py (lnbits_create_invoice, lnbits_pay_invoice, check_transaction_status) — views_lnurl imports create_invoice separately from services"
    - "Tristate modelling via PaymentStatus: paid=True (PaymentSuccessStatus), paid=False (PaymentFailedStatus), paid=None (PaymentPendingStatus) — NOT the .pending property (True for both None and False)"
    - "HodlNode models paid=None via PaymentPendingStatus while pending_hodl is non-empty (source raised; LNbits returns paid=None — equivalent per RQ12)"
    - "InFlightNode models the pre-registration window with asyncio.Event coordination (pay_started/pay_release)"
    - "Per-test DB isolation: drop + re-migrate ext_lnurlmint tables in db_setup fixture; clear _in_flight_melts between tests"
    - "_CONFIRMATION_RETRY_DELAYS_SECONDS monkeypatched to () for fast single-attempt confirmation"
    - "Endpoint functions called directly with bare BackgroundTasks (tasks register but don't run outside FastAPI response machinery) — keeps notes pending for duplicate-melt testing"
    - "@pytest.mark.anyio (anyio plugin) for async tests — LNbits uses anyio, not pytest-asyncio"

key-files:
  created:
    - tests/__init__.py
    - tests/conftest.py
    - tests/test_poc_duplicate_melt.py
    - tests/test_poc_a2_settle_race.py
    - tests/test_melt_restore_double_payout_poc.py
    - tests/test_poc_reconcile_inflight_race.py
    - tests/test_poc_f2_pending_info_leak.py
  modified: []

key-decisions:
  - "Tests live in lnurlmint/tests/ (symlinked into lnbits/extensions/lnurlmint/tests/); run via `cd /home/exedev/lnbits && .venv/bin/python -m pytest lnbits/extensions/lnurlmint/tests/` — the plan's `tests/` path is relative to the extension root, and `cd /home/exedev/lnbits` is where the venv lives (documentation simplification, same as Plans 02-01..02-04)"
  - "Async tests use @pytest.mark.anyio (anyio plugin, LNbits convention) NOT @pytest.mark.asyncio (pytest-asyncio not installed) — matches the giftcards extension test pattern"
  - "Test files import helpers via `from lnurlmint.tests.conftest import fake_invoice, mint_note` (full package path) — a bare `from tests.conftest import ...` would collide with lnbits' root tests/ package; the full path resolves correctly under pytest's prepend import mode (lnbits/extensions/ has no __init__.py, so lnurlmint is importable as top-level)"
  - "FakeNode patches BOTH services_module and views_module — views_lnurl.py imports lnbits_create_invoice separately at module level (line 24); only patching services_module would leave the mint callback (get_pay_callback) calling the real LNbits create_invoice"
  - "InFlightNode.check_transaction_status checks `if payment_hash in self.settled: return PaymentSuccessStatus()` before returning PaymentFailedStatus — so the mint flow's lazy settlement (mint_note setup) still materializes the note (the mint payment_hash is in settled), while melt payment hashes (not yet settled) report paid=False (lnd 404 for unregistered payment)"
  - "HodlNode.settle_hodl_payments decodes each pending_hodl payment_request and adds its payment_hash to self.settled (the research pseudocode's `sha256(bytes.fromhex(p))` was incorrect — p is a bolt11 string, not hex) — tracking settled hashes by decoding the bolt11 is correct"
  - "TEST-01 uses a bare BackgroundTasks() object passed to get_withdraw_callback — add_task registers _melt_pay but it never runs outside FastAPI's response machinery, so the note stays pending between the two melt calls (exactly the window the duplicate-melt guard protects)"
  - "mint_note helper goes through the real lazy-settlement path (_try_settle_mint) rather than directly inserting a note row — exercises the same code the /w poll walks, so settlement bugs surface in setup too"

patterns-established:
  - "FakeNode tristate fixture pattern: monkeypatch module-level payment imports with controllable paid=True/False/None behaviour — reusable for Phase 7 (full test suite port) and any future test needing a fake Lightning backend"
  - "Per-test DB isolation via drop + re-migrate in an async db_setup fixture (giftcards pattern) — no in-memory SQLite engine swap needed; the ext_lnurlmint file is re-created each test"
  - "Direct endpoint function calls with MagicMock request + bare BackgroundTasks for unit-level endpoint tests — avoids the full LNbits app/auth setup while still exercising the real endpoint logic"

requirements-completed: [TEST-01, TEST-02, TEST-03, TEST-04, TEST-05, SEC-01, SEC-03, SEC-04]

coverage:
  - id: D1
    description: "TEST-01: a note melted twice is rejected — the second melt callback returns {\"status\":\"ERROR\",\"reason\":\"pending\"} because the note is already pending from the first melt (mark_pending raises PendingNoteError as a backstop; the /w/cb endpoint checks note.pending first)"
    requirement: TEST-01
    verification:
      - kind: integration
        ref: "lnbits/extensions/lnurlmint/tests/test_poc_duplicate_melt.py::test_poc_duplicate_melt[asyncio]"
        status: pass
    human_judgment: false
  - id: D2
    description: "TEST-02: compare-and-set settle_mint is atomic — two settle_mint calls for the same payment_hash produce exactly one note (first returns amount, second returns None via rowcount==0)"
    requirement: TEST-02
    verification:
      - kind: integration
        ref: "lnbits/extensions/lnurlmint/tests/test_poc_a2_settle_race.py::test_poc_a2_settle_race[asyncio]"
        status: pass
    human_judgment: false
  - id: D3
    description: "TEST-03: tristate settlement — pay_invoice raising with paid=None (HodlNode ambiguous) leaves the note pending (NOT restored); after hodl settles (paid=True), reconcile finalizes (burn); benign failure (paid=False) restores. SEC-01 confirm-before-burn."
    requirement: TEST-03
    verification:
      - kind: integration
        ref: "lnbits/extensions/lnurlmint/tests/test_melt_restore_double_payout_poc.py::test_melt_restore_double_payout_ambiguous_leaves_pending[asyncio]"
        status: pass
      - kind: integration
        ref: "lnbits/extensions/lnurlmint/tests/test_melt_restore_double_payout_poc.py::test_melt_restore_double_payout_settle_after_hodl[asyncio]"
        status: pass
      - kind: integration
        ref: "lnbits/extensions/lnurlmint/tests/test_melt_restore_double_payout_poc.py::test_melt_restore_double_payout_benign_failed_restores[asyncio]"
        status: pass
    human_judgment: false
  - id: D4
    description: "TEST-04: reconcile skips in-flight melts — while a payment is in-flight (InFlightNode pay_started set, pay_release not set), reconcile runs but SKIPS the in-flight payment_hash (note stays pending); after pay_release, _melt_pay finalizes (spent). SEC-03."
    requirement: TEST-04
    verification:
      - kind: integration
        ref: "lnbits/extensions/lnurlmint/tests/test_poc_reconcile_inflight_race.py::test_poc_reconcile_inflight_race[asyncio]"
        status: pass
    human_judgment: false
  - id: D5
    description: "TEST-05: /w rejects pending notes with {\"status\":\"ERROR\",\"reason\":\"pending\"} — no withdrawRequest fields (callback, minWithdrawable, maxWithdrawable) are returned, so a pending note's value is not leaked. SEC-04."
    requirement: TEST-05
    verification:
      - kind: integration
        ref: "lnbits/extensions/lnurlmint/tests/test_poc_f2_pending_info_leak.py::test_poc_f2_pending_info_leak[asyncio]"
        status: pass
    human_judgment: false
  - id: D6
    description: "FakeNode/HodlNode/InFlightNode fixtures monkeypatch services_module + views_module payment functions with controllable tristate behaviour; PaymentPendingStatus models paid=None (NOT a raise); _CONFIRMATION_RETRY_DELAYS_SECONDS=() for fast tests; per-test DB isolation."
    requirement: SEC-01
    verification:
      - kind: automated
        ref: "grep 'class FakeNode' tests/conftest.py → 1; grep 'class HodlNode' → 1; grep 'class InFlightNode' → 1; grep 'PaymentPendingStatus' → 6; grep 'PaymentFailedStatus' → 4; grep '_CONFIRMATION_RETRY_DELAYS_SECONDS' → 2; grep 'lnbits_create_invoice|lnbits_pay_invoice' → 6"
        status: pass
    human_judgment: false

# Metrics
duration: 18min
completed: 2026-08-28
status: complete
---

# Plan 02-05: Critical PoC Tests Summary

**Five funds-loss security PoCs ported to LNbits fixtures (FakeNode/HodlNode/InFlightNode monkeypatching the async payment-service layer) — all passing and locking the confirm-before-burn tristate, double-melt/double-mint guards, in-flight reconcile safety, and pending-note rejection.**

## Performance

- **Duration:** ~18 min
- **Tasks:** 7
- **Files created:** 7 (tests/__init__.py, conftest.py, 5 test files)
- **Tests:** 7 (5 PoCs; TEST-03 has 3 sub-tests) — all pass in 0.68s, stable across 3 consecutive runs

## Accomplishments
- TEST-01 (duplicate_melt): a note melted twice is rejected — the second melt returns `{"status":"ERROR","reason":"pending"}` because the note is already pending from the first melt.
- TEST-02 (a2_settle_race): compare-and-set `UPDATE ... WHERE minted=0` + `rowcount==1` ensures only one `settle_mint` call materializes a note (first returns amount, second returns None).
- TEST-03 (tristate — highest-risk): `pay_invoice` raising with `paid=None` (HodlNode ambiguous) leaves the note pending, NOT restored; after the hodl settles (`paid=True`), reconcile finalizes (burn); benign failure (`paid=False`) restores. A naive `except PaymentError: restore` would fail the ambiguous test.
- TEST-04 (reconcile_inflight): while a payment is in-flight (InFlightNode), reconcile skips it (`_melt_in_flight`) — the note is not restored while the HTLC is still being sent; after the payment completes, the note is finalized.
- TEST-05 (f2_pending_info_leak): `/w` rejects pending notes with `{"status":"ERROR","reason":"pending"}` — no withdrawRequest fields are returned, preventing the sell-during-melt scam.
- FakeNode/HodlNode/InFlightNode fixtures monkeypatch `services_module` + `views_module` payment imports with controllable tristate behaviour; `PaymentPendingStatus` models `paid=None` (the source raised; LNbits returns `paid=None` — equivalent per RQ12).

## Task Commits

Each task was committed atomically:

1. **Task 1: Create tests/conftest.py — FakeNode/HodlNode/InFlightNode fixtures + in-memory DB setup** - `fc1b518` (feat)
2. **Task 2: Create test_poc_duplicate_melt.py (TEST-01)** - `25fb4e3` (feat)
3. **Task 3: Create test_poc_a2_settle_race.py (TEST-02)** - `5dee9ef` (feat)
4. **Task 4: Create test_melt_restore_double_payout_poc.py (TEST-03 tristate)** - `b022087` (feat)
5. **Task 5: Create test_poc_reconcile_inflight_race.py (TEST-04)** - `7ebfba1` (feat)
6. **Task 6: Create test_poc_f2_pending_info_leak.py (TEST-05)** - `fd14716` (feat)
7. **Task 7: Run all 5 PoC tests together** - (no commit — verification only; 7 passed in 0.68s)

## Files Created/Modified
- `tests/__init__.py` — makes `lnurlmint.tests` a package (importable under pytest's prepend mode)
- `tests/conftest.py` — FakeNode/HodlNode/InFlightNode classes, `node`/`hodl_node`/`inflight_node` fixtures (monkeypatch services + views payment imports), `db_setup` fixture (drop + re-migrate per test, clear in-flight registry), `fake_invoice` + `mint_note` helpers
- `tests/test_poc_duplicate_melt.py` — TEST-01: double-melt rejection via the /w/cb endpoint (bare BackgroundTasks keeps the note pending)
- `tests/test_poc_a2_settle_race.py` — TEST-02: compare-and-set settle_mint atomicity (direct CRUD calls)
- `tests/test_melt_restore_double_payout_poc.py` — TEST-03: tristate settlement (3 sub-tests: ambiguous→pending, settle_after_hodl→finalize, benign_failed→restore) via _melt_pay + reconcile
- `tests/test_poc_reconcile_inflight_race.py` — TEST-04: reconcile skips in-flight melts (asyncio.create_task + pay_started/pay_release coordination)
- `tests/test_poc_f2_pending_info_leak.py` — TEST-05: /w rejects pending notes (direct endpoint call + mark_pending setup)

## Decisions Made
- Tests run via `cd /home/exedev/lnbits && .venv/bin/python -m pytest lnbits/extensions/lnurlmint/tests/` (the lnurlmint repo is symlinked into lnbits/extensions/lnurlmint). The plan's `cd /home/exedev/lnbits && .venv/bin/python -m pytest tests/test_poc_*.py` is a documentation simplification — `tests/` is relative to the extension root, and the venv lives in lnbits (same simplification as Plans 02-01..02-04).
- Async tests use `@pytest.mark.anyio` (anyio plugin, LNbits convention) — `@pytest.mark.asyncio` fails because pytest-asyncio is not installed. Matches the giftcards extension test pattern.
- Test files import helpers via `from lnurlmint.tests.conftest import fake_invoice, mint_note` — a bare `from tests.conftest import ...` collides with lnbits' root `tests/` package; the full package path resolves correctly under pytest's prepend import mode (`lnbits/extensions/` has no `__init__.py`, so `lnurlmint` is importable as top-level).
- FakeNode patches BOTH `services_module` and `views_module` — `views_lnurl.py` imports `lnbits_create_invoice` separately at module level; only patching services would leave the mint callback calling the real LNbits create_invoice.
- InFlightNode.check_transaction_status checks `settled` before returning `PaymentFailedStatus` — so mint-note setup (mint payment_hash in settled) still materializes, while melt hashes (not yet settled) report `paid=False` (lnd 404).
- HodlNode.settle_hodl_payments decodes each pending_hodl bolt11 and adds its payment_hash to `settled` (the research pseudocode's `sha256(bytes.fromhex(p))` was incorrect — `p` is a bolt11 string, not hex).
- TEST-01 uses a bare `BackgroundTasks()` — `add_task` registers `_melt_pay` but it never runs outside FastAPI's response machinery, keeping the note pending between the two melt calls.
- `mint_note` goes through the real lazy-settlement path (`_try_settle_mint`) rather than directly inserting a note row — settlement bugs surface in setup too.

## Deviations from Plan

### Auto-fixed Issues

**1. [Async framework] @pytest.mark.asyncio → @pytest.mark.anyio**
- **Found during:** Task 1 (conftest probe)
- **Issue:** The plan's pseudocode uses `@pytest.mark.asyncio`, but LNbits uses the anyio plugin (not pytest-asyncio) — `@pytest.mark.asyncio` fails with "async def functions are not natively supported".
- **Fix:** Used `@pytest.mark.anyio` throughout (matches the giftcards extension test pattern).
- **Files modified:** all 5 test files + conftest.py
- **Verification:** all 7 tests pass with anyio
- **Committed in:** each task commit

**2. [Import path] `from tests.conftest import ...` → `from lnurlmint.tests.conftest import ...`**
- **Found during:** Task 1 (conftest import check)
- **Issue:** The plan's pseudocode uses `from tests.conftest import fake_invoice`, but a bare `tests` resolves to lnbits' root `tests/` package (which has no `fake_invoice`) — import collision.
- **Fix:** Test files import via the full package path `from lnurlmint.tests.conftest import ...`, which resolves correctly under pytest's prepend import mode.
- **Files modified:** all 5 test files
- **Verification:** `from lnurlmint.tests.conftest import fake_invoice, mint_note, FakeNode` succeeds under pytest
- **Committed in:** each task commit

**3. [Monkeypatch scope] views_lnurl.lnbits_create_invoice also patched**
- **Found during:** Task 1 (conftest probe)
- **Issue:** The plan only mentions monkeypatching `services_module`, but `views_lnurl.py` imports `lnbits_create_invoice` separately at module level (line 24) — the mint callback (`get_pay_callback`) would call the real LNbits create_invoice without patching views.
- **Fix:** `_patch_services` also does `monkeypatch.setattr(views_module, "lnbits_create_invoice", fake.create_invoice)`.
- **Files modified:** tests/conftest.py
- **Verification:** mint_note setup (which uses the real lazy-settlement path) works end-to-end
- **Committed in:** fc1b518 (Task 1)

**4. [HodlNode correctness] settle_hodl_payments decodes bolt11, not sha256(hex)**
- **Found during:** Task 4 (TEST-03 probe)
- **Issue:** The research pseudocode's HodlNode.check_transaction_status computed settled hashes via `sha256(bytes.fromhex(p)) for p in self.paid` — but `self.paid` holds bolt11 strings (not hex), so `bytes.fromhex` would crash.
- **Fix:** HodlNode.settle_hodl_payments decodes each pending_hodl bolt11 via `bolt11.decode(pr).payment_hash` and adds it to `self.settled`; check_transaction_status checks `self.settled` directly.
- **Files modified:** tests/conftest.py
- **Verification:** TEST-03 settle_after_hodl passes (reconcile sees paid=True after settle_hodl_payments)
- **Committed in:** fc1b518 (Task 1)

**5. [InFlightNode correctness] check_transaction_status checks settled before returning Failed**
- **Found during:** Task 5 (TEST-04 probe)
- **Issue:** A first-draft InFlightNode.check_transaction_status returned `PaymentFailedStatus()` unconditionally — but mint_note setup calls `_try_settle_mint` → `check_transaction_status(mint payment_hash)`, which would return paid=False and never materialize the note (mark_pending then fails with "Invalid or already spent k1.").
- **Fix:** InFlightNode.check_transaction_status checks `if payment_hash in self.settled: return PaymentSuccessStatus()` before returning `PaymentFailedStatus()` — the mint payment_hash is in settled (mint_note adds it), so setup materializes; melt hashes (not yet settled) still report paid=False.
- **Files modified:** tests/conftest.py
- **Verification:** TEST-04 passes (note materializes, reconcile skips in-flight, payment finalizes after release)
- **Committed in:** fc1b518 (Task 1)

---

**Total deviations:** 5 auto-fixed (1 async framework, 1 import path, 1 monkeypatch scope, 2 fixture correctness)
**Impact on plan:** All auto-fixes necessary for the tests to run and correctly model the tristate. No scope creep — the tests assert exactly what the plan specifies.

## Issues Encountered
None beyond the auto-fixes above. The tristate contract (the highest-risk detail) worked on the first real run: `paid=None` (HodlNode ambiguous) correctly leaves the note pending, and a naive `except PaymentError: restore` would fail the `test_melt_restore_double_payout_ambiguous_leaves_pending` test (SEC-01 invariant holds). All 7 tests are stable across 3 consecutive runs (no flakiness from the asyncio.Event coordination in TEST-04).

## User Setup Required
None — the tests use a fake Lightning backend (FakeNode) and the extension's file-backed SQLite (dropped + re-migrated per test). No external service configuration required. Run with:
```
cd /home/exedev/lnbits && .venv/bin/python -m pytest lnbits/extensions/lnurlmint/tests/ -v --no-cov
```

## Next Phase Readiness
- Phase 2 is complete: all 5 plans delivered. The mint + melt vertical MVP is fully implemented and locked by the 5 critical PoC tests.
- The FakeNode/HodlNode/InFlightNode fixture pattern is reusable for Phase 7 (full test suite port) and any future test needing a fake Lightning backend with tristate control.
- Phase 3 (rotate/split/merge/sunset) and Phase 4 (comment protection + verify) can proceed in parallel — both depend on the Phase 2 API contract and state machine, which are now stable and tested.
- The tristate settlement contract (paid=None → leave pending) is locked by TEST-03 — any future change that breaks it will fail the test.

---
*Phase: 02-mint-melt-vertical-mvp*
*Completed: 2026-08-28*
