---
phase: 02
status: fixes_applied
depth: standard
reviewed_at: 2026-08-28
---

# Phase 02 Code Review — Mint + Melt Vertical MVP

Reviewed files: `crud.py`, `models.py`, `services.py`, `views_lnurl.py`, `tasks.py`, `__init__.py`, `tests/conftest.py`, `tests/test_poc_duplicate_melt.py`, `tests/test_poc_a2_settle_race.py`, `tests/test_melt_restore_double_payout_poc.py`, `tests/test_poc_reconcile_inflight_race.py`, `tests/test_poc_f2_pending_info_leak.py`.

## Security Invariant Verification

| # | Invariant | Status | Notes |
|---|-----------|--------|-------|
| 1 | Tristate settlement (paid=True/False/None via _confirm_payment) | PASS | `_melt_pay` (services.py:248-331) always routes through `_confirm_payment` on raise. No naive "except PaymentError: restore". |
| 2 | status.pending gotcha (use success/failed/paid is None) | PASS | `_confirm_payment` (services.py:178-191) uses `status.success`, `status.failed`, `status.paid is None` — never `status.pending`. |
| 3 | In-flight registry (asyncio.Lock, register after mark_pending, clear in finally, skip in reconcile) | PASS | `asyncio.Lock` (services.py:54), register in callback (views_lnurl.py:313), clear in finally (services.py:333), skip in reconcile (services.py:362). |
| 4 | Store-hashes-not-secrets (no preimage/secret/k1 column or in logs) | PASS | No k1/pr/preimage/request.url in any logger call. Only payment_hash, note_ids, mint_id (all hashes/ids). |
| 5 | Cross-wallet isolation (all note queries scoped by mint_id) | PASS | `get_note`, `mark_pending`, `finalize_melt`, `restore` all include `AND mint_id = :mid`. `pending_melts` is intentionally unscoped (system-level reconcile). |
| 6 | DB transaction atomicity (settle_mint and mark_pending use db.connect()) | PASS | Both use `async with db.connect() as conn:` (crud.py:265, 314). |
| 7 | Fee math (ceil-round mint fee, max(0.5%, 5000, mint_fee) melt limit) | PASS | `_mint_fee_msat` uses `-(-fee_msat // 1000) * 1000` ceil idiom (services.py:65). `_melt_fee_limit_msat` uses `max(round(amount * 0.005), 5000, mint_fee)` (services.py:108). |
| 8 | No-secret-logging | PASS | All 15 logger calls verified — none include k1, pr, preimage, or request.url. |
| 9 | LNURL error format ({"status":"ERROR","reason":"..."} with HTTP 200) | PASS | All endpoint errors return plain dicts (FastAPI serializes as JSON 200). |
| 10 | Background task lifecycle (boot_reconcile + periodic, cancel in stop) | PASS | boot_reconcile via `asyncio.create_task` (one-shot), periodic via `create_permanent_unique_task`, cancel in `lnurlmint_stop` (__init__.py:26-53). |

---

## Findings

### Warning

#### W-01: In-flight registry leak on record_melt or add_task failure
**File:** `views_lnurl.py`, lines 310-322

