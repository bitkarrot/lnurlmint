---
phase: 02
status: passed
verified_at: 2026-08-28
---

# Phase 02 Verification: Mint + Melt Vertical MVP

**Verdict:** PASSED — all 5 success criteria met, all 28 requirements verified, all 4 code review fixes applied, 8/8 tests passing.

---

## Test Suite Execution

**Command:** `cd /home/exedev/lnbits && .venv/bin/python -m pytest lnbits/extensions/lnurlmint/tests/ -v`

**Result:** 8 passed in 2.63s (exit code 0)

| # | Test | Status |
|---|------|--------|
| 1 | `test_melt_restore_double_payout_ambiguous_leaves_pending[asyncio]` | PASS |
| 2 | `test_melt_restore_double_payout_settle_after_hodl[asyncio]` | PASS |
| 3 | `test_melt_restore_double_payout_benign_failed_restores[asyncio]` | PASS |
| 4 | `test_melt_restore_double_payout_failed_with_htlc_leaves_pending[asyncio]` (W-04) | PASS |
| 5 | `test_poc_a2_settle_race[asyncio]` | PASS |
| 6 | `test_poc_duplicate_melt[asyncio]` | PASS |
| 7 | `test_poc_f2_pending_info_leak[asyncio]` | PASS |
| 8 | `test_poc_reconcile_inflight_race[asyncio]` | PASS |

---

## Success Criteria

### SC-1: Mint flow — LUD-06 payRequest with fee-aware bounds, callback creates invoice, lazy settlement materializes note

**Status:** PASS

**Evidence:**
- `views_lnurl.py:54-97` — `GET /lnurlmint/lnurlp/{mint_id}` returns LUD-06 payRequest with `tag: "payRequest"`, fee-aware `minSendable` (via `_min_sendable_msat` walk), `maxSendable`, `metadata` (description + `text/identifier` + `Mint fees:` entry), `withdrawLink` pointing to `/lnurlmint/w/{mint_id}`, `commentAllowed: 64`.
- `views_lnurl.py:100-151` — `GET /lnurlmint/p/cb/{mint_id}` creates invoice via `lnbits_create_invoice(wallet_id=mint.wallet, ...)`, records pending mint via `record_mint_record` (stores net amount after fee, `minted=0`), returns `{"pr": pr, "disposable": False}`.
- `services.py:124-153` — `_try_settle_mint` checks pending mint record, calls `check_transaction_status`, and on `status.success` calls `settle_mint` (compare-and-set) to materialize the note. Note is NOT materialized at callback time — only on first `/w` poll after settlement.
- `crud.py:252-295` — `settle_mint` uses compare-and-set (`UPDATE mints_records SET minted=1 WHERE minted=0` + `rowcount==1` check) then INSERTs note with `amount_msat` = net amount (amount - mint_fee).

### SC-2: Melt flow — LUD-03 withdraw callback with tristate settlement (paid=True/False/None)

**Status:** PASS

**Evidence:**
- `views_lnurl.py:208-335` — `GET /lnurlmint/w/cb/{mint_id}` validates `pr` via `bolt11.decode`, rejects self-mint/duplicate payment hashes (SEC-06), atomically reserves via `mark_pending`, registers in-flight via `_track_melt_start`, replies `{"status":"OK"}` immediately, schedules background `_melt_pay`.
- `services.py:258-343` — `_melt_pay` implements full tristate: `pay_invoice` → on raise/pending → `_confirm_payment` → `paid=True` finalize (burn), `paid=False` restore, `paid=None` leave pending. Every restore goes through `_confirm_payment` first (SEC-01).
- `services.py:169-206` — `_confirm_payment` uses `status.success`/`status.failed`/`status.paid is None` (NOT `.pending` property which is True for both None and False).
- TEST-03 (3 sub-tests + W-04) confirms: ambiguous (paid=None) → leave pending; settle_after_hodl → finalize; benign_failed → restore; failed_with_htlc → leave pending.

### SC-3: Double-melt rejection — pending state prevents second melt; /w rejects pending notes

**Status:** PASS

