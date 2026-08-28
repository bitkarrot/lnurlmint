---
phase: 03-rotate-split-merge-sunset
plan: 02
subsystem: redeem
tags: [lnbits, sqlite, async, swap, split, lnurl, fee-arithmetic, h2-validation, conservation]

# Dependency graph
requires:
  - phase: 03-rotate-split-merge-sunset
    provides: "Plan 03-01: crud.swap (atomic burn N + mint M with validate-then-burn-then-mint and two-table collision check), services.sign_note stub, /w/cb rotate + merge branches, _MAX_K1S=100, shared k1 resolution loop pattern"
provides:
  - "views_lnurl.py /w/cb split branch: one/many k1 + amount + h + h2 → burn all, mint two notes (amount keyed by h, change keyed by h2) with fee arithmetic (change = total - amount - base_fee, reject change < 1)"
  - "views_lnurl.py /w/cb h2 validation: h2 required when amount is present, validated against HEX32_PATTERN; missing/invalid h2 returns {\"status\":\"ERROR\",\"reason\":\"missing h2\"}"
  - "Shared k1 resolution loop moved before split/rotate/merge branching point — both branches reuse the same note resolution + lazy settlement + pending/spent checks"
affects: [03-rotate-split-merge-sunset, 05-offline-verification]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Split fee arithmetic: change = total - amount - base_fee_msat; reject change_before_fee < base_fee (negative change) and change_amount < 1 (zero-value note) — the split costs exactly one base_fee from the change side, preventing fee dodging via dust splits"
    - "h2 validation in the pr-absent block: h2 required when amount is present (split), validated against HEX32_PATTERN — completes the callback's input validation (REDEEM-06)"
    - "Shared k1 resolution loop: the note resolution (k1 → note_id + value with lazy settlement, pending/spent checks) is extracted before the split/rotate/merge branching point so both branches reuse it — avoids code duplication between split and rotate/merge"

key-files:
  created: []
  modified:
    - lnurlmint/views_lnurl.py

key-decisions:
  - "Split branch placed BEFORE rotate/merge branch (if amount is not None → split; else → rotate/merge) — the amount parameter distinguishes the two branches"
  - "Shared k1 resolution loop moved before the split/rotate/merge branching point — Plan 01 had it inside the rotate/merge branch; Plan 02 extracts it so both branches can use it without duplication"
  - "base_fee taken from the change side (not the amount) — prevents fee dodging via repeated dust splits; a holder can't avoid the fee by splitting into many small notes and melting each separately"
  - "change_amount < 1 rejection (not < 0) — a change of exactly 0 is a zero-value note which is never valid; a change of 1 msat (dust) IS allowed"
  - "Temporary 'Split not available.' guard removed — replaced by the full split branch with h2 validation and two-note mint arithmetic"
  - "sign_note called for both h and h2 (stub returns None) — Phase 5 implements real signing and captures the return values as sig/sig2"

patterns-established:
  - "Split fee arithmetic: change = total - amount - base_fee_msat with two rejection guards (change_before_fee < base_fee, change_amount < 1) — the conservation guard that prevents zero-value notes and ensures exactly one base_fee per split"
  - "Shared k1 resolution: the note resolution loop is extracted before the branching point so split and rotate/merge both reuse it — establishes the pattern for any future burn-N-mint-M callback branch"

requirements-completed: [REDEEM-04, REDEEM-06, REDEEM-07]

