---
phase: 02-mint-melt-vertical-mvp
plan: 04
subsystem: payments
tags: [lnbits, tristate-settlement, confirm-before-burn, in-flight-registry, asyncio-lock, background-reconcile, retry-with-backoff, create_permanent_unique_task, run_interval, no-secret-logging]

# Dependency graph
requires:
  - phase: 02-mint-melt-vertical-mvp
    provides: "Note state-machine CRUD (finalize_melt, restore, pending_melts, mark_melt_settled, get_mint_id_for_note, get_mint_by_id) (Plan 02-01); services.py (_CONFIRMATION_RETRY_DELAYS_SECONDS, check_transaction_status, _try_settle_mint) (Plan 02-02); in-flight registry (_in_flight_melts, _track_melt_start/_track_melt_end/_melt_in_flight, _melt_pay stub) + melt callback scheduling _melt_pay (Plan 02-03)"
provides:
  - "Full _melt_pay tristate settlement: pay_invoice → on raise/pending _confirm_payment → paid=True finalize, paid=False restore, paid=None leave pending (SEC-01, REC-01)"
  - "_confirm_payment retry-with-backoff: check_transaction_status with delays (1,2,4,8,16) default, delays=() single-attempt; uses status.success/status.failed/status.paid is None (NOT .pending)"
  - "reconcile_pending_melts: skips in-flight melts (SEC-03), resolves wallet_id via mint_id→mints.wallet, single-attempt confirm, logs+leaves pending for unconfirmable (REC-02)"
  - "boot_reconcile: one-shot reconcile at startup, guarded against exceptions (REC-02)"
  - "tasks.py: wait_for_melt_reconcile wrapping run_interval(60, reconcile_pending_melts)"
  - "lnurlmint_start/stop wiring: boot_reconcile via asyncio.create_task + periodic via create_permanent_unique_task (EXT-03)"
affects: [02-mint-melt-vertical-mvp, 03-rotate-split-merge-sunset, 04-comment-protection-verify]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Tristate settlement contract: pay_invoice raises OR returns pending → _confirm_payment → paid=True/False/None → finalize/restore/leave-pending. NEVER restore on a raise alone (SEC-01)."
    - "Tristate detection via status.success/status.failed/status.paid is None — NOT the .pending property (which is self.paid is not True, True for BOTH None AND False)"
    - "Retry-with-backoff with configurable delays: default (1,2,4,8,16) for _melt_pay, delays=() for single-attempt reconcile"
    - "finally: _track_melt_end ALWAYS clears the in-flight registry — cleared even on crash/exception (SEC-03)"
    - "Reconcile skip-in-flight pattern: _melt_in_flight(payment_hash) → continue before any check_payment_status (SEC-03, TEST-04)"
    - "Boot-time one-shot reconcile before periodic task: asyncio.create_task(boot_reconcile()) in lnurlmint_start (REC-02)"
    - "Task lifecycle via create_permanent_unique_task + run_interval (giftcards pattern, EXT-03)"

key-files:
  created:
    - lnurlmint/tasks.py
  modified:
    - lnurlmint/services.py
    - lnurlmint/__init__.py

key-decisions:
  - "Task execution order adjusted to dependency order: Task 2 (_confirm_payment) → Task 1 (_melt_pay) → Task 3 (reconcile) → Task 4 (wiring) — _melt_pay calls _confirm_payment, so _confirm_payment must exist first for the module to be self-consistent at each commit"
  - "Rephrased comments to avoid the literal string 'status.pending' (used '.pending property' instead) to satisfy the grep 'status.pending returns no matches' acceptance criterion — the invariant (never use .pending for tristate) is identical and documented in the comment"
  - "_melt_pay's finally block calls _track_melt_end(payment_hash) unconditionally (no has_payment_hash guard) — matches the plan exactly; if has_payment_hash is False, payment_hash is None and _track_melt_end(None) is a harmless no-op (pops a non-existent key). In practice the melt callback only schedules _melt_pay for valid bolt11 invoices that always have a payment_hash."
  - "boot_reconcile is NOT added to scheduled_tasks (it's a one-shot that completes quickly; if still running at shutdown, it's cancelled when the event loop closes) — matches the plan"
  - "lnurlmint_start uses local imports (inside the function) for create_permanent_unique_task, boot_reconcile, wait_for_melt_reconcile — avoids circular import at module load time and matches the giftcards pattern"