**Evidence:**
- `views_lnurl.py:270-271` — melt callback checks `if note.pending: return {"status": "ERROR", "reason": "pending"}` before any state mutation.
- `crud.py:298-333` — `mark_pending` validates ALL notes before updating any; raises `PendingNoteError("pending")` if any note is already pending (backstop).
- `views_lnurl.py:192-193` — informational `/w` endpoint rejects pending notes: `if note.pending: return {"status": "ERROR", "reason": "pending"}` — no withdrawRequest fields returned (no value leaked).
- TEST-01 (`test_poc_duplicate_melt`) confirms second melt returns `{"status":"ERROR","reason":"pending"}`.
- TEST-05 (`test_poc_f2_pending_info_leak`) confirms `/w` rejects pending notes with no withdrawRequest fields.

### SC-4: Reconcile — in-flight melts skipped, stranded notes resolved on tick

**Status:** PASS

**Evidence:**
- `services.py:359-403` — `reconcile_pending_melts` calls `pending_melts()`, skips in-flight melts via `_melt_in_flight(payment_hash)` (SEC-03), resolves `wallet_id` via `mint_id → mints.wallet`, confirms with single-attempt (`delays=()`), finalizes on `paid=True`, restores on `paid=False`, logs+leaves pending on `paid=None` (NEVER auto-restore unconfirmable).
- `services.py:406-418` — `boot_reconcile` one-shot at startup, guarded against exceptions.
- `tasks.py:14-21` — `wait_for_melt_reconcile` wraps `run_interval(60, reconcile_pending_melts)`.
- `__init__.py:50` — `asyncio.create_task(boot_reconcile())` at startup.
- `__init__.py:53` — `create_permanent_unique_task("ext_lnurlmint", wait_for_melt_reconcile)` for periodic reconcile.
- TEST-04 (`test_poc_reconcile_inflight_race`) confirms reconcile skips in-flight melts.

### SC-5: All 5 PoC tests pass against LNbits test fixtures with fake backend returning paid=None

**Status:** PASS

**Evidence:** 8/8 tests passing (5 PoCs + W-04 addition + 3 sub-tests for TEST-03). FakeNode/HodlNode/InFlightNode fixtures monkeypatch `services_module` and `views_module` payment imports with controllable tristate behavior. `PaymentPendingStatus` models `paid=None`. `_CONFIRMATION_RETRY_DELAYS_SECONDS` monkeypatched to `()` for fast tests.

---

## Requirements Verification (28 requirements)

### EXT-03: Background task wired via create_permanent_unique_task

**Status:** PASS

**Evidence:**
- `__init__.py:43` — `from lnbits.tasks import create_permanent_unique_task`
- `__init__.py:53` — `task = create_permanent_unique_task("ext_lnurlmint", wait_for_melt_reconcile)`
- `__init__.py:54` — `scheduled_tasks.append(task)`
- `__init__.py:26-32` — `lnurlmint_stop` cancels scheduled tasks
- `__init__.py:50` — `asyncio.create_task(boot_reconcile())` for boot-time one-shot

### MINT-01: LUD-06 payRequest with fee-aware bounds, callback, metadata, withdrawLink

**Status:** PASS

**Evidence:** `views_lnurl.py:54-97` — returns `tag: "payRequest"`, `callback`, `minSendable` (fee-aware via `_min_sendable_msat`), `maxSendable`, `metadata` (description + `text/identifier` + `Mint fees:` when non-zero), `withdrawLink`, `commentAllowed: 64`. Sunset rejection at lines 67-71.

### MINT-02: Callback creates invoice, records pending mint, returns pr

**Status:** PASS

**Evidence:** `views_lnurl.py:100-151` — creates invoice via `lnbits_create_invoice(wallet_id=mint.wallet, amount=amount//1000, ...)`, records pending mint via `record_mint_record(payment_hash, mint_id, pr, net_amount_msat)`, returns `{"pr": pr, "disposable": False}`.

### MINT-03: Lazy settlement materializes note on settlement, credited with amount - mint_fee

**Status:** PASS

