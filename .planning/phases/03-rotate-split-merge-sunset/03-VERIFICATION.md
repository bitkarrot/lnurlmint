---
phase: 03
status: passed
verified_at: 2026-08-28
---

# Phase 3 Verification: Rotate + Split + Merge + Sunset

## VERIFICATION PASSED

All 4 success criteria met, all 7 requirements implemented and verified in code,
all 3 plan SUMMARY files present, code review clean (6 Info-level notes only),
and the full test suite passes (29 tests: 8 Phase 2 + 21 Phase 3, 0 failures).

---

## Success Criteria

### SC-1: Rotate + merge — swap primitive works, callback branches implemented — PASS

- `crud.swap` (crud.py:494-571) atomically burns N notes and mints M notes in
  one `async with db.connect() as conn:` block using a strict
  validate-then-burn-then-mint structure (dedup → validation → burn → mint).
  All burn validations and collision checks complete before any mutation.
- `/w/cb` rotate/merge branch (views_lnurl.py:432-449): resolves all k1 →
  note_ids + values via the shared resolution loop (lazy settlement + pending/
  spent checks), computes `refund = (len(note_ids) - 1) * mint.base_fee_msat`
  and `merged_amount = total_msat + refund`, calls
  `swap(note_ids, [h], [merged_amount], mint_id)`, calls `sign_note` (stub),
  returns `{"status": "OK"}`. Rotate is merge with n=1 (refund=0, value-neutral).
- Verified by `test_poc_fee_conservation.py::test_simple_cycles` (cycle A:
  mint→rotate→melt; cycle B: mint→split→merge→melt) and
  `test_poc_a1_collision_griefing.py::test_legitimate_ids_still_pass_the_guard`
  (rotate + merge with fresh WALLET-generated h).

### SC-2: Split — fee arithmetic correct (change = total - amount - base_fee, reject < 1) — PASS

- `/w/cb` split branch (views_lnurl.py:402-430): `amount` bounds check
  (`0 < amount < total_msat`), `change_before_fee = total_msat - amount`,
  reject if `change_before_fee < mint.base_fee_msat` ("insufficient value"),
  `change_amount = change_before_fee - mint.base_fee_msat`, reject if
  `change_amount < 1` ("insufficient value" — zero-value note prevention),
  `swap(note_ids, [h, h2], [amount, change_amount], mint_id)`, `sign_note`
  for both h and h2 (stub), return `{"status": "OK"}`.
- base_fee taken from the change side (not the amount) — prevents fee dodging
  via repeated dust splits.
- Verified by `test_poc_fee_conservation.py::test_dust_split_edges` (change of
  exactly 1 msat allowed; change of 0 rejected; failed split changes nothing)
  and `test_hundred_note_merge_is_not_a_base_fee_printing_press` (99 dust
  splits + 100-note merge = net zero, exactly one mint fee kept).

### SC-3: Sunset — /p/cb and /w/cb split reject when sunset_mint=True; rotate/merge/melt unaffected — PASS

- `/lnurlp` (views_lnurl.py:75-79): rejects with "This mint is sunsetting -
  minting is disabled." when `mint.sunset_mint`.
- `/p/cb` (views_lnurl.py:122-126): same rejection when `mint.sunset_mint`.
- `/w/cb` split (views_lnurl.py:276-280): rejects with "This mint is sunsetting
  - splitting is disabled." when `mint.sunset_mint and amount is not None`.
  Placed after pr-combination rejection and max_k1s check, before h/h2
  validation — so a sunsetting mint rejects split even if h/h2 are missing.
- Rotate (amount is None), merge (amount is None), and melt (pr is not None)
  are NOT rejected — none increase outstanding liability (ECON-05).
- Sunset gating pattern verified by grep: "sunsetting - splitting" → 1 match
  in /w/cb; "sunsetting - minting" → 2 matches (/lnurlp + /p/cb, unchanged
  from Phase 2).

### SC-4: Fee conservation + collision-griefing PoC tests pass (21 new tests) — PASS

- `test_poc_fee_conservation.py` (9 tests): white-box `Ledger` drives real
  endpoint functions, tracks `paid_in == outstanding + melted_out + fees -
  refunds` after every operation, asserts `attacker_gain <= 0`. Covers simple
  cycles, dust edges, hundred-note merge printing press, fee arithmetic grid,
  zero-value mint, sub-sat base fee rounding, failed-request atomicity,
  oversized merge, operator fee raise overrefund.
- `test_poc_fee_loop.py` (6 tests): `CreateMint` bounds validation
  (fee_percent_ppm `le=100_000`, base_fee_msat `ge=0`, sendable bounds
  ordered), `_min_sendable_msat` walk terminates under worst legal config,
  100K iteration cap converts pathological config to RuntimeError.
