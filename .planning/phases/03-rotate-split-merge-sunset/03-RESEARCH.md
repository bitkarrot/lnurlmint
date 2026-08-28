# Phase 3 Research: Rotate + Split + Merge + Sunset

**Researched:** 2026-08-28
**Confidence:** HIGH — all source patterns verified against `~/lnurl-mint` (`router.py:763-959`, `db.py:243-286`) and all existing LNbits port code verified against `~/lnurlmint` (`crud.py`, `views_lnurl.py`, `services.py`, `models.py`, `tests/conftest.py`).

---

## RQ1: swap() Async Port — Atomic Burn N + Mint M

### Source pattern

The source `NoteStore.swap` (`~/lnurl-mint/lnurl_mint/db.py:243-286`) is the core primitive for rotate/split/merge. It atomically burns N notes and mints M notes in one `with self._lock, self.conn:` block (one sqlite transaction):

```python
def swap(self, burn_ids: list[str], mint_note_ids: list[str], mint_amounts: list[int]) -> None:
    with self._lock:
        try:
            with self.conn:
                # 1. Burn loop: validate each note (not spent, not pending), then burn
                for note_id in burn_ids:
                    row = self.conn.execute(
                        "SELECT pending FROM notes WHERE id = ? AND spent = 0", (note_id,)
                    ).fetchone()
                    if row is None:
                        raise ValueError("Invalid or already spent k1.")
                    if row[0]:
                        raise PendingNoteError("pending")
                    self.conn.execute("UPDATE notes SET spent = 1 WHERE id = ?", (note_id,))
                # 2. Mint loop: collision-check both mints (pending invoices) AND notes, then INSERT
                for note_id, amount_msat in zip(mint_note_ids, mint_amounts):
                    if self.conn.execute(
                        "SELECT 1 FROM mints WHERE payment_hash = ?", (note_id,)
                    ).fetchone():
                        raise ValueError("Invalid or already spent k1.")
                    self.conn.execute(
                        "INSERT INTO notes (id, amount_msat) VALUES (?, ?)", (note_id, amount_msat)
                    )
        except sqlite3.Error as exc:
            raise ValueError(log_internal_error("Note swap failed", exc)) from exc
```

**Key design points:**
1. **Validate-then-burn-then-mint** — all burn validations complete before any burn; all burns complete before any mint. If ANY validation fails, nothing is burned or minted (the `with self.conn:` transaction rolls back on exception).
2. **Two-table collision check** — the mint loop checks `mints` (pending/settled mint invoices) for collisions BEFORE checking `notes`. This prevents the A1 pending-mint squat attack (TEST-08): an attacker planting a note under a victim's pending mint payment_hash would shadow that mint and brick `settle_mint`'s INSERT forever.
3. **Generic error message** — collision on either table returns the same `"Invalid or already spent k1."` message as an invalid/spent burn id. Which table collided is nobody's business but the operator's (no information leak).
4. **Duplicate k1 in burn_ids** — the second burn of a duplicate finds the note already spent (by the first burn) and raises `ValueError`, rolling back the whole transaction. This is the atomicity guarantee tested in `test_poc_fee_conservation.py:test_failed_requests_change_no_value`.
5. **h == h2 in split** — the second INSERT violates the PRIMARY KEY constraint, which the `except sqlite3.Error` catches and converts to `ValueError`. Same rollback.

### LNbits target

The LNbits `Database` abstraction (`~/lnbits/lnbits/db.py`) provides `async with db.connect() as conn:` for multi-statement atomicity. The `Connection` object's `execute()` commits per call, but the `async with db.connect()` block holds a single `asyncio.Lock` throughout, serializing all access. This replaces the source's `threading.Lock` + `with self.conn:` pattern.