**Evidence:** `services.py:124-153` — `_try_settle_mint` called on first `/w` poll; checks `get_pending_mint_record`, calls `check_transaction_status`, on `status.success` calls `settle_mint`. `crud.py:439-469` — `record_mint_record` stores NET amount (`amount - _mint_fee_msat`). `crud.py:252-295` — `settle_mint` INSERTs note with the stored net amount.

### MINT-04: Rejects amounts below min_sendable, above max_sendable, and net-of-fee < min_mint

**Status:** PASS

**Evidence:** `views_lnurl.py:119-132` — checks `amount < mint.min_sendable_msat` ("Amount too low"), `amount > mint.max_sendable_msat` ("Amount too high"), `net_amount_msat < mint.min_mint_msat` ("Amount too low to mint a note").

### MINT-05: disposable: false in payRequest action response

**Status:** PASS

**Evidence:** `views_lnurl.py:151` — `return {"pr": pr, "disposable": False}`. `models.py:252` — `LnurlPayActionResponse` has `disposable: Literal[False] = False`.

### REDEEM-01: LUD-03 withdrawRequest is purely informational, rejects pending/spent/unknown, echoes k1 verbatim

**Status:** PASS

**Evidence:** `views_lnurl.py:154-205` — `GET /lnurlmint/w/{mint_id}` returns `tag: "withdrawRequest"`, `callback`, `k1` (echoed verbatim), `minWithdrawable = maxWithdrawable = note.amount_msat`. Rejects pending (line 192-193), spent (line 194-195), unknown (line 190-191). Lazily settles via `_try_settle_mint` (line 186). Never calls `mark_pending`/`finalize_melt`/`restore`.

### REDEEM-02: Melt callback — single k1 + pr → mark_pending, {status:OK}, async pay, burn on confirmation

**Status:** PASS

**Evidence:** `views_lnurl.py:208-335` — validates pr, rejects self-mint/duplicate, `mark_pending` atomically, `_track_melt_start`, `record_melt`, `background_tasks.add_task(_melt_pay, ...)`, returns `{"status":"OK"}`. `services.py:258-343` — `_melt_pay` pays invoice, burns on `paid=True`, restores on `paid=False`, leaves pending on `paid=None`.

### REDEEM-06: pr MUST NOT combine with multiple k1s or amount; h required when pr absent

**Status:** PASS

**Evidence:** `views_lnurl.py:240-244` — `if pr is not None and (len(k1) > 1 or amount is not None)` → error "pr cannot be combined with multiple k1s or amount". `views_lnurl.py:248-254` — `if pr is None: if h is None or not HEX32_PATTERN.match(h): return {"status":"ERROR","reason":"missing h"}`; valid h returns "Rotate/split/merge not yet implemented." (Phase 3 deferral).

### SEC-01: No naive "except PaymentError: restore" without _confirm_payment

**Status:** PASS

**Evidence:** Grep for `except.*PaymentError.*restore` in services.py → 0 matches. `_melt_pay` (services.py:258-343) always routes through `_confirm_payment` on raise before any restore. Every restore path (lines 311, 336) is preceded by `completed = await _confirm_payment(...)` and `if not completed:` check. TEST-03 confirms: `paid=None` leaves pending, NOT restored.

### SEC-02: Store-hashes-not-secrets (no preimage column, note IDs are sha256(k1))

**Status:** PASS

**Evidence:** `views_lnurl.py:181` — `note_id = sha256(bytes.fromhex(k1)).hexdigest()`. `views_lnurl.py:261` — same derivation in melt callback. `crud.py:281-284` — `settle_mint` uses `comment_hash if present else payment_hash` as note_id (both hashes, never raw secrets). No DB column named `preimage`, `secret`, or `k1` in any table. Grep for `preimage|secret|raw_k1` in source files: only in comments/docstrings and `PrivateKey().secret.hex()` (coincurve API attribute, not a stored credential).

### SEC-03: In-flight registry (asyncio.Lock, register after mark_pending, clear in finally)

**Status:** PASS