patterns-established:
  - "Tristate settlement pattern: pay_invoice → _confirm_payment → paid=True finalize / paid=False restore / paid=None leave pending. Every restore goes through _confirm_payment first (SEC-01)."
  - "Configurable retry delays pattern: delays=None defaults to _CONFIRMATION_RETRY_DELAYS_SECONDS; delays=() for single-attempt (reconcile). (0, *delays) iterates with an immediate first attempt."
  - "Reconcile pattern: pending_melts() → skip in-flight → resolve wallet_id → single-attempt confirm → finalize/restore/leave-pending. Never auto-restore unconfirmable."
  - "Boot reconcile pattern: asyncio.create_task(boot_reconcile()) before create_permanent_unique_task for periodic — resolves stranded notes immediately on startup."

requirements-completed: [EXT-03, SEC-01, SEC-03, SEC-05, REC-01, REC-02]

coverage:
  - id: D1
    description: "_melt_pay implements tristate settlement: pay_invoice raises → _confirm_payment → paid=True finalize, paid=False restore, paid=None leave pending (SEC-01, REC-01). Also handles pay_invoice returning a pending Payment (not raising)."
    requirement: SEC-01
    verification:
      - kind: integration
        ref: "from lnbits.extensions.lnurlmint.services import _melt_pay; grep 'lnbits_pay_invoice' services.py → 2; grep '_confirm_payment' services.py → 9; grep 'finally' services.py → 5; grep '_track_melt_end' services.py → 3; no 'except PaymentError: restore' without _confirm_payment (SEC-01)"
        status: pass
      - kind: automated
        ref: "grep -n 'except.*PaymentError.*restore' services.py | grep -v '_confirm_payment' → empty (SEC-01 invariant holds)"
        status: pass
    human_judgment: false
  - id: D2
    description: "_confirm_payment retries check_transaction_status with backoff (default 1,2,4,8,16s; delays=() single-attempt). Uses status.success/status.failed/status.paid is None — NOT the .pending property (which is True for both None and False)."
    requirement: REC-01
    verification:
      - kind: automated
        ref: "from lnbits.extensions.lnurlmint.services import _confirm_payment; grep 'status.success' services.py → 3; grep 'status.failed' services.py → 3; grep 'status.paid is None' services.py → 3; grep 'status.pending' services.py → 0; grep '_CONFIRMATION_RETRY_DELAYS_SECONDS' services.py → 3"
        status: pass
    human_judgment: false
  - id: D3
    description: "reconcile_pending_melts skips in-flight melts (_melt_in_flight, SEC-03), resolves wallet_id via mint_id→mints.wallet, confirms with delays=() single-attempt, finalizes on paid=True, restores on paid=False, logs+leaves pending on paid=None (NEVER auto-restore unconfirmable). boot_reconcile is a guarded one-shot at startup."
    requirement: REC-02
    verification:
      - kind: integration
        ref: "from lnbits.extensions.lnurlmint.services import reconcile_pending_melts, boot_reconcile; grep '_melt_in_flight' services.py → 3; grep 'delays=()' services.py → 4; grep 'left pending' services.py → 7; grep 'get_mint_id_for_note' services.py → 2"
        status: pass
    human_judgment: false
  - id: D4
    description: "In-flight registry cleared in finally: _track_melt_end ALWAYS called (SEC-03). asyncio.Lock (not threading). Reconcile skips in-flight melts."
    requirement: SEC-03
    verification:
      - kind: automated
        ref: "grep 'asyncio.Lock' services.py → 5; grep 'threading' services.py → 0; grep 'finally' services.py → 5; grep '_track_melt_end' services.py → 3; grep '_melt_in_flight' services.py → 3"
        status: pass
    human_judgment: false
  - id: D5
    description: "Task lifecycle: tasks.py defines wait_for_melt_reconcile (run_interval(60, reconcile_pending_melts)). lnurlmint_start schedules boot_reconcile + create_permanent_unique_task. lnurlmint_stop cancels scheduled_tasks (EXT-03)."
    requirement: EXT-03
    verification:
      - kind: automated
        ref: "from lnbits.extensions.lnurlmint.tasks import wait_for_melt_reconcile; from lnbits.extensions.lnurlmint import lnurlmint_start, lnurlmint_stop; grep 'run_interval' tasks.py → 4; grep 'create_permanent_unique_task' __init__.py → 3; grep 'boot_reconcile' __init__.py → 2; grep 'asyncio.create_task' __init__.py → 1; grep 'scheduled_tasks.append' __init__.py → 1; grep 'Phase 2 will add' __init__.py → 0"
        status: pass
    human_judgment: false
  - id: D6
    description: "No-secret-logging on /w, /w/cb, and reconcile paths: no logger call includes k1, pr, preimage, or query strings (SEC-05)."
    requirement: SEC-05
    verification:
      - kind: automated
        ref: "grep -n 'logger\\.' services.py | grep -iE '(\\bpr\\b|k1|preimage)' → empty; grep -n 'logger\\.' views_lnurl.py | grep -iE '(\\bpr\\b|k1|preimage)' → empty"
        status: pass
    human_judgment: false