- `test_poc_a1_collision_griefing.py` (6 tests): `swap` collision-checks both
  `mints_records` AND `notes`; rotate/split/merge squat on a pending mint's
  payment_hash rejected with generic "Invalid or already spent k1."; attacker's
  note NOT burned (atomic); victim mint materializes; settled-mint squat also
  rejected; legitimate fresh h/h2 pass with no false positives.
- All 21 pass (verified: `29 passed in 21.62s`, stable).

---

## Requirements

### REDEEM-03: Rotate (burn old, mint new same value) — PASS

- views_lnurl.py:437-449: `refund = (len(note_ids) - 1) * mint.base_fee_msat`
  gives 0 for n=1 (rotate), `merged_amount = total_msat + 0 = total_msat`.
  New note keyed by `h` has exactly the same value as the old note.
- `swap([note_id], [h], [merged_amount], mint_id)` burns the old note and
  mints the new one atomically.
- Verified by `test_poc_fee_conservation.py::test_simple_cycles` cycle A
  (mint→rotate→melt, attacker_gain == -1000 = one mint fee) and
  `Ledger.rotate` asserting `new_note.amount_msat == old`.

### REDEEM-04: Split (burn all, mint two with fee arithmetic) — PASS

- views_lnurl.py:402-430: `swap(note_ids, [h, h2], [amount, change_amount],
  mint_id)` burns all input notes and mints two: `amount` keyed by `h`,
  `change_amount = total - amount - base_fee` keyed by `h2`.
- h2 required when amount is present (views_lnurl.py:287-288), validated
  against `HEX32_PATTERN`.
- Verified by `test_poc_fee_conservation.py::test_dust_split_edges` and
  `Ledger.split` asserting `amount_note.amount_msat == amount_msat` and
  `change_note.amount_msat == change`.

### REDEEM-05: Merge (burn all, mint one with refund) — PASS

- views_lnurl.py:437-449: `refund = (n-1) * mint.base_fee_msat`,
  `merged_amount = sum(values) + refund`. Merging n notes refunds every base
  fee collected beyond the single one the now-one note should have cost.
- Verified by `test_poc_fee_conservation.py::test_hundred_note_merge_is_not_
  a_base_fee_printing_press` (99 splits collect 99 base fees, 100-note merge
  refunds 99 base fees — net zero, exactly one mint fee kept) and
  `Ledger.merge` asserting `merged_note.amount_msat == sum(values) + refund`.

### REDEEM-07: No secret in callback response (just {status:OK}) — PASS

- views_lnurl.py:430 (split) and 449 (rotate/merge) return `{"status": "OK"}`
  with no secret, no k1, no h/h2, no amount in the response body.
- `sign_note` stub (services.py:434-444) returns `None`; the return value is
  discarded — Phase 3 responses carry no `sig`/`sig2` (deferred to Phase 5).
- The informational `/w` endpoint (views_lnurl.py:162-213) accepts an `amount`
  query param but ignores it — `maxWithdrawable` is authoritative (line 211).
- No logger call includes k1, h, h2, amount, or pr — all `logger.debug` calls
  (views_lnurl.py:158, 373, 429, 448) log only `mint_id` (SEC-05).

### ECON-05: Sunset mode gating — PASS

- `/p/cb` (views_lnurl.py:122-126) and `/w/cb` split branch
  (views_lnurl.py:276-280) reject when `sunset_mint=True`.
- Rotate (amount is None), merge (amount is None), and melt (pr is not None)
  are unaffected — none increase outstanding liability.
- `/lnurlp` (views_lnurl.py:75-79) also rejects (advertisement suppressed).

### TEST-06: Fee conservation + fee loop PoCs — PASS

- `test_poc_fee_conservation.py` (9 tests) + `test_poc_fee_loop.py` (6 tests)
  = 15 tests, all passing.
- Fee rounding is UP (`_mint_fee_msat`: `-(-fee_msat // 1000) * 1000`),
  fee-aware bounds are correct, no fee-loop or short-a-sat. The conservation
  identity `paid_in == outstanding + melted_out + fees - refunds` holds after
  every operation; `attacker_gain <= 0` after every attack cycle.

### TEST-08: Collision griefing PoC — PASS

- `test_poc_a1_collision_griefing.py` (6 tests: rotate squat, parametrized
  split_h/split_h2/merge squat, settled-mint squat, legitimate ids), all
  passing.
