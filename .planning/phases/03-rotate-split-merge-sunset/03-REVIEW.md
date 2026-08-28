---
phase: 03
status: clean
depth: standard
reviewed_at: 2026-08-28
---

# Phase 3 Review: Rotate + Split + Merge + Sunset

## Summary

Reviewed the `swap()` primitive, rotate/split/merge callback branches, sunset
gating, `sign_note` stub, and 3 PoC test suites (21 tests, all passing). The
code is well-structured and all eight key security invariants are upheld. No
Critical or Warning findings — all issues are Info-level defensive gaps or
known, acknowledged limitations.

## Verified Invariants

1. **swap atomicity** — `swap()` (crud.py:515-571) uses a single
   `async with db.connect() as conn:` block with a strict
   validate-then-burn-then-mint structure. All burn validations (not spent,
   not pending) and all collision checks (mints_records + notes) complete
   before ANY mutation. No partial writes on validation failure.

2. **Collision check** — crud.py:537-554 checks both
   `mints_records.payment_hash` (pending AND settled mint invoices — no
   `minted` filter) AND `notes.id` inside the atomic block, before the burn
   phase. Prevents the A1 pending-mint squat attack (TEST-08). The generic
   "Invalid or already spent k1." error reveals no info about which table
   collided.

3. **Fee arithmetic** — Split (views_lnurl.py:415-420): `change_before_fee =
   total - amount`, reject if `< base_fee`; `change_amount = change_before_fee
   - base_fee`, reject if `< 1`. Merge (views_lnurl.py:437-438): `refund =
   (n-1) * base_fee`, `merged_amount = total + refund`. Both match the source
   exactly. Rotate is merge with n=1 (refund=0, value-neutral).

4. **Sunset gating** — `/p/cb` rejects when `sunset_mint=True`
   (views_lnurl.py:122-126). `/w/cb` split branch rejects when
   `sunset_mint and amount is not None` (views_lnurl.py:276-280). Rotate,
   merge, and melt are unaffected (amount is None for rotate/merge; pr is
   present for melt). `/lnurlp` also rejects (views_lnurl.py:75-79).

5. **No-secret-logging** — All `logger.debug` calls in views_lnurl.py
   (lines 158, 373, 429, 448) include only `mint_id`. No k1, h, h2, amount,
   or pr in any log statement. Confirmed across all Phase 3 code paths.

6. **Cross-wallet isolation** — `swap()` burn validation, burn UPDATE, and
   mint INSERT are all scoped by `mint_id` (crud.py:530, 560, 569). The
   collision checks on mints_records and notes are intentionally global
   (note ids and payment hashes are globally unique sha256 outputs) — this
   is more conservative, not a gap.

7. **Duplicate k1 handling** — crud.py:521-524 deduplicates both `burn_ids`
   and `mint_note_ids` before validation. Duplicate burn_ids → ValueError.
   h == h2 in split → ValueError. Both tested in
   `test_failed_requests_change_no_value`.

8. **h/h2 validation** — views_lnurl.py:284-288: h required when pr absent,
   h2 required when amount present, both matched against HEX32_PATTERN
   (`^[0-9a-fA-F]{64}$`). Validated before entering any branch.

## Findings

### Info

---

#### INFO-01: swap() does not validate len(mint_note_ids) == len(mint_amounts)

**File:** crud.py:565
**Description:** The mint phase uses `zip(mint_note_ids, mint_amounts)` which
silently truncates to the shorter list if lengths differ. A mismatch would
burn notes without minting the expected number of new notes (value loss).
Current callers (views_lnurl.py:422, 441) always pass correctly-sized lists,
so this is not a live bug — it is a defensive gap should a future caller
misuse the primitive.
**Suggested fix:** Add an assertion or explicit check at the top of `swap()`:
```python
if len(mint_note_ids) != len(mint_amounts):
    raise ValueError("Invalid or already spent k1.")
```

---

#### INFO-02: swap() does not validate mint_amounts are non-negative

**File:** crud.py:565-570
**Description:** The mint INSERT stores `amount_msat` as-is. A negative amount
would create a negative-value note. Current callers ensure positive amounts
(split: `amount` is checked `0 < amount < total`, `change_amount >= 1`;
merge: `merged_amount = total + refund` where both are `>= 0`), so this is
not a live bug — it is a defensive gap.
**Suggested fix:** Add a check in the validation phase:
```python
for amt in mint_amounts:
    if amt < 0:
        raise ValueError("Invalid or already spent k1.")
```

---

#### INFO-03: Partial commit risk on DB error during burn/mint phase