coverage:
  - id: D1
    description: "The /w/cb callback handles split: one/many k1 + amount + h + h2 → swap(note_ids, [h, h2], [amount, change_amount], mint_id), returns {\"status\":\"OK\"}"
    requirement: REDEEM-04
    verification:
      - kind: automated
        ref: "grep 'await swap.*h.*h2' views_lnurl.py → 1 match (split swap call with two mint notes); grep 'change_amount' views_lnurl.py → 4 matches (computation, check, swap arg, sign_note arg)"
        status: pass
    human_judgment: false
  - id: D2
    description: "h2 is required when amount is present; missing/invalid h2 returns {\"status\":\"ERROR\",\"reason\":\"missing h2\"}"
    requirement: REDEEM-06
    verification:
      - kind: automated
        ref: "grep 'missing h2' views_lnurl.py → 1 match in the 'if pr is None:' h2 validation block; h2 validated against HEX32_PATTERN"
        status: pass
    human_judgment: false
  - id: D3
    description: "amount must satisfy 0 < amount < total_msat; otherwise rejected with {\"status\":\"ERROR\",\"reason\":\"amount must be between 0 and {total_msat} msat.\"}"
    requirement: REDEEM-04
    verification:
      - kind: automated
        ref: "grep 'amount must be between' views_lnurl.py → 1 match in the split branch"
        status: pass
    human_judgment: false
  - id: D4
    description: "change_before_fee = total_msat - amount; rejected if < base_fee_msat with 'insufficient value'; change_amount = change_before_fee - base_fee_msat; rejected if < 1 with 'insufficient value'"
    requirement: REDEEM-04
    verification:
      - kind: automated
        ref: "grep 'insufficient value' views_lnurl.py → 2 matches (change_before_fee < base_fee and change_amount < 1)"
        status: pass
    human_judgment: false
  - id: D5
    description: "sign_note is called for both h and h2 (stub returns None; Phase 5 adds real signatures)"
    requirement: REDEEM-07
    verification:
      - kind: automated
        ref: "grep 'await sign_note.*change' views_lnurl.py → 1 match (h2 sign_note call); grep 'await sign_note' views_lnurl.py → 3 matches (rotate/merge + split h + split h2)"
        status: pass
    human_judgment: false
  - id: D6
    description: "Failed split (any rejection) changes nothing — no notes burned, no notes minted (atomicity via swap; all rejections return before swap is called)"
    requirement: REDEEM-04
    verification:
      - kind: automated
        ref: "All rejection paths (amount bounds, insufficient value x2) return before the swap call; swap's validate-then-burn-then-mint ensures atomicity if swap itself fails"
        status: pass
    human_judgment: false
  - id: D7
    description: "No logger call includes k1, h, h2, amount, or any query string (SEC-05)"
    requirement: REDEEM-07
    verification:
      - kind: automated
        ref: "grep 'logger.debug' views_lnurl.py → 4 matches, all log only mint_id (scheduled melt, split, rotate/merge, recorded pending mint); no k1/h/h2/amount in any log call"
        status: pass
    human_judgment: false
  - id: D8
    description: "Temporary 'Split not available.' guard removed — replaced by full split branch"
    requirement: REDEEM-04
    verification:
      - kind: automated
        ref: "grep 'Split not available' views_lnurl.py → no matches (exit 1); grep 'Split not yet implemented' views_lnurl.py → no matches (exit 1)"
        status: pass
    human_judgment: false

# Metrics
duration: 8min
completed: 2026-08-28
status: complete
---

# Plan 03-02: Split Summary

**Split callback branch with fee arithmetic (change = total - amount - base_fee, reject change < 1) and h2 validation — completing the three redeem operations alongside rotate/merge from Plan 01.**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-08-28T22:06Z
- **Completed:** 2026-08-28T22:14Z
- **Tasks:** 1
- **Files modified:** 1
- **Tests:** 8 existing Phase 2 tests still pass (no regressions)

## Accomplishments
- `/w/cb` split branch — one/many `k1` + `amount` + `h` + `h2` → resolve all k1 (shared resolution loop with lazy settlement + pending/spent checks), validate amount bounds (`0 < amount < total_msat`), compute `change = total - amount - base_fee` with two rejection guards (`change_before_fee < base_fee` → "insufficient value", `change_amount < 1` → "insufficient value"), `swap(note_ids, [h, h2], [amount, change_amount], mint_id)`, `sign_note` for both h and h2, return `{"status":"OK"}`.
- `h2` validation added to the `if pr is None:` block — when `amount` is present, `h2` must be present and match `HEX32_PATTERN`; otherwise `{"status":"ERROR","reason":"missing h2"}`.
- Shared k1 resolution loop extracted before the split/rotate/merge branching point — both branches reuse the same note resolution (lazy settlement, pending check, spent check) without duplication.
- Temporary "Split not available." guard removed — replaced by the full split branch.
- All 8 Phase 2 PoC tests still pass (no regressions from the callback restructuring).

## Task Commits

Each task was committed atomically:

1. **Task 1: Add h2 validation and split branch to /w/cb callback** - `8422cff` (feat)