**Critical atomicity gap (from Phase 2 research, RQ1 gotcha #2):** LNbits' `conn.execute` commits after each call — there is no automatic rollback if a later statement in the block fails. The source relies on `with self.conn:` rolling back on exception. **Mitigation:** the validate-then-burn-then-mint pattern is safe as long as ALL validation (burn loop + collision check) completes before ANY mutation (burn UPDATE or mint INSERT). If a burn UPDATE succeeds and then a later burn validation fails, the earlier burn is already committed — partial state. **Resolution:** structure the port as three distinct phases within the `async with db.connect()` block:
1. **Validation phase** — SELECT pending/spent for ALL burn_ids; SELECT collision for ALL mint_note_ids. Raise on any failure (nothing mutated yet).
2. **Burn phase** — UPDATE spent=1 for ALL burn_ids (all validated, no failure expected).
3. **Mint phase** — INSERT for ALL mint_note_ids (all collision-checked, no failure expected).

This matches the source's structure exactly (the source's burn loop validates+burns in one pass, but the sqlite transaction rollback saves it; we can't rely on rollback, so we separate validation from mutation).

### Port mapping

```python
async def swap(
    burn_ids: list[str],
    mint_note_ids: list[str],
    mint_amounts: list[int],
    mint_id: str,
) -> None:
    """Atomically burn N notes and mint M notes in one db.connect() block.

    Validate-then-burn-then-mint: all burn validations (not spent, not
    pending) and all mint collision checks (notes + mints_records) complete
    before any mutation. Raises ValueError on invalid/spent/duplicate burn
    id or collision; PendingNoteError on pending burn id.

    The mint_id scoping (SEC-07) prevents cross-wallet note access.
    """
    async with db.connect() as conn:
        # 1. Validation phase — complete before any mutation.
        for note_id in burn_ids:
            row = await conn.fetchone(
                "SELECT pending FROM lnurlmint.notes "
                "WHERE id = :id AND spent = 0 AND mint_id = :mid",
                {"id": note_id, "mid": mint_id},
            )
            if row is None:
                raise ValueError("Invalid or already spent k1.")
            if row["pending"]:
                raise PendingNoteError("pending")
        for note_id in mint_note_ids:
            # Collision check: mints_records (pending/settled mint invoices)
            collision = await conn.fetchone(
                "SELECT 1 FROM lnurlmint.mints_records WHERE payment_hash = :id",
                {"id": note_id},
            )
            if collision is not None:
                raise ValueError("Invalid or already spent k1.")
            # Collision check: notes (existing outstanding/spent/pending notes)
            collision = await conn.fetchone(
                "SELECT 1 FROM lnurlmint.notes WHERE id = :id",
                {"id": note_id},
            )
            if collision is not None:
                raise ValueError("Invalid or already spent k1.")
        # 2. Burn phase — all validated, no failure expected.
        for note_id in burn_ids:
            await conn.execute(
                "UPDATE lnurlmint.notes SET spent = 1 "
                "WHERE id = :id AND mint_id = :mid",
                {"id": note_id, "mid": mint_id},
            )
        # 3. Mint phase — all collision-checked, no failure expected.
        for note_id, amount_msat in zip(mint_note_ids, mint_amounts):
            await conn.execute(
                "INSERT INTO lnurlmint.notes "
                "(id, mint_id, amount_msat, spent, pending) "
                "VALUES (:id, :mint_id, :amount, 0, 0)",
                {"id": note_id, "mint_id": mint_id, "amount": amount_msat},
            )
```

### Gotchas

1. **`mint_id` on notes INSERT** — the source's `notes` table has no `mint_id` column; ours does (FK to `mints`). The `swap` function must pass `mint_id` to every INSERT. The `mint_id` is derived from the burned notes (all burned notes belong to the same mint — the callback URL identifies it).
2. **No `pending_payment_hash` clear on burn** — the source's burn is `UPDATE notes SET spent = 1 WHERE id = ?` (doesn't clear pending). Our `finalize_melt` clears `pending=0, pending_payment_hash=NULL`. For `swap`, the burned notes are outstanding (not pending — the validation phase rejects pending notes), so `pending` is already 0 and `pending_payment_hash` is already NULL. The burn UPDATE only sets `spent=1`. This matches the source.
3. **`conn.execute` commits per call** — the validate-then-burn-then-mint structure ensures no partial state: if validation fails, nothing was mutated; if burn succeeds and mint fails (shouldn't happen after collision check, but defensively), the burned notes are spent but no new notes minted — the holder lost value. This is the same risk as `mark_pending` (Phase 2) and is mitigated by the collision check being complete before any INSERT. A true rollback would require `conn.rollback()` but LNbits' Connection doesn't expose that cleanly.
4. **`row["pending"]`** — LNbits returns rows as dict-like objects; `row["pending"]` is an `int` (0/1), not a `bool`. The `if row["pending"]:` check works (0 is falsy, 1 is truthy).
5. **Duplicate k1 in burn_ids** — the validation phase will find the same note valid twice (it's not spent yet), then the burn phase will burn it once (spent=1), then the second burn UPDATE will set spent=1 again (no-op, already 1). This is NOT the same as the source's behavior (which burns on the first pass and finds it spent on the second). **Fix:** deduplicate burn_ids before validation, OR check for duplicates in the validation phase. The source relies on the burn loop's `SELECT ... WHERE spent = 0` finding the note spent after the first burn. Our validate-then-burn structure doesn't burn during validation. **Resolution:** add a duplicate check in the validation phase: `if burn_ids.count(note_id) > 1: raise ValueError(...)`. Or simpler: `if len(set(burn_ids)) != len(burn_ids): raise ValueError("Invalid or already spent k1.")` at the top of the function. This matches the source's atomicity (the whole swap rolls back).

---

## RQ2: Rotate Branch — Single k1 + h, No pr/amount

### Source pattern

The source (`~/lnurl-mint/lnurl_mint/router.py:943-952`) handles rotate as a special case of merge (n=1):

```python
# rotate is a merge of one note - the refund below is exactly 0
# then, so it's covered by this same branch without a special case.
assert h is not None  # validated above, whenever pr is None
refund = (len(note_ids) - 1) * settings.base_fee_msat
merged_amount = total_msat + refund
notes.swap(note_ids, [h], [merged_amount])
return WithdrawSuccessResponse(sig=await sign_note(h, merged_amount, settings.funding_source()))
```

For a single note (rotate): `refund = (1 - 1) * base_fee = 0`, `merged_amount = total_msat + 0 = total_msat`. So rotate burns the old note and mints a new one with the **same value**, keyed by `h`.

### Port mapping

The callback (`views_lnurl.py:get_withdraw_callback`) currently returns `"Rotate/split/merge not yet implemented."` when `pr is None`. This will be replaced with the real branches.

**Validation flow (already in place from Phase 2):**
1. `pr is not None and (len(k1) > 1 or amount is not None)` → reject (REDEEM-06, already implemented).
2. `pr is None and (h is None or not HEX32_PATTERN.match(h))` → reject "missing h" (already implemented).
3. `pr is None and amount is not None and (h2 is None or not HEX32_PATTERN.match(h2))` → reject "missing h2" (to be added).

**Rotate/merge branch (pr is None, amount is None):**
```python
# Resolve all k1 → note_ids + values
note_ids = []
values = []
for note_k1 in k1:
    if not HEX32_PATTERN.match(note_k1):
        return {"status": "ERROR", "reason": "Invalid or already spent k1."}
    note_id = sha256(bytes.fromhex(note_k1)).hexdigest()
    note = await get_note(note_id, mint_id)
    if note is None:
        settled = await _try_settle_mint(note_id, mint)
        if settled:
            note = await get_note(note_id, mint_id)
    if note is None:
        return {"status": "ERROR", "reason": "Invalid or already spent k1."}
    if note.pending:
        return {"status": "ERROR", "reason": "pending"}
    note_ids.append(note_id)
    values.append(note.amount_msat)

total_msat = sum(values)
refund = (len(note_ids) - 1) * mint.base_fee_msat
merged_amount = total_msat + refund

try:
    await swap(note_ids, [h], [merged_amount], mint_id)
except PendingNoteError:
    return {"status": "ERROR", "reason": "pending"}
except ValueError as exc:
    return {"status": "ERROR", "reason": str(exc)}

await sign_note(h, merged_amount, mint)  # stub in Phase 3
return {"status": "OK"}
```

**Note:** The source resolves notes via `_resolve_note(k1)` which returns `(note_id, amount_msat)` or None. Our port uses `get_note(note_id, mint_id)` + lazy settlement (`_try_settle_mint`), matching the Phase 2 melt branch pattern. The pending check (`note.pending`) is done in the resolution loop, not in `swap` — but `swap` also validates pending (defense in depth). The resolution-loop check gives a cleaner error message ("pending" vs the generic "Invalid or already spent k1." from swap).

### Gotchas

1. **`h` is the WALLET-supplied hash, not the secret** — `h = sha256(k1_new).hexdigest()`. The mint never sees `k1_new`. The new note is stored keyed by `h` (the hash), never the underlying secret (SEC-02).
2. **`sign_note` stub** — Phase 3 calls `await sign_note(h, merged_amount, mint)` which returns `None`. The response is `{"status": "OK"}` without `sig` (Phase 5 adds real signing). The stub must exist so the callback can call it without import errors.
3. **Rotate is value-neutral** — `merged_amount = total_msat + 0 = total_msat`. The new note has exactly the same value as the old one. No fee is collected or refunded on rotate.
4. **Multiple k1s with no amount = merge** — the same code path handles both rotate (1 k1) and merge (n k1s). The `refund = (n-1) * base_fee` formula gives 0 for rotate and `(n-1) * base_fee` for merge.

---

## RQ3: Merge Branch — Many k1 + h, No pr/amount

### Source pattern

The source (`router.py:943-952`) handles merge in the same branch as rotate:

```python
refund = (len(note_ids) - 1) * settings.base_fee_msat
merged_amount = total_msat + refund
notes.swap(note_ids, [h], [merged_amount])
```

For merge (n > 1): `refund = (n - 1) * base_fee_msat`, `merged_amount = sum(values) + refund`. Merging n notes refunds `(n-1) * base_fee` — every base fee collected beyond the single one this now-one note should have cost.

### Fee arithmetic

The merge refund is `(n-1) * base_fee_msat` (NOT `fee_percent_ppm` — that was already withheld once at mint time and is not refunded). The output note value = `sum(inputs) + (n-1) * base_fee_msat`.

**Conservation argument (from `test_poc_fee_conservation.py:test_hundred_note_merge_is_not_a_base_fee_printing_press`):** Each split collects ONE base_fee while producing two notes. Merging N notes refunds (N-1) base fees. If you split 99 dust notes off one mint (99 splits, 99 base fees collected) then merge all 100 notes (refund = 99 * base_fee), the refund EXACTLY equals what the splits collected. Net effect zero; the mint keeps precisely the mint fee. No inflation.

### Port mapping

Same code path as rotate (RQ2). The `refund = (len(note_ids) - 1) * mint.base_fee_msat` formula handles both cases. The only difference is `len(note_ids) > 1` for merge.

### Gotchas

1. **`max_k1s` limit** — the source has `settings.max_k1s = 100` and rejects `len(k1) > max_k1s`. Our port doesn't have a `max_k1s` setting (per-wallet mint config doesn't include it). **Decision:** port a constant `_MAX_K1S = 100` in `views_lnurl.py` (or add it to the mint model later). For Phase 3, add the check: `if len(k1) > _MAX_K1S: return {"status": "ERROR", "reason": f"Too many k1s (max {_MAX_K1S})."}`.
2. **Merge can exceed `max_sendable_msat`** — `test_poc_fee_conservation.py:test_merge_can_exceed_max_sendable_but_stays_conserved` documents this. Merging two near-max notes produces an oversized note. This is NOT inflation (the melt pays out exactly what was paid in minus fees). No cap is needed on merged note value — `max_sendable_msat` bounds `/p/cb` only.
3. **Operator fee raise overrefunds** — `test_poc_fee_conservation.py:test_operator_fee_raise_overrefunds` documents that merge refunds use the CURRENT `base_fee_msat`, not the historical one. If an operator raises `base_fee_msat` while notes are outstanding, merges of pre-raise notes refund more than was collected. This is an operator footgun (not attacker-reachable), documented but not fixed.

---

## RQ4: Split Branch — k1 + amount + h + h2

### Source pattern

The source (`router.py:913-941`) handles split:

```python
if amount is not None:
    if not 0 < amount < total_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, f"amount must be between 0 and {total_msat} msat.")
    # base_fee_msat (never fee_percent_ppm) comes out of change
    change_before_fee = total_msat - amount
    if change_before_fee < settings.base_fee_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "insufficient value")
    change_amount = change_before_fee - settings.base_fee_msat
    if change_amount < 1:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "insufficient value")
    assert h is not None and h2 is not None
    notes.swap(note_ids, [h, h2], [amount, change_amount])
    return WithdrawSuccessResponse(
        sig=await sign_note(h, amount, funding_source),
        sig2=await sign_note(h2, change_amount, funding_source),
    )
```

### Fee arithmetic

Split collects exactly ONE `base_fee_msat`, taken from the **change** side (not the requested amount):
- `change_before_fee = total_msat - amount`
- Reject if `change_before_fee < base_fee_msat` (negative change)
- `change_amount = change_before_fee - base_fee_msat`
- Reject if `change_amount < 1` (zero-value note — "nothing" is never a valid note value)
- Mint two notes: `[h=amount, h2=change_amount]`

**Bounds on amount:** `0 < amount < total_msat` (amount must be positive and less than total).

**Why base_fee from change (not amount):** so a holder can't dodge the fee by splitting into many dust notes and melting each separately. The fee is always on the change side.

### Port mapping

```python
# Split branch (pr is None, amount is not None)
if not 0 < amount < total_msat:
    return {"status": "ERROR", "reason": f"amount must be between 0 and {total_msat} msat."}
change_before_fee = total_msat - amount
if change_before_fee < mint.base_fee_msat:
    return {"status": "ERROR", "reason": "insufficient value"}
change_amount = change_before_fee - mint.base_fee_msat
if change_amount < 1:
    return {"status": "ERROR", "reason": "insufficient value"}

try:
    await swap(note_ids, [h, h2], [amount, change_amount], mint_id)
except PendingNoteError:
    return {"status": "ERROR", "reason": "pending"}
except ValueError as exc:
    return {"status": "ERROR", "reason": str(exc)}

await sign_note(h, amount, mint)  # stub
await sign_note(h2, change_amount, mint)  # stub
return {"status": "OK"}
```

### Gotchas

1. **`h2` validation** — `h2` is required when `amount` is present. The validation `if amount is not None and (h2 is None or not HEX32_PATTERN.match(h2)): return {"status": "ERROR", "reason": "missing h2"}` must be added to the callback. This is currently missing (Phase 2 only checks `h`).
2. **Sunset gating** — split is rejected when `sunset_mint=True` (increases outstanding liability — one note becomes two). The check `if mint.sunset_mint and amount is not None: return {"status": "ERROR", "reason": "This mint is sunsetting - splitting is disabled."}` must be added BEFORE the split branch. Rotate/merge/melt are unaffected.
3. **`change_amount < 1` vs `change_amount < 0`** — the source rejects `change_amount < 1` (not `< 0`). A change of exactly 0 is a zero-value note, which is never valid regardless of settings. A change of 1 msat (dust) IS allowed.
4. **`change_before_fee < base_fee_msat`** — this catches the case where change would go negative after fee. If `change_before_fee == base_fee_msat`, `change_amount = 0` which is then caught by `change_amount < 1`.
5. **Response carries no secret** — split returns `{"status": "OK"}` (with optional `sig`/`sig2` in Phase 5). No `pr` or preimage is returned. The new notes' secrets (`k1_new` for h, `k1_new2` for h2) are WALLET-generated and never transmitted to the mint.

---

## RQ5: Sunset Gating

### Source pattern

The source gates sunset at two points:

1. **`/p/cb` (mint callback)** — `router.py:607-608`:
```python
if settings.sunset_mint:
    raise HTTPException(HTTPStatus.BAD_REQUEST, "This mint is sunsetting - minting is disabled.")
```

2. **`/w/cb` split branch** — `router.py:814-815`:
```python
if settings.sunset_mint and amount is not None:
    raise HTTPException(HTTPStatus.BAD_REQUEST, "This mint is sunsetting - splitting is disabled.")
```

Rotate, merge, and melt are **unaffected** by sunset — none increase outstanding liability. A sunsetting operator still needs holders able to consolidate (merge) and redeem (melt).

### LNbits target (already partially implemented)

The `/p/cb` sunset check is **already implemented** in `views_lnurl.py:114-118`:
```python
if mint.sunset_mint:
    return {
        "status": "ERROR",
        "reason": "This mint is sunsetting - minting is disabled.",
    }
```

The `/lnurlp/{mint_id}` payRequest also checks sunset (`views_lnurl.py:67-71`), rejecting with the same message. This prevents the wallet from even seeing the payRequest.

The `/w/cb` split branch sunset check is **NOT yet implemented** — it will be added in Phase 3. The check goes after the `pr` combination rejection and before the `h`/`h2` validation:

```python
# Sunset: split grows outstanding notes, rejected while sunsetting.
# rotate/merge/melt are unaffected (none increase liability).
if mint.sunset_mint and amount is not None:
    return {
        "status": "ERROR",
        "reason": "This mint is sunsetting - splitting is disabled.",
    }
```

### Gotchas

1. **Sunset check placement** — the split sunset check must be BEFORE the `h`/`h2` validation (so a sunsetting mint rejects split even if h/h2 are missing/invalid). The source places it at `router.py:814` before the `if pr is None:` block at line 819.
2. **Per-mint sunset** — our port uses `mint.sunset_mint` (per-mint DB row), not a global `settings.sunset_mint`. This is the per-wallet multi-tenancy model.
3. **Error message** — the source uses "This mint is sunsetting - minting is disabled." for `/p/cb` and "This mint is sunsetting - splitting is disabled." for split. Our port should match these exactly (ECON-05).

---

## RQ6: h/h2 Validation

### Source pattern

The source (`router.py:819-823`) validates h/h2:

```python
if pr is None:
    if h is None or not HEX32_PATTERN.match(h):
        raise HTTPException(HTTPStatus.BAD_REQUEST, "missing h")
    if amount is not None and (h2 is None or not HEX32_PATTERN.match(h2)):
        raise HTTPException(HTTPStatus.BAD_REQUEST, "missing h2")
```

Rules:
- `h` required when `pr` is absent (rotate/split/merge all need h).
- `h2` additionally required when `amount` is present (split needs both h and h2).
- Both must match `HEX32_PATTERN` (64-char hex = sha256 hash or 32-byte preimage hex).
- Multiple k1s allowed for merge/split (the source accepts `k1: list[str] = Query(...)`).

### LNbits target (partially implemented)

The `h` validation is already in `views_lnurl.py:248-250`:
```python
if pr is None:
    if h is None or not HEX32_PATTERN.match(h):
        return {"status": "ERROR", "reason": "missing h"}
    return {
        "status": "ERROR",
        "reason": "Rotate/split/merge not yet implemented.",
    }
```

The `h2` validation is **NOT yet implemented** — it will be added when the split branch is implemented. The `HEX32_PATTERN` is already defined at `views_lnurl.py:51`.

### Port mapping

Replace the "not yet implemented" stub with:
```python
if pr is None:
    if h is None or not HEX32_PATTERN.match(h):
        return {"status": "ERROR", "reason": "missing h"}
    if amount is not None and (h2 is None or not HEX32_PATTERN.match(h2)):
        return {"status": "ERROR", "reason": "missing h2"}
```

Then proceed to the rotate/merge/split branches.

### Gotchas

1. **`h`/`h2` are hashes, not secrets** — `h = sha256(k1_new).hexdigest()`. The mint stores the note keyed by `h` (the hash). The WALLET keeps `k1_new` (the secret) and never transmits it. This is the store-hashes-not-secrets policy (SEC-02).
2. **`HEX32_PATTERN`** — `re.compile(r"^[0-9a-fA-F]{64}$")`. Matches 64-char hex (lower or upper case). Already defined in `views_lnurl.py:51`.
3. **h == h2 in split** — if the WALLET sends the same hash for both h and h2, the swap's second INSERT will collide with the first (same note id). The collision check in `swap` catches this (the second mint_note_id collides with the first, which was just INSERTed in the mint phase). **Wait** — with our validate-then-burn-then-mint structure, the collision check runs BEFORE any INSERT. So h == h2 would pass the collision check (neither h nor h2 exists in notes yet), then the first INSERT succeeds, then the second INSERT collides (PRIMARY KEY violation). **Fix:** add a check `if h == h2: return {"status": "ERROR", "reason": "Invalid or already spent k1."}` in the split branch, OR rely on the DB PRIMARY KEY constraint catching it. The source relies on the sqlite PK constraint + `except sqlite3.Error`. Our port should check explicitly: `if len(set(mint_note_ids)) != len(mint_note_ids): raise ValueError(...)` inside `swap`'s validation phase.

---

## RQ7: Fee Conservation PoC (TEST-06)

### Source pattern

`test_poc_fee_conservation.py` (398 lines) is a white-box Ledger that drives the real endpoints and tracks `paid_in`, `outstanding`, `melted_out`, `fees`, `refunds`. After every operation it asserts the conservation identity:

```
paid_in == outstanding + melted_out + fees_collected - refunds
```

And `attacker_gain = outstanding + melted_out - paid_in <= 0` (no inflation).

**Test cases:**
1. `test_simple_cycles` — mint→rotate→melt, mint→split→merge→melt, deep split chain (9 dust splits + merge), cross-merge of 3 mints×2 notes.
2. `test_dust_split_edges` — change of exactly 1 msat allowed; change of 0 rejected; amount of 1 msat works; failed split changes nothing.
3. `test_hundred_note_merge_is_not_a_base_fee_printing_press` — 99 dust splits + 100-note merge: refund exactly equals split fees collected.
4. `test_fee_arithmetic_grid_never_attacker_favorable` — grid sweep of (base_fee, ppm, gross): minted net never exceeds gross - base_fee.
5. `test_zero_value_mint_edge_no_gain` — zero-value notes (fee == gross) merge into zero-value notes; no gain.
6. `test_sub_sat_base_fee_rounding_is_mint_favorable` — base_fee=1 msat: mint fee rounds up to 1000, splits/refunds use raw 1; rounding gap kept by mint.
7. `test_failed_requests_change_no_value` — duplicate k1, h==h2, merge onto existing note, split amount==total: all fail atomically, nothing burned/minted.
8. `test_merge_can_exceed_max_sendable_but_stays_conserved` — merge of two near-max notes produces oversized note; melt pays out exactly.
9. `test_operator_fee_raise_overrefunds` — informational: operator raising base_fee while notes outstanding causes overrefund (not attacker-reachable).

`test_poc_fee_loop.py` (109 lines) tests fee/bounds config validation:
1. `test_fee_percent_ppm_at_or_above_100_percent_rejected_at_startup` — ppm >= 1M rejected.
2. `test_fee_percent_ppm_above_the_practical_bound_is_also_rejected` — ppm > 100K rejected; 100K allowed.
3. `test_negative_fee_values_rejected_at_startup` — negative ppm/base_fee rejected.
4. `test_inverted_sendable_bounds_rejected_at_startup` — min > max rejected.
5. `test_zero_health_check_interval_rejected_at_startup` — health check interval 0 rejected (N/A for LNbits — no health check interval setting).
6. `test_min_sendable_walk_terminates_under_worst_legal_config` — worst legal config terminates.
7. `test_iteration_cap_turns_a_pathological_config_into_a_loud_error` — cap converts hang to error.

### LNbits target adaptation

The source uses `TestClient` (sync) + `FakeNode` + global `settings`. The port uses:
- **`httpx.AsyncClient`** or direct `views_lnurl.py` function calls (the Phase 2 tests call the endpoint functions directly, not via HTTP client — check existing test pattern).
- **`FakeNode`** from `tests/conftest.py` (already monkeypatches LNbits payment services).
- **Per-mint DB rows** instead of global `settings` — the `Ledger` must create a mint with specific fee settings via `create_mint` + `update_mint`, not monkeypatch `settings`.
- **`mint_note` helper** from `tests/conftest.py` (already exists, returns `(k1, note_id, mint)`).
- **`fresh_secret()` helper** — needs to be added to `tests/conftest.py`: `secret = urandom(32).hex(); return secret, sha256(bytes.fromhex(secret)).hexdigest()`.

**Key adaptation:** The source's `Ledger` reads note values via `notes.note_amount(note_id)` (direct DB access). The port should use `get_note(note_id, mint_id)` from `crud.py` (or a direct DB query helper). The source's `notes.mint_settled(ph)` and `notes.pending_mint(ph)` have no direct port equivalent — use `get_pending_mint_record` and check `minted` flag.

**Fee loop tests adaptation:** The source tests `Settings(fee_percent_ppm=...)` validation. Our port uses `CreateMint(fee_percent_ppm=...)` which already has `Field(0, ge=0, le=100_000)` (Phase 1). The `test_fee_percent_ppm_above_the_practical_bound` and `test_negative_fee_values` tests are already covered by the pydantic model constraints. The `_min_sendable_msat` walk termination and iteration cap tests can be ported directly (the function already exists in `services.py` with the 100K iteration cap). The `test_zero_health_check_interval` test is N/A (LNbits has no health check interval setting).

### Gotchas

1. **`Ledger` reads from DB, not responses** — the source's `Ledger` reads `notes.note_amount(note_id)` directly from the DB (white-box). The port should do the same via `get_note` or a direct query. Never trust the response's `maxWithdrawable` for conservation assertions (the source doesn't).
2. **`base_fee_msat` for split/merge** — the `Ledger` uses `settings.base_fee_msat` for fee arithmetic. The port uses `mint.base_fee_msat` from the mint row. The `Ledger` must create the mint with the desired fee settings.
3. **`fee_percent_ppm` is NOT used in split/merge** — only `base_fee_msat`. The `fee_percent_ppm` was already withheld at mint time and is not refunded or re-collected on split/merge.
4. **Mint fee rounding** — `_mint_fee_msat` rounds UP to nearest sat. Split/merge use raw `base_fee_msat` (no rounding). The `test_sub_sat_base_fee_rounding` test verifies this gap is always mint-favorable.

---

## RQ8: Collision Griefing PoC (TEST-08)

### Source pattern

`test_poc_a1_collision_griefing.py` (144 lines) tests the A1 pending-mint squat attack:

**Attack scenario:**
1. Victim requests a mint invoice (`/p/cb?amount=50000`) — payment_hash is visible in the BOLT11 pr.
2. Attacker mints a dust note (`/p/cb?amount=10000`), pays it, gets k1.
3. Attacker rotates/splits/merges with `h = victim's payment_hash` — plants a squatter note under the victim's future note id.
4. Pre-fix: the squatter note shadows the victim's mint. When the victim pays, `settle_mint`'s INSERT PK-collides with the squatter row and rolls back forever — the paid mint can never materialize.
5. Post-fix: `swap` collision-checks `mints` (pending invoices) and rejects the squat atomically (nothing burned).

**Test cases:**
1. `test_rotate_squat_is_rejected_and_victim_mint_survives` — rotate with h=victim_ph rejected; victim mint materializes normally.
2. `test_split_and_merge_squats_are_rejected_identically` (parametrized: split_h, split_h2, merge) — all swap paths reach the same guard.
3. `test_squat_on_an_already_settled_mints_id_is_also_rejected` — settled mint's payment_hash stays in `mints` forever; collision rejected.
4. `test_legitimate_ids_still_pass_the_guard` — no false positives: fresh WALLET-generated h/h2 work fine.

### LNbits target adaptation

The source uses `client.get("/w/cb?...")` (TestClient). The port calls the callback function directly or via an async HTTP client. The key assertions:
- Squat rejected with `{"status": "ERROR", "reason": "Invalid or already spent k1."}`.
- Attacker's note NOT burned (atomic rollback).
- No squatter note planted under victim's id.
- Victim mint materializes normally after the rejected squat.

**Port-specific:** The victim's pending mint is created via `record_mint_record` (not via the `/p/cb` endpoint, since we need the payment_hash directly). The attacker's note is minted via the `mint_note` helper. The squat attempt calls the `/w/cb` callback with `h=victim_ph`.

### Gotchas

1. **`mints_records` not `mints`** — the source's `mints` table maps to our `mints_records` table. The collision check in `swap` queries `mints_records.payment_hash`.
2. **Settled mint collision** — a settled mint's payment_hash stays in `mints_records` forever (the row is never deleted). The collision check catches this too.
3. **Atomicity** — the squat must fail atomically: the attacker's input note is NOT burned. This is the validate-then-burn-then-mint structure in `swap` (validation fails before any burn).
4. **Generic error message** — the squat returns the same `"Invalid or already spent k1."` as any other invalid id. No information about which table collided.

---

## RQ9: sign_note Stub

### Source pattern

The source (`~/lnurl-mint/lnurl_mint/signing.py:51`) signs notes via the funding source's `signmessage` RPC:

```python
async def sign_note(note_id_hex: str, amount_msat: int, config: LightningBackendConfig) -> str | None:
    """A recoverable signature over (note_id_hex, amount_msat) per LUD-25's
    Offline verification, signed by the funding source node's own
    signmessage RPC, as 65 bytes (r, then s, then recovery id),
    hex-encoded."""
```

### LNbits target (Phase 5 implements real signing)

Phase 3 stubs `sign_note` in `services.py`:

```python
async def sign_note(h: str, amount_msat: int, mint: Mint) -> None:
    """Stub — Phase 5 implements real signing with per-mint keypair.

    Returns None so the callback can call it without import errors.
    Phase 5 will return a recoverable ECDSA signature over
    `LNURLcash:<amount>:<note_id_hex>` using the mint's private key.
    """
    return None
```

The callback calls `await sign_note(h, merged_amount, mint)` and ignores the return value (Phase 3 returns `{"status": "OK"}` without `sig`/`sig2`). Phase 5 will capture the return value and include it in the `WithdrawSuccessResponse`.

### Gotchas

1. **Do NOT reference `sign_note` in production code paths until Phase 5** — per CONTEXT.md. The stub exists only so the callback can call it without import errors. The return value is discarded.
2. **Signature is over `h` (the hash), not `k1` (the secret)** — the mint never sees `k1`. Phase 5 signs `LNURLcash:<amount>:<h>` with the mint's private key.
3. **`WithdrawSuccessResponse` already has `sig`/`sig2`** — `models.py:274-285` defines `WithdrawSuccessResponse` with `sig: Optional[str] = None` and `sig2: Optional[str] = None`. Phase 3 returns `{"status": "OK"}` (no sig/sig2). Phase 5 will populate them.

---

## Summary: Port Decisions

| Topic | Source | Port | Notes |
|-------|--------|------|-------|
| `swap()` atomicity | `with self._lock, self.conn:` (sqlite rollback) | `async with db.connect() as conn:` (validate-then-burn-then-mint) | No rollback; separate validation from mutation |
| Collision check | `mints` table | `mints_records` table | Same logic, different table name |
| Rotate | merge with n=1 (refund=0) | same | Value-neutral |
| Merge | `refund = (n-1) * base_fee` | same | `base_fee_msat` from mint row |
| Split | `change = total - amount - base_fee` | same | Reject `change < 1` |
| Sunset /p/cb | `settings.sunset_mint` | `mint.sunset_mint` (already implemented) | Per-mint |
| Sunset split | `settings.sunset_mint and amount is not None` | `mint.sunset_mint and amount is not None` | New check |
| h/h2 validation | `HEX32_PATTERN` | `HEX32_PATTERN` (already defined) | Add h2 check |
| `sign_note` | `signmessage` RPC | stub returning None | Phase 5 implements |
| Fee conservation PoC | `TestClient` + `FakeNode` + `settings` | async + `FakeNode` + per-mint DB rows | `Ledger` reads from DB |
| Collision PoC | `TestClient` + `mint_note` fixture | async + `mint_note` helper | Same assertions |
| `max_k1s` | `settings.max_k1s = 100` | `_MAX_K1S = 100` constant | No per-mint setting |