**Description:** After `_track_melt_start` registers the melt as in-flight, the code calls `record_melt` and `background_tasks.add_task` without a try/except guard. If either raises (DB error during INSERT, or an unexpected exception from FastAPI's task scheduling), the exception propagates, `_melt_pay` is never scheduled, and its `finally:` block (which calls `_track_melt_end`) never runs. The in-flight registry entry leaks permanently for that payment_hash.

The consequence: the note is already `pending=1` (mark_pending committed), but reconcile skips it forever because `_melt_in_flight` returns True. The note is stranded — neither spendable nor meltable — until the process restarts (which clears the in-process registry). After restart, reconcile would resolve it (restore on paid=False, leave pending on paid=None), but until then the user's funds are locked.

The source explicitly handles this case with a try/except (RQ6, source router.py:728-734):
```python
try:
    if decoded.has_payment_hash:
        notes.record_melt(decoded.payment_hash, pr)
    background_tasks.add_task(_melt_pay, note_ids, pr, decoded, funding_source)
except Exception:
    _track_melt_end(decoded.payment_hash)  # never scheduled — drop registration
    raise
```

The port omits this error handling.

**Likelihood:** Low — `record_melt` uses `INSERT OR IGNORE` (very unlikely to fail), and `background_tasks.add_task` is a list append. But the consequence is severe (funds locked until restart) and the fix is trivial.

**Suggested fix:** Wrap `record_melt` and `add_task` in a try/except that clears the in-flight registration on failure:
```python
if decoded.has_payment_hash:
    await _track_melt_start(decoded.payment_hash)

try:
    if decoded.has_payment_hash:
        await record_melt(
            decoded.payment_hash, pr, mint.id, note_id, total_msat
        )
    background_tasks.add_task(_melt_pay, [note_id], pr, decoded, mint)
except Exception:
    if decoded.has_payment_hash:
        await _track_melt_end(decoded.payment_hash)
    raise
```

---

#### W-02: finalize_melt and restore lack db.connect() atomicity for multi-note operations
**File:** `crud.py`, lines 336-366

**Description:** `finalize_melt` and `restore` use individual `await db.execute(...)` calls in a loop, each of which commits independently (LNbits' `conn.execute` commits per call). The source uses a single `with self._lock, self.conn:` transaction for these, ensuring all-or-nothing semantics. If an UPDATE fails mid-loop (e.g., DB error on the second note), the first note is already burned/restored but the second is not — partial state.

For Phase 2 this is a non-issue because the melt callback only ever passes a single note_id (`[note_id]`). But Phase 3 (merge/split) will pass multiple note_ids, and the partial-write risk becomes real. The `mark_pending` function correctly uses `async with db.connect() as conn:` for its validate-then-update pattern; `finalize_melt` and `restore` should follow the same pattern.

**Suggested fix:** Wrap the loop in `async with db.connect() as conn:` and use `conn.execute` instead of `db.execute`:
```python
async def finalize_melt(note_ids: list[str], mint_id: str) -> None:
    async with db.connect() as conn:
        for note_id in note_ids:
            await conn.execute(
                "UPDATE lnurlmint.notes "
                "SET spent = 1, pending = 0, pending_payment_hash = NULL "
                "WHERE id = :id AND mint_id = :mid",
                {"id": note_id, "mid": mint_id},
            )
```
Same for `restore`. This also serializes the updates under the connection lock.

---

#### W-03: _try_settle_mint does not catch check_transaction_status exceptions
**File:** `services.py`, lines 124-143

**Description:** `_try_settle_mint` calls `check_transaction_status(mint.wallet, note_id)` without a try/except. If the funding source is unreachable (connection error, timeout), the exception propagates up to the `/w` or `/w/cb` endpoint, which also has no catch. FastAPI returns a raw 500 Internal Server Error — not the LNURL-mandated `{"status":"ERROR","reason":"..."}` with HTTP 200.

This is not a funds-loss issue (the note simply isn't materialized yet; the user can retry), but it violates the LNURL error format invariant and exposes a stack trace to the caller in debug mode.

**Suggested fix:** Wrap the `check_transaction_status` call in a try/except, returning False on error:
```python
try:
    status = await check_transaction_status(mint.wallet, note_id)
except Exception:
    logger.warning(f"settle_mint: check_transaction_status failed for {note_id}")
    return False
```
Alternatively, catch at the endpoint level and return an LNURL-formatted error.

---

#### W-04: Missing test for HodlNode "failed" pay_mode (terminal raise with live HTLC)
**File:** `tests/test_melt_restore_double_payout_poc.py`

**Description:** The test file's docstring mentions "Three scenarios" but only tests `ambiguous` (paid=None → leave pending), `settle_after_hodl` (reality catches up → finalize), and `benign_failed` (no HTLC → paid=False → restore). The `HodlNode` supports `pay_mode = "failed"` (terminal FAILED raise, but HTLC stays live → check_transaction_status returns paid=None), which is the most insidious tristate case: a raise that looks like a definitive failure but the payment is actually still in flight. A naive `except PaymentError: restore` would restore the note and enable a double-spend.

This case is not tested. The `ambiguous` test covers the "raise + paid=None" path, but `failed` exercises a different `PaymentError.status` ("failed" vs "pending") and a different code path through `HodlNode.pay_invoice`. Adding it would strengthen the tristate contract coverage.

**Suggested fix:** Add a test:
```python
@pytest.mark.anyio
async def test_melt_restore_double_payout_failed_with_htlc_leaves_pending(hodl_node, db_setup):
    """A terminal FAILED raise with a live HTLC → paid=None → leave pending."""
    k1, note_id, mint = await mint_note(hodl_node, VALUE)
    hodl_node.pay_mode = "failed"

    pr = fake_invoice(VALUE, "ee" * 32)
    decoded = bolt11.decode(pr)
    await _start_melt(note_id, pr, decoded, mint)
    await _melt_pay([note_id], pr, decoded, mint)

    note = await get_note(note_id, mint.id)
    assert note.pending is True, "failed with live HTLC must leave pending (not restore)"
    assert note.spent is False
```

---

### Info

#### I-01: /w/cb does not explicitly check note.spent before proceeding to melt
**File:** `views_lnurl.py`, lines 269-270

The `/w` endpoint checks both `note.pending` and `note.spent` explicitly (lines 191-194). The `/w/cb` endpoint only checks `note.pending` (line 269-270) and relies on `mark_pending`'s `WHERE spent = 0` clause as a backstop. This is safe (the backstop works — `mark_pending` raises `ValueError("Invalid or already spent k1.")` for spent notes), but the inconsistency means a spent note reaches the invoice validation and self-mint/duplicate-melt checks before being rejected. Adding an explicit `if note.spent:` check before the bolt11 decode would fail faster and produce a more specific error message.

---

#### I-02: _melt_fee_limit_msat and max_mintable_msat are dead code
**File:** `services.py`, lines 89-108

`_melt_fee_limit_msat` is defined but never called — LNbits' `pay_invoice` does not accept a per-payment fee limit (documented deviation, RQ10 Gotcha #2). `max_mintable_msat` is also never called — the payRequest advertises `mint.max_sendable_msat` (gross), not the net max. Both are preserved as protocol contracts per the research but are currently unused. Consider adding a `# noqa: F841` or a comment noting they are reserved for future use / accounting.

---

#### I-03: _track_melt_end called with None when has_payment_hash is False
**File:** `services.py`, line 333

`_melt_pay`'s `finally:` block unconditionally calls `await _track_melt_end(payment_hash)`. When `decoded.has_payment_hash` is False, `payment_hash = decoded.payment_hash` is None, and `_track_melt_start` was never called (gated on `has_payment_hash` in the callback). `_track_melt_end(None)` does `get(None, 0) - 1 = -1`, which is not > 0, so it calls `pop(None, None)` — a harmless no-op. In practice, every valid BOLT11 invoice has a payment_hash, so `has_payment_hash` is always True. This is a theoretical code smell, not a bug.

---

#### I-04: Private functions imported across modules
**File:** `views_lnurl.py`, lines 36-43

`_melt_pay`, `_mint_fee_msat`, `_min_sendable_msat`, `_public_base_url`, `_track_melt_start`, `_try_settle_mint` are all underscore-prefixed (private convention) but imported from `services.py` into `views_lnurl.py`. This is a Python convention violation but is common in LNbits extensions and matches the source's pattern. No action needed unless the project adopts strict public/private separation.

---

#### I-05: boot_reconcile task not tracked in scheduled_tasks
**File:** `__init__.py`, line 50

`asyncio.create_task(boot_reconcile())` is not appended to `scheduled_tasks`, so it is not cancelled in `lnurlmint_stop`. This is intentional per the research (RQ11 Gotcha #3) — boot_reconcile is a one-shot that completes quickly. If still running at shutdown, asyncio cancels it when the event loop closes. This is acceptable but means a slow boot_reconcile could briefly outlive `lnurlmint_stop`.

---

#### I-06: reconcile_pending_melts does not handle per-melt exceptions
**File:** `services.py`, lines 349-393

If `finalize_melt` or `restore` raises (DB error) for one melt in the loop, the exception propagates and remaining melts in that tick are not processed. `run_interval` catches the exception at the outer level, and the next tick (60s later) retries. However, the same failing melt would be first in the list again (pending_melts returns it), potentially causing an infinite error loop that starves other pending melts. A per-iteration try/except would improve robustness:
```python
for payment_hash, note_ids in pending.items():
    try:
        # ... existing body ...
    except Exception as exc:
        logger.error(f"reconcile: error processing melt {note_ids}: {exc}")
        continue
```

---

#### I-07: Code duplication in _melt_pay's tristate handling
**File:** `services.py`, lines 275-331

The `finalize_melt + mark_melt_settled` sequence is repeated 3 times (success path, PaymentError confirmed-paid path, generic Exception confirmed-paid path). The `restore` + log sequence is repeated 2 times. This is a direct port of the source's structure and is correct, but a helper function (e.g., `_finalize_melt_settled(note_ids, payment_hash, mint)`) would reduce duplication and the risk of divergent edits.

---

#### I-08: bolt11 decode exception exposed in error response
**File:** `views_lnurl.py`, line 277

```python
return {"status": "ERROR", "reason": f"Invalid invoice: {exc!s}"}
```

The bolt11 decode exception message is included verbatim in the LNURL error response. This could expose internal parsing details (e.g., "invalid bech32 checksum at position X") to the caller. This is not a secret leak (bolt11 decode errors don't contain preimages or k1), but it is a minor information disclosure. A generic message like `"Invalid invoice."` would be safer.

---

#### I-09: mark_melt_settled not atomic with finalize_melt
**File:** `services.py`, lines 276-278, 304-306, 328-330, 384-385

In `_melt_pay` and `reconcile_pending_melts`, `finalize_melt` and `mark_melt_settled` are called as separate `db.execute` operations (each commits independently). If `mark_melt_settled` fails after `finalize_melt` succeeds, the notes are burned (spent=1) but the melt record shows `settled=0`. This is a verify-layer consistency issue (the melt would appear unsettled even though the notes are gone), not a funds-loss issue. Wrapping both in a `db.connect()` block would ensure consistency.

---

## Test Fixture Assessment

| Fixture | Tristate Modeling | Correctness |
|---------|-------------------|-------------|
| `FakeNode` (conftest.py:86-166) | settled→paid=True, unknown→paid=None | Correct — default pending is the safe default |
| `HodlNode` (conftest.py:169-224) | hodl live→paid=None, settled→paid=True, else→paid=False | Correct — `settle_hodl_payments` properly moves hashes to settled |
| `InFlightNode` (conftest.py:227-247) | settled→paid=True, unregistered→paid=False | Correct — models lnd 404 for unregistered payments |
| `mint_note` helper (conftest.py:327-346) | Creates invoice, records mint, settles hash, triggers lazy settlement | Correct — test mint has zero fees so net=gross |

The `_patch_services` function (conftest.py:250-265) correctly monkeypatches both `services_module` and `views_module` module-level imports. The `_CONFIRMATION_RETRY_DELAYS_SECONDS` monkeypatch to `()` ensures fast single-attempt confirmation in tests.

---

## Summary

Phase 2 is well-architected with strong adherence to the 10 security invariants. The tristate settlement contract (the highest-risk element) is correctly implemented — `_confirm_payment` properly uses `status.success`/`status.failed`/`status.paid is None` (never `status.pending`), and `_melt_pay` always routes through confirmation before restoring. The in-flight registry, compare-and-set settlement, and cross-wallet isolation are all correct.

Four Warning-level findings require attention:
- **W-01** (in-flight registry leak) is the most actionable — a missing try/except that the source explicitly has, with a trivial fix that prevents funds from being locked until restart.
- **W-02** (finalize/restore atomicity) is a latent issue for Phase 3 but worth fixing now.
- **W-03** (uncatched check_transaction_status) violates LNURL error format on funding source errors.
- **W-04** (missing test case) leaves a critical tristate path untested.

No Critical findings — no permanent fund-loss or secret-leak vulnerabilities were identified.