- `swap` collision-checks both `mints_records` (crud.py:540-546) AND `notes`
  (crud.py:549-554) inside the atomic block, before any INSERT. Squat
  rejected with generic "Invalid or already spent k1." (no info leak);
  attacker's note NOT burned (atomic rollback via validate-then-burn-then-
  mint); victim mint materializes normally.

---

## Code Review

**Status: clean** (03-REVIEW.md) — No Critical or Warning findings. 6
Info-level notes only:

1. INFO-01: `swap()` does not validate `len(mint_note_ids) == len(mint_amounts)`
   (defensive gap; current callers always pass correctly-sized lists).
2. INFO-02: `swap()` does not validate mint_amounts are non-negative (defensive
   gap; current callers ensure positive amounts).
3. INFO-03: Partial commit risk on DB error during burn/mint phase (LNbits'
   `conn.execute` commits per call, no automatic rollback; acknowledged in
   research, same risk as Phase 2's `mark_pending`/`finalize_melt`).
4. INFO-04: Unhandled DB integrity error on cross-process INSERT collision
   (astronomically unlikely, same risk as Phase 2).
5. INFO-05: Melt branch does not explicitly check `note.spent` (Phase 2 code;
   minor inconsistency, not a security gap).
6. INFO-06: Operator fee raise overrefund documented but not mitigated (NOT
   attacker-reachable; operator footgun, conservation identity still holds).

No fixes required — all 8 key security invariants verified by the review.

---

## Plan Summaries

All 3 plans have SUMMARY.md files with complete frontmatter, coverage tables,
and self-checks:

- `03-01-SUMMARY.md` — Rotate + merge (swap primitive, sign_note stub,
  rotate/merge callback branches, _MAX_K1S). 3 tasks, 3 commits. Requirements
  completed: REDEEM-03, REDEEM-05, REDEEM-07.
- `03-02-SUMMARY.md` — Split (split callback branch, h2 validation, fee
  arithmetic, shared k1 resolution loop). 1 task, 1 commit. Requirements
  completed: REDEEM-04, REDEEM-06, REDEEM-07.
- `03-03-SUMMARY.md` — Sunset mode + collision griefing + fee conservation
  PoCs (sunset split gating, fresh_secret helper, 3 PoC test suites). 5 tasks,
  5 commits. Requirements completed: ECON-05, TEST-06, TEST-08.

---

## Test Run

**Command:** `cd /home/exedev/lnbits && .venv/bin/python -m pytest lnbits/extensions/lnurlmint/tests/ -v`

**Result:** `29 passed in 21.62s` (exit code 0)

**Breakdown:**

| File | Tests | Phase |
|------|-------|-------|
| test_melt_restore_double_payout_poc.py | 4 | 2 |
| test_poc_a2_settle_race.py | 1 | 2 |
| test_poc_duplicate_melt.py | 1 | 2 |
| test_poc_f2_pending_info_leak.py | 1 | 2 |
| test_poc_reconcile_inflight_race.py | 1 | 2 |
| **Phase 2 subtotal** | **8** | |
| test_poc_fee_conservation.py | 9 | 3 |
| test_poc_fee_loop.py | 6 | 3 |
| test_poc_a1_collision_griefing.py | 6 | 3 |
| **Phase 3 subtotal** | **21** | |
| **Total** | **29** | |

---

## Notes

- **REQUIREMENTS.md checkboxes:** The 7 Phase 3 requirements (REDEEM-03, 04,
  05, 07; ECON-05; TEST-06, 08) are still marked `[ ]` (unchecked) and their
  traceability status is still "pending" in REQUIREMENTS.md, despite being
  fully implemented, tested, and verified. This is a documentation-tracking
  gap, not a functional gap — the plan SUMMARY files correctly list each
  requirement as completed. **Recommendation:** update REQUIREMENTS.md
  checkboxes to `[x]` and traceability status to "Complete" for these 7
  requirements. (Info-level — does not affect Phase 3 completion.)
- **sign_note stub:** Returns `None` (services.py:434-444). Phase 5 implements
  real recoverable ECDSA signing with per-mint keypair (coincurve, Option B).
  The stub is called for all rotate/split/merge paths but the return value is
  discarded — Phase 3 responses carry `{"status":"OK"}` without sig/sig2, per
  the CONTEXT.md decision to defer signing to Phase 5.
- **Operator fee raise overrefund (INFO-06):** Merge refunds use the CURRENT
  `base_fee_msat`, not the historical fee. NOT attacker-reachable (operator
  controls the fee change); conservation identity still holds (overrefund
  absorbed by mint treasury). Documented in
  `test_operator_fee_raise_overrefunds`. No code change needed.

---

*Verified: 2026-08-28*