**Evidence:** `services.py:53-54` — `_in_flight_melts: dict[str, int] = {}` + `_in_flight_melts_lock = asyncio.Lock()`. Grep for `threading` → 0 matches. `views_lnurl.py:314` — `_track_melt_start` called AFTER `mark_pending` succeeds (line 305), BEFORE `background_tasks.add_task` (line 328). `services.py:342-343` — `finally: await _track_melt_end(payment_hash)` always clears. `services.py:372` — reconcile skips in-flight via `_melt_in_flight`.

### SEC-04: /w rejects pending notes

**Status:** PASS

**Evidence:** `views_lnurl.py:192-193` — `if note.pending: return {"status": "ERROR", "reason": "pending"}`. No withdrawRequest fields (callback, minWithdrawable, maxWithdrawable) returned. TEST-05 confirms.

### SEC-05: No-secret-logging (no k1/pr/preimage in logs)

**Status:** PASS

**Evidence:** Grep for `logger\..*(k1|pr|preimage)` in all .py files → 0 matches. All 15 logger calls verified — only log `mint_id`, `note_ids`, `payment_hash` (all hashes/ids, not secrets). `request.url` references in views_lnurl.py are in docstrings only (lines 170, 231), not in logger calls.

### SEC-06: Self-mint/duplicate payment hash rejection

**Status:** PASS

**Evidence:** `views_lnurl.py:289-293` — `if decoded.has_payment_hash and await mint_record_exists(decoded.payment_hash)` → "Cannot melt into an invoice this mint issued itself." `views_lnurl.py:297-301` — `if decoded.has_payment_hash and await melt_record_exists(decoded.payment_hash)` → "Invoice already used by an earlier melt". Both checks BEFORE `mark_pending` (no state mutation on rejection). `crud.py:212-235` — `mint_record_exists` and `melt_record_exists` query functions.

### SEC-07: Note mutations scoped by mint_id

**Status:** PASS

**Evidence:** `crud.py:319` — `mark_pending` SELECT includes `AND mint_id = :mid`. `crud.py:331` — `mark_pending` UPDATE includes `AND mint_id = :mid`. `crud.py:352` — `finalize_melt` UPDATE includes `AND mint_id = :mid`. `crud.py:374` — `restore` UPDATE includes `AND mint_id = :mid`. `crud.py:190-191` — `get_note` includes `AND n.mint_id = :mid`. `pending_melts` is intentionally unscoped (system-level reconcile, resolves mint_id via `get_mint_id_for_note`).

### REC-01: _melt_pay tristate settlement

**Status:** PASS

**Evidence:** `services.py:258-343` — full tristate: `pay_invoice` raises OR returns pending → `_confirm_payment` → `paid=True` finalize, `paid=False` restore, `paid=None` leave pending. `_confirm_payment` (services.py:169-206) retries with backoff (default 1,2,4,8,16s; `delays=()` for single-attempt). Uses `status.success`/`status.failed`/`status.paid is None` — NOT `.pending`.

### REC-02: Boot-time reconcile + periodic 60s

**Status:** PASS

**Evidence:** `__init__.py:50` — `asyncio.create_task(boot_reconcile())` at startup. `__init__.py:53` — `create_permanent_unique_task("ext_lnurlmint", wait_for_melt_reconcile)`. `tasks.py:21` — `await run_interval(60, reconcile_pending_melts)()`. `services.py:406-418` — `boot_reconcile` guarded against exceptions.

### REC-03: DB transaction atomicity (async with db.connect())

**Status:** PASS

**Evidence:** `crud.py:265` — `settle_mint` uses `async with db.connect() as conn:`. `crud.py:314` — `mark_pending` uses `async with db.connect() as conn:`. `crud.py:347` — `finalize_melt` uses `async with db.connect() as conn:` (W-02 fix). `crud.py:369` — `restore` uses `async with db.connect() as conn:` (W-02 fix). Compare-and-set pattern: `UPDATE ... WHERE minted=0` + `rowcount==1` in one connection block.

### ECON-01: Mint fee rounds UP to nearest whole sat

**Status:** PASS