# Metrics
duration: 16min
completed: 2026-08-28
status: complete
---

# Plan 02-04: Confirm-before-burn + In-flight Tracking + Reconcile Summary

**Full _melt_pay tristate settlement (pay_invoice → _confirm_payment → paid=True finalize / paid=False restore / paid=None leave pending), retry-with-backoff confirmation using status.success/.failed/.paid-is-None (NOT .pending), background reconcile that skips in-flight melts, and task lifecycle wiring via create_permanent_unique_task — the highest-risk plan in Phase 2.**

## Performance

- **Duration:** ~16 min
- **Tasks:** 4
- **Files created:** 1 (tasks.py)
- **Files modified:** 2 (services.py, __init__.py)

## Accomplishments
- _melt_pay full tristate settlement: pay_invoice → on success finalize+mark_settled; on PaymentError or pending return → _confirm_payment → paid=True finalize, paid=False restore, paid=None leave pending. Every restore path goes through _confirm_payment first (SEC-01 — no naive except:restore). finally block always clears the in-flight registry (SEC-03).
- _confirm_payment retry-with-backoff: retries check_transaction_status with delays (1,2,4,8,16) default, delays=() for single-attempt. Uses status.success/status.failed/status.paid is None directly — NEVER the .pending property (which is self.paid is not True, True for BOTH None AND False — the single most critical tristate gotcha, RQ7 #1).
- reconcile_pending_melts: skips in-flight melts (_melt_in_flight, SEC-03), resolves wallet_id via mint_id→mints.wallet, single-attempt confirm (delays=()), finalizes on paid=True, restores on paid=False, logs+leaves pending on paid=None (NEVER auto-restore unconfirmable — would risk double-spend if HTLC is actually in flight).
- boot_reconcile: one-shot reconcile at startup, guarded against exceptions so a failure never blocks startup (REC-02).
- tasks.py: wait_for_melt_reconcile wrapping run_interval(60, reconcile_pending_melts).
- lnurlmint_start: schedules boot_reconcile via asyncio.create_task + periodic reconcile via create_permanent_unique_task("ext_lnurlmint", wait_for_melt_reconcile) appended to scheduled_tasks (EXT-03). Phase 1 stub comment removed.
- No logger call on /w, /w/cb, or reconcile paths includes k1, pr, preimage, or query strings (SEC-05).

## Task Commits

Each task was committed atomically (dependency-ordered: Task 2 → Task 1 → Task 3 → Task 4):

1. **Task 2: Implement _confirm_payment with retry-with-backoff in services.py** - `353b813` (feat)
2. **Task 1: Implement full _melt_pay with tristate settlement in services.py** - `3506dcf` (feat)
3. **Task 3: Implement reconcile_pending_melts and boot_reconcile in services.py** - `e3ee67d` (feat)
4. **Task 4: Create tasks.py and wire lnurlmint_start/lnurlmint_stop in __init__.py** - `83fbe82` (feat)

## Files Created/Modified
- `lnurlmint/tasks.py` (created) — wait_for_melt_reconcile wrapping run_interval(60, reconcile_pending_melts); imports run_interval from lnbits.tasks and reconcile_pending_melts from .services
- `lnurlmint/services.py` (modified) — added _confirm_payment (retry-with-backoff tristate detection), replaced _melt_pay stub with full tristate settlement, added reconcile_pending_melts + boot_reconcile; extended imports (lnbits_pay_invoice, PaymentError, PaymentState, Optional, crud helpers: finalize_melt, restore, mark_melt_settled, pending_melts, get_mint_by_id, get_mint_id_for_note)
- `lnurlmint/__init__.py` (modified) — lnurlmint_start now schedules boot_reconcile (asyncio.create_task) + periodic reconcile (create_permanent_unique_task, scheduled_tasks.append); Phase 1 stub comment removed

## Decisions Made
- Task execution order adjusted to dependency order: Task 2 (_confirm_payment) → Task 1 (_melt_pay) → Task 3 (reconcile) → Task 4 (wiring) — _melt_pay calls _confirm_payment, so _confirm_payment must exist first for each commit to be self-consistent
- Rephrased comments to avoid the literal string "status.pending" (used ".pending property" instead) to satisfy the grep "status.pending returns no matches" acceptance criterion — the invariant (never use .pending for tristate) is identical and documented in the comment
- _melt_pay's finally block calls _track_melt_end(payment_hash) unconditionally (no has_payment_hash guard) — matches the plan exactly; if has_payment_hash is False, payment_hash is None and _track_melt_end(None) is a harmless no-op. In practice the melt callback only schedules _melt_pay for valid bolt11 invoices that always have a payment_hash.
- boot_reconcile is NOT added to scheduled_tasks (one-shot that completes quickly; if still running at shutdown, cancelled when event loop closes) — matches the plan
- lnurlmint_start uses local imports (inside the function) for create_permanent_unique_task, boot_reconcile, wait_for_melt_reconcile — avoids circular import at module load time and matches the giftcards pattern

## Deviations from Plan

### Execution Order Adjustment

**Task ordering changed from plan's 1→2→3→4 to 2→1→3→4.**
- **Reason:** Task 1 (_melt_pay) calls _confirm_payment which is defined in Task 2. Executing in plan order would commit a _melt_pay that references a function not yet committed. Dependency-ordered execution ensures each commit is self-consistent.
- **Impact:** None — all 4 tasks complete with identical content; only the commit order changed.

### Auto-fixed Issues

**1. [Rule 1 - Grep] `status.pending` literal in comments**
- **Found during:** Task 2 (_confirm_payment verification)
- **Issue:** Plan's acceptance criterion `grep "status.pending" services.py returns NO matches` failed because the comments explaining the tristate gotcha contained the literal string "status.pending" (e.g. "do NOT use status.pending") — even though they document the invariant (NOT using it)
- **Fix:** Rephrased comments from "status.pending" to ".pending property" (identical invariant, no literal "status.pending" string)
- **Files modified:** lnurlmint/services.py
- **Verification:** `grep -c "status.pending" services.py` → 0
- **Committed in:** 353b813 (Task 2 commit)

**2. [Documentation] Bare `lnurlmint` import path in acceptance criteria**
- **Found during:** Task 2 verification
- **Issue:** Plan's acceptance criteria use `from lnurlmint.services import ...` but LNbits loads extensions as `lnbits.extensions.lnurlmint` (bare import fails with ModuleNotFoundError) — same documentation simplification as Plans 01-01 through 02-03
- **Fix:** Verified with `from lnbits.extensions.lnurlmint.services import ...` (the actual loader path); no code change needed
- **Files modified:** none
- **Verification:** `from lnbits.extensions.lnurlmint.services import _melt_pay, _confirm_payment, reconcile_pending_melts, boot_reconcile` succeeds
- **Committed in:** N/A (documentation simplification in plan, no code impact)

---

**Total deviations:** 1 execution order adjustment + 2 auto-fixed (1 grep-wording fix, 1 documentation simplification)
**Impact on plan:** All acceptance criteria met. No scope creep. The tristate settlement, SEC-01 invariant, and no-secret-logging invariants are all correctly implemented and verified.

## Issues Encountered
None — all functions importable on the first attempt. The tristate gotcha (status.pending is True for both None and False) was handled correctly by using status.success/status.failed/status.paid is None directly, as documented in the plan and research. The only fix needed was rephrasing comments to avoid the literal "status.pending" grep match.

## User Setup Required
None — no external service configuration required. The background tasks use LNbits' create_permanent_unique_task + run_interval which work with any configured funding source. _melt_pay uses LNbits' pay_invoice; _confirm_payment uses check_transaction_status. Reconcile runs at boot + every 60 seconds automatically once the extension starts.

## Next Phase Readiness
- The confirm-before-burn state machine is complete — _melt_pay pays the invoice and settles the note based on the tristate outcome, with every restore path going through _confirm_payment first (SEC-01)
- The in-flight registry is cleared in _melt_pay's finally block (SEC-03) and reconcile skips in-flight melts — TEST-04 (reconcile_inflight_race) foundation is in place
- _confirm_payment's tristate detection (status.success/.failed/.paid is None, NOT .pending) is the TEST-03 (melt_restore_double_payout) foundation — Plan 02-05 will exercise it with a fake backend returning paid=None
- The task lifecycle (boot_reconcile + periodic reconcile) is wired — Plan 02-05's TEST-04 and TEST-05 will verify reconcile behavior end-to-end
- Plan 02-05 (critical PoC tests) can now port all 5 PoC tests against LNbits fixtures: TEST-01 (duplicate_melt), TEST-02 (a2_settle_race), TEST-03 (melt_restore_double_payout — tristate), TEST-04 (reconcile_inflight_race), TEST-05 (f2_pending_info_leak)

## Self-Check: PASSED

All acceptance criteria from all 4 tasks verified:
- Task 2: _confirm_payment importable; status.success (3), status.failed (3), status.paid is None (3), status.pending (0), _CONFIRMATION_RETRY_DELAYS_SECONDS (3)
- Task 1: _melt_pay importable; lnbits_pay_invoice (2), PaymentError (3), _confirm_payment (9), finally (5), _track_melt_end (3); no "except PaymentError: restore" without _confirm_payment (SEC-01)
- Task 3: reconcile_pending_melts + boot_reconcile importable; _melt_in_flight (3), delays=() (4), left pending (7), get_mint_id_for_note (2)
- Task 4: wait_for_melt_reconcile importable; run_interval in tasks.py (4); create_permanent_unique_task in __init__.py (3), boot_reconcile (2), asyncio.create_task (1), scheduled_tasks.append (1), Phase 2 will add (0)

Plan-level verification:
1. Tristate settlement: _melt_pay → _confirm_payment → paid=True/False/None → finalize/restore/leave-pending ✓
2. No naive restore: every restore preceded by _confirm_payment (SEC-01) ✓
3. _confirm_payment: status.success/.failed/.paid is None (NOT .pending) ✓
4. In-flight registry: finally _track_melt_end always clears (SEC-03); asyncio.Lock (not threading) ✓
5. Reconcile: skips in-flight, delays=() single-attempt, logs+leaves pending for unconfirmable (REC-02) ✓
6. Task lifecycle: boot_reconcile + create_permanent_unique_task (EXT-03) ✓
7. No-secret-logging: no k1/pr/preimage in logger calls (SEC-05) ✓

---
*Phase: 02-mint-melt-vertical-mvp*
*Completed: 2026-08-28*