**File:** crud.py:556-570
**Description:** LNbits' `conn.execute` commits per call — there is no
automatic rollback if a later statement fails. The validate-then-burn-then-mint
structure ensures no partial state on validation failure (nothing mutated
yet). However, if a DB-level error occurs during the burn phase (after some
burns committed) or mint phase (after all burns committed), earlier writes
are already committed and not rolled back. This could leave notes burned
without new notes minted (holder value loss). This only occurs on DB-level
errors (disk full, connection lost) — not logic errors, since all validation
completes before any mutation. This is explicitly acknowledged in the research
(RQ1 gotcha #3) and is the same risk accepted in Phase 2's `mark_pending` /
`finalize_melt`.
**Suggested fix:** No action required for Phase 3. If LNbits' Database
abstraction later exposes `conn.rollback()`, wrapping the burn+mint phases in
a try/except that rolls back on error would close this gap. Documenting the
risk in the `swap()` docstring (already partially done) is sufficient.

---

#### INFO-04: Unhandled DB integrity error on cross-process INSERT collision

**File:** crud.py:565-570, views_lnurl.py:421-426, 440-445
**Description:** The collision check (crud.py:537-554) and the INSERT
(crud.py:566-570) are serialized within a single process by the
`async with db.connect()` lock. Across processes (multiple LNbits workers),
a TOCTOU race is theoretically possible: another process could INSERT a note
with the same id between the collision check and this INSERT. The PRIMARY KEY
constraint on `notes.id` prevents data corruption (the INSERT fails), but the
resulting `IntegrityError` is not caught by the `ValueError` handler in the
callback, resulting in an HTTP 500 instead of an LNURL-formatted error. The
burned notes would already be committed (value loss). This is astronomically
unlikely (requires two processes generating the same 32-byte random hash) and
is the same risk accepted in Phase 2.
**Suggested fix:** Optionally catch `Exception` (or the DB-specific integrity
error) in the callback's swap call and return an LNURL error:
```python
except Exception:
    return {"status": "ERROR", "reason": "Invalid or already spent k1."}
```
This would not prevent the value loss (burns already committed) but would
give the holder a clean error instead of a 500. Low priority given the
probability.

---

#### INFO-05: Melt branch does not explicitly check note.spent (Phase 2 code)

**File:** views_lnurl.py:298-306
**Description:** The melt branch (Phase 2 code) checks `note.pending` but not
`note.spent` — it relies on `mark_pending`'s `WHERE spent = 0` clause to reject
spent notes. The split/merge branch (Phase 3, views_lnurl.py:395-396) checks
`note.spent` explicitly. This is a minor inconsistency in error messaging
(the melt branch gives "Invalid or already spent k1." from mark_pending vs
the explicit check in split/merge), not a security gap — both paths reject
spent notes.
**Suggested fix:** For consistency, add `if note.spent: return {"status":
"ERROR", "reason": "Invalid or already spent k1."}` after the pending check
in the melt branch. Optional — the current behavior is correct.

---

#### INFO-06: Operator fee raise overrefund documented but not mitigated

**File:** views_lnurl.py:437, tests/test_poc_fee_conservation.py:487-508
**Description:** Merge refunds use the CURRENT `base_fee_msat`, not the
historical fee each input note paid at mint time. If an operator raises
`base_fee_msat` while notes are outstanding, merges of pre-raise notes refund
more than was collected — the mint pays out from its own treasury. This is
NOT attacker-reachable (the operator controls the fee change) and is correctly
documented as an operator footgun in
`test_operator_fee_raise_overrefunds`. The conservation identity
(`paid_in == outstanding + melted_out + fees - refunds`) still holds — the
overrefund is absorbed by the mint's treasury, not created from thin air.
**Suggested fix:** No code change needed. Consider a documentation note for
operators: "Raising base_fee_msat while notes are outstanding will cause
merges to over-refund. Lower or raise fees only when outstanding note count
is low."

---

## Test Assessment

All 21 tests pass (verified: `21 passed in 12.31s`).

**test_poc_fee_conservation.py (8 tests):** Excellent white-box accounting via
the `Ledger` class. Verifies the conservation identity
`paid_in == outstanding + melted_out + fees - refunds` after every operation.
Covers simple cycles, dust edges, hundred-note merge (the lead suspect),
fee arithmetic grid, zero-value mint, sub-sat rounding, failed requests
(atomicity), oversized merge, and operator fee raise. The
`assert_no_attacker_gain()` invariant (`outstanding + melted_out - paid_in
<= 0`) is checked at the end of each attack test.

**test_poc_fee_loop.py (6 tests):** Correctly tests pydantic Field bounds
(ppm <= 100_000, non-negative fees, min <= max) and the defensive iteration
cap in `_min_sendable_msat`. The cap test bypasses pydantic via `update_mint`
to verify the RuntimeError fires on a pathological config.

**test_poc_a1_collision_griefing.py (7 tests with parametrize):** Thoroughly
covers the A1 squat attack across all three swap paths (rotate h, split h,
split h2, merge h), the settled-mint variant, and a no-false-positives test
for legitimate ids. Atomicity is verified (attacker's note not burned on
rejected squat, victim's mint materializes normally).

**conftest.py:** `fresh_secret()` helper correctly returns `(k1, h)` where
k1 is `urandom(32).hex()` and h is `sha256(k1)`. `mint_note()` helper
intentionally bypasses fee logic (records raw amount) to simplify collision
test arithmetic — this is appropriate for the collision tests, while the fee
conservation tests use `ledger.mint()` which goes through the real `/p/cb`
endpoint with fees applied.

## REVIEW COMPLETE

**Status: clean** — No Critical or Warning findings. 6 Info-level findings
(4 defensive coding gaps, 1 minor inconsistency with Phase 2 code, 1
documented operator footgun). All 8 key security invariants verified. 21/21
tests passing. The swap primitive, callback branches, sunset gating, and
sign_note stub are correct and ready for Phase 4.