**Evidence:** `services.py:57-65` — `_mint_fee_msat` uses `fee_msat = base_fee_msat + (amount_msat * fee_percent_ppm) // 1_000_000` then `-(-fee_msat // 1000) * 1000` (ceil rounding idiom). Never floor rounds.

### ECON-02: Fee-aware minSendable walks up until net >= min_mint_msat

**Status:** PASS

**Evidence:** `services.py:68-86` — `_min_sendable_msat` starts at `max(min_sendable_msat, min_mint_msat)`, walks up by 1000 msat until `amount - _mint_fee_msat(amount, mint) >= min_mint_msat`. 100,000 iteration safety cap.

### ECON-03: maxSendable net of fee advertised correctly

**Status:** PASS

**Evidence:** `services.py:89-96` — `max_mintable_msat(mint) = max_sendable_msat - _mint_fee_msat(max_sendable_msat, mint)`. `views_lnurl.py:93` — payRequest advertises `mint.max_sendable_msat` (gross, what payer pays).

### ECON-04: Melt fee limit is max(round(amount * 0.005), 5000, mint_fee(amount))

**Status:** PASS

**Evidence:** `services.py:99-108` — `_melt_fee_limit_msat` returns `max(round(amount_msat * 0.005), 5000, _mint_fee_msat(amount_msat, mint))`. Documented deviation: LNbits' `pay_invoice` does not accept a per-payment fee limit; formula preserved for accounting/logging.

### TEST-01: test_poc_duplicate_melt.py — double-melt rejected

**Status:** PASS

**Evidence:** `tests/test_poc_duplicate_melt.py` — `test_poc_duplicate_melt[asyncio]` PASSED. Second melt returns `{"status":"ERROR","reason":"pending"}`.

### TEST-02: test_poc_a2_settle_race.py — compare-and-set atomicity

**Status:** PASS

**Evidence:** `tests/test_poc_a2_settle_race.py` — `test_poc_a2_settle_race[asyncio]` PASSED. Two `settle_mint` calls produce exactly one note (first returns amount, second returns None via `rowcount==0`).

### TEST-03: test_melt_restore_double_payout_poc.py — tristate settlement

**Status:** PASS

**Evidence:** `tests/test_melt_restore_double_payout_poc.py` — 4 sub-tests PASSED:
- `test_melt_restore_double_payout_ambiguous_leaves_pending` — paid=None leaves pending, NOT restored
- `test_melt_restore_double_payout_settle_after_hodl` — reconcile finalizes after hodl settles
- `test_melt_restore_double_payout_benign_failed_restores` — paid=False restores
- `test_melt_restore_double_payout_failed_with_htlc_leaves_pending` (W-04) — terminal FAILED raise with live HTLC → paid=None → leave pending

### TEST-04: test_poc_reconcile_inflight_race.py — reconcile skips in-flight

**Status:** PASS

**Evidence:** `tests/test_poc_reconcile_inflight_race.py` — `test_poc_reconcile_inflight_race[asyncio]` PASSED. While payment is in-flight (InFlightNode `pay_started` set, `pay_release` not set), reconcile skips it; after release, `_melt_pay` finalizes.

### TEST-05: test_poc_f2_pending_info_leak.py — /w rejects pending notes

**Status:** PASS

**Evidence:** `tests/test_poc_f2_pending_info_leak.py` — `test_poc_f2_pending_info_leak[asyncio]` PASSED. `/w` returns `{"status":"ERROR","reason":"pending"}` — no withdrawRequest fields leaked.

---

## Code Review Fixes (W-01 through W-04)

### W-01: In-flight registry leak on record_melt/add_task failure

**Status:** PASS — Applied

**Evidence:** `views_lnurl.py:323-332` — `record_melt` and `background_tasks.add_task` wrapped in try/except. On exception, `_track_melt_end(decoded.payment_hash)` is called to release the in-flight registration, then the exception is re-raised. This prevents the note from being stranded if `_melt_pay` is never scheduled.

### W-02: finalize_melt/restore wrapped in db.connect() for atomicity

**Status:** PASS — Applied