## Files Created/Modified
- `lnurlmint/views_lnurl.py` - Added h2 validation to the `if pr is None:` block (required when amount is present, validated against HEX32_PATTERN). Moved the shared k1 resolution loop before the split/rotate/merge branching point. Replaced the temporary "Split not available." guard with the full split branch: amount bounds check, two-stage fee arithmetic (change_before_fee < base_fee → "insufficient value", change_amount < 1 → "insufficient value"), `swap(note_ids, [h, h2], [amount, change_amount], mint_id)`, `sign_note` for both h and h2, return `{"status":"OK"}`. Updated docstring to document the split branch.

## Decisions Made
- Split branch placed BEFORE rotate/merge branch (`if amount is not None:` → split; else → rotate/merge) — the amount parameter distinguishes the two branches.
- Shared k1 resolution loop moved before the branching point — Plan 01 had it inside the rotate/merge branch; Plan 02 extracts it so both branches reuse it without duplication.
- base_fee taken from the change side (not the amount) — prevents fee dodging via repeated dust splits; a holder can't avoid the fee by splitting into many small notes and melting each separately.
- `change_amount < 1` rejection (not `< 0`) — a change of exactly 0 is a zero-value note which is never valid; a change of 1 msat (dust) IS allowed.
- Temporary "Split not available." guard removed — replaced by the full split branch with h2 validation and two-note mint arithmetic.
- `sign_note` called for both h and h2 (stub returns None) — Phase 5 implements real signing and captures the return values as sig/sig2.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None — the split branch implemented cleanly on the first attempt. The shared k1 resolution loop extraction, h2 validation, fee arithmetic, and swap call all pass their acceptance criteria. All 8 Phase 2 PoC tests still pass after the callback restructuring.

## User Setup Required
None - no external service configuration required. The split branch operates on the existing ext_lnurlmint.sqlite3 database (migrations from Phase 1 already ran).

## Next Phase Readiness
- The split branch is ready for Plan 03 (sunset gating + collision griefing + fee conservation PoCs) — sunset gating will add a `mint.sunset_mint` check to the split branch (reject split when sunsetting).
- The `sign_note` stub is called for both h and h2 — Phase 5 (Offline Verification) implements real recoverable ECDSA signing and captures the return values as sig/sig2 in the response.
- The shared k1 resolution loop pattern is established for any future burn-N-mint-M callback branch.
- All three redeem operations (rotate, split, merge) are now implemented — the full redeem lifecycle is complete pending sunset gating and PoC tests.

## Self-Check: PASSED

All acceptance criteria from Task 1 verified:
- `grep "change_amount" views_lnurl.py` → 4 matches (computation, check, swap arg, sign_note arg) ✓
- `grep "missing h2" views_lnurl.py` → 1 match in the h2 validation block ✓
- `grep "insufficient value" views_lnurl.py` → 2 matches (change_before_fee < base_fee and change_amount < 1) ✓
- `grep "amount must be between" views_lnurl.py` → 1 match in the split branch ✓
- `grep "await swap.*h.*h2" views_lnurl.py` → 1 match (split swap call with two mint notes) ✓
- `grep "await sign_note.*change" views_lnurl.py` → 1 match (h2 sign_note call) ✓
- `grep "Split not yet implemented" views_lnurl.py` → no matches (exit 1) ✓
- `grep "Split not available" views_lnurl.py` → no matches (exit 1) ✓
- `from lnurlmint.views_lnurl import get_withdraw_callback` → ok ✓
- All 8 Phase 2 tests pass (no regressions) ✓

Plan-level verification:
1. Split branch: one/many k1 + amount + h + h2 → burn all, mint two notes ✓
2. h2 required when amount present, HEX32_PATTERN validated ✓
3. Fee arithmetic: change = total - amount - base_fee, reject change < 1 ✓
4. amount bounds: 0 < amount < total_msat ✓
5. sign_note called for both h and h2 (stub returns None) ✓
6. Shared k1 resolution loop before branching point ✓
7. Temporary "Split not available." guard removed ✓
8. No secret in logs (SEC-05) ✓
9. All queries scoped by mint_id (SEC-07) ✓
10. No Phase 2 regressions (8 tests pass) ✓

---
*Phase: 03-rotate-split-merge-sunset*
*Completed: 2026-08-28*