**Evidence:** `crud.py:347` — `finalize_melt` uses `async with db.connect() as conn:` with `conn.execute` in loop. `crud.py:369` — `restore` uses `async with db.connect() as conn:` with `conn.execute` in loop. Both docstrings reference W-02. This ensures all-or-nothing semantics for multi-note operations (important for Phase 3 merge/split).

### W-03: _try_settle_mint catches check_transaction_status exceptions

**Status:** PASS — Applied

**Evidence:** `services.py:143-149` — `check_transaction_status` call wrapped in try/except. On exception, logs warning and returns `False` (settlement not confirmed, try again later) instead of propagating a 500 to the `/w` or `/w/cb` endpoint. Preserves LNURL error format invariant.

### W-04: Added test for HodlNode "failed" pay_mode (terminal raise with live HTLC)

**Status:** PASS — Applied

**Evidence:** `tests/test_melt_restore_double_payout_poc.py:120` — `test_melt_restore_double_payout_failed_with_htlc_leaves_pending[asyncio]` PASSED. Tests the most insidious tristate case: a `PaymentError(status="failed")` raise where the HTLC is still live → `check_transaction_status` returns `paid=None` → note left pending (NOT restored). A naive `except PaymentError: restore` would fail this test.

---

## Plan Summaries (5/5 complete)

| Plan | Summary File | Status |
|------|-------------|--------|
| 02-01 | `02-01-SUMMARY.md` | Complete — DB transaction discipline + note CRUD core (13 CRUD functions, 4 LNURL wire models) |
| 02-02 | `02-02-SUMMARY.md` | Complete — Mint flow (LUD-06 payRequest + callback, fee math, lazy settlement) |
| 02-03 | `02-03-SUMMARY.md` | Complete — Informational /w + melt callback (LUD-03, in-flight registry, REDEEM-06 validation) |
| 02-04 | `02-04-SUMMARY.md` | Complete — Confirm-before-burn + in-flight tracking + reconcile (tristate, retry-with-backoff, boot_reconcile, task lifecycle) |
| 02-05 | `02-05-SUMMARY.md` | Complete — Critical PoC tests (5 PoCs, FakeNode/HodlNode/InFlightNode fixtures) |

---

## Additional Verification

### No-secret-logging invariant

Grep for `logger\..*(k1|pr|preimage)` across all source files → 0 matches. All 15 logger calls in `services.py` and `views_lnurl.py` log only `mint_id`, `note_ids`, and `payment_hash` (all hashes/ids, not secrets). `request.url` references in `views_lnurl.py` are in docstrings only (lines 170, 231), not in logger calls.

### status.pending gotcha

Grep for `status\.pending` in `services.py` → 0 matches. `_confirm_payment` uses `status.success`, `status.failed`, and `status.paid is None` directly — never the `.pending` property (which is `self.paid is not True`, True for both `None` and `False`).

### asyncio.Lock (not threading)

Grep for `threading` in `services.py` → 0 matches. `_in_flight_melts_lock = asyncio.Lock()` at line 54. All registry access is async/await under the lock.

### Cross-wallet isolation

All note mutations (`get_note`, `mark_pending`, `finalize_melt`, `restore`) include `AND mint_id = :mid` in WHERE clauses. `pending_melts` is intentionally unscoped (system-level reconcile); wallet resolution deferred to `get_mint_id_for_note`.

---

## Summary

| Category | Count | Status |
|----------|-------|--------|
| Success Criteria | 5/5 | PASS |
| Requirements | 28/28 | PASS |
| Code Review Fixes | 4/4 | PASS |
| Plan Summaries | 5/5 | PASS |
| Tests | 8/8 | PASS |

**Phase 2 is COMPLETE.** All success criteria met, all 28 requirements verified, all 4 code review fixes applied and confirmed in source, all 5 plan summaries present, and the full test suite passes (8 tests in 2.63s). The confirm-before-burn tristate state machine, in-flight melt tracking, background reconciliation, store-hashes-not-secrets discipline, and fee math protocol contracts are all correctly implemented and locked by the PoC tests.
