# Phase 04 Research: Comment Protection + Verify

**Researched:** 2026-08-28
**Status:** Complete — 3 plans ready

## Summary

Phase 4 ports LUD-25 comment protection (note keyed by WALLET-supplied comment hash)
and the LUD-21 verify endpoint (settlement status with a real off-switch and
comment-protection gating) from the standalone `lnurl-mint` into the LNbits
extension. The verify race PoC (`test_poc_verify_race.py`, TEST-07) locks both
together.

---

## Key Finding: m003 Migration Is NOT Needed

The `comment_hash TEXT` column **already exists** on both `mints_records` and
`notes` tables — it was added proactively in the `m002_notes_records_melts`
migration (Phase 1, Plan 01-02). Evidence:

- `migrations.py` line 98: `mints_records` table includes `comment_hash TEXT`
- `migrations.py` line 83: `notes` table includes `comment_hash TEXT`
- `crud.py` `record_mint_record` (line 444) already accepts `comment_hash: Optional[str] = None`
- `crud.py` `settle_mint` (lines 274-284) already uses `comment_hash` as note ID when present
- `models.py` `MintRecord` (line 185) and `Note` (line 150) both have `comment_hash: Optional[str] = None`

**Conclusion:** No `m003` migration is required. The DB layer is already wired
for comment protection. Plan 04-01 only needs to: (1) add the collision check to
`record_mint_record`, (2) accept the `comment` query param in `/p/cb`, (3) add
`mint_uses_comment` / `mint_pr` / `melt_pr` queries, (4) advertise the verify URL.

This is a deviation from the CONTEXT.md / ROADMAP mention of "migration m003" —
documented here so the plans reflect reality, not the original assumption.

---

## Research Topic 1: m003 Migration (N/A — column already exists)

**Source:** `lnurl-mint/db.py` uses `_add_column_if_missing` (line 110-118) — a
`PRAGMA table_info` check + `ALTER TABLE ... ADD COLUMN` — as its only migration
mechanism. The port uses LNbits' numbered migration runner (`m001`, `m002`, ...).

**Finding:** The port's `m002` already created `comment_hash TEXT` on both
`mints_records` and `notes`. LNbits' `Database` abstraction supports `ALTER TABLE
ADD COLUMN` on both SQLite and Postgres (used by other extensions' migrations),
but it is not needed here. If a future column is needed, the pattern is:

```python
async def m003_some_column(db):
    await db.execute(
        "ALTER TABLE lnurlmint.mints_records ADD COLUMN some_col TEXT"
    )
```

SQLite supports `ALTER TABLE ADD COLUMN` natively (since 3.2). Postgres supports
it natively. LNbits' `Database.execute` is cross-DB safe for this DDL.

---

## Research Topic 2: record_mint_record with comment_hash + Collision Check

**Current state** (`crud.py` lines 439-469): `record_mint_record` already
accepts `comment_hash` and inserts it. It uses `INSERT OR IGNORE` (PRIMARY KEY
on `payment_hash` prevents duplicate payment hashes).

**Missing:** The collision check from the source (`db.py` lines 137-143). The
source's `create_mint` rejects a `comment_hash` that collides with an existing
note id OR another mint record's comment_hash:

```python
# Source: db.py lines 138-143
collision = self.conn.execute(
    "SELECT 1 FROM notes WHERE id = ? UNION SELECT 1 FROM mints WHERE comment_hash = ?",
    (comment_hash, comment_hash),
).fetchone()
if collision:
    raise ValueError("comment already in use")
```

**Port adaptation:** The port's `mints` table is named `mints_records` (not
`mints` — `mints` is the per-wallet config table). The collision check becomes:

```python
# Port: check notes.id and mints_records.comment_hash
collision = await conn.fetchone(
    "SELECT 1 FROM lnurlmint.notes WHERE id = :ch "
    "UNION SELECT 1 FROM lnurlmint.mints_records WHERE comment_hash = :ch",
    {"ch": comment_hash},
)
if collision:
    raise ValueError("comment already in use")
```

This must run in the same `async with db.connect() as conn:` block as the
INSERT (atomicity — a separate transaction would allow a race between check and
insert). The current `record_mint_record` is a single `db.execute` (no
`db.connect()` block); it needs to be wrapped in a `db.connect()` block when
`comment_hash` is not None.

**Caller behavior** (`views_lnurl.py` `/p/cb`): the source catches `ValueError`
from `create_mint` and returns HTTP 400 (line 635-636). The port returns
LNURL-formatted errors (`{"status":"ERROR","reason":"..."}`) with HTTP 200, so
the caller catches `ValueError` and returns `{"status":"ERROR","reason":"comment already in use"}`.

---

## Research Topic 3: settle_mint with comment_hash (Already Done)

**Current state** (`crud.py` lines 252-295): `settle_mint` already:
1. Compare-and-set: `UPDATE mints_records SET minted=1 WHERE minted=0` + `rowcount==1`
2. Reads `amount_msat, comment_hash, mint_id` from the row
3. Uses `comment_hash` as `note_id` when present, else `payment_hash`
4. INSERTs the note with `(id, mint_id, amount_msat, 0, 0)`

This is a faithful port of the source's `settle_mint` (`db.py` lines 194-215).
**No changes needed.**

---

## Research Topic 4: _try_settle_mint with comment_hash

**Current state** (`services.py` lines 124-153): `_try_settle_mint(note_id, mint)`
looks up the pending mint record by `note_id` (which is `sha256(k1)` for
no-comment mints = the payment_hash). For comment-protected mints, the note_id
IS the comment_hash, but `get_pending_mint_record` looks up by `payment_hash`:

```python
# crud.py get_pending_mint_record (lines 196-209)
"SELECT * FROM lnurlmint.mints_records "
"WHERE payment_hash = :ph AND mint_id = :mid AND minted = 0"
```

**Problem:** For a comment-protected mint, the `/w` endpoint computes
`note_id = sha256(k1)` where `k1` is the WALLET-held secret. But the note's
stored id is the `comment_hash` (a WALLET-supplied hash, NOT `sha256(k1)`).
So `_try_settle_mint(note_id, mint)` is called with `note_id = sha256(k1)`,
which does NOT match the `payment_hash` in `mints_records`.

**Source solution** (`router.py` lines 264-286, 392-404): The source has a
separate `_mint_settled_by_comment(comment_hash)` path and
`pending_mint_by_comment(comment_hash)` query (`db.py` lines 156-165) that looks
up by `comment_hash` instead of `payment_hash`. The `_note_amount_by_id` helper
tries `note_id` as a payment hash first, then as a comment_hash.

**Port adaptation:** The `/w` endpoint's lazy-settlement path needs a
comment-keyed lookup. Two options:

- **Option A (source-faithful):** Add `get_pending_mint_record_by_comment(comment_hash, mint_id)`
  to `crud.py` and a `_try_settle_mint_by_comment` path in `services.py`. The
  `/w` endpoint tries payment-hash-keyed settlement first, then comment-keyed.
- **Option B (simpler):** Modify `get_pending_mint_record` to accept the
  `note_id` and try BOTH `payment_hash = :note_id` and `comment_hash = :note_id`
  in one query: `WHERE (payment_hash = :nid OR comment_hash = :nid) AND mint_id = :mid AND minted = 0`.

**Recommendation:** Option B — a single query that tries both keys. The
`note_id` passed to `_try_settle_mint` is either `sha256(k1)` (= payment_hash
for no-comment) or the comment_hash itself (for comment-protected). One query,
no branching. Then `settle_mint` is called with the `payment_hash` from the
found record (not the `note_id`), since `settle_mint` looks up by
`payment_hash`. This requires `_try_settle_mint` to resolve the record's
`payment_hash` and pass it to `settle_mint`.

Actually, looking more carefully at the current `_try_settle_mint`: it calls
`get_pending_mint_record(note_id, mint.id)` then
`check_transaction_status(mint.wallet, note_id)` then `settle_mint(note_id)`.
All three use `note_id` as the `payment_hash`. For comment-protected mints,
`note_id` is the comment_hash, NOT the payment_hash — so all three would fail.

**Cleanest port:** Modify `get_pending_mint_record` to match on either
`payment_hash` OR `comment_hash`, return the record (which has the real
`payment_hash`). Then `_try_settle_mint` uses `record.payment_hash` for
`check_transaction_status` and `settle_mint`. This is a small change to
`_try_settle_mint` and `get_pending_mint_record`.

---

## Research Topic 5: /p/cb comment param

**Current state** (`views_lnurl.py` lines 108-159): `get_pay_callback` accepts
only `amount` (no `comment` param). It calls `record_mint_record` without
`comment_hash` and returns `{"pr": pr, "disposable": False}` (no `verify` URL).

**Source** (`router.py` lines 582-642):
```python
async def get_pay_callback(req, amount, comment: str | None = None):
    comment_hash = comment if comment is not None and HEX32_PATTERN.match(comment) else None
    # ... create invoice, record mint with comment_hash ...
    verify = f"{base}/verify/{payment_hash}" if settings.verify_enabled and comment_hash is not None else None
    return LnurlPayActionResponse(pr=pr, verify=verify)
```

**Port adaptation:**
1. Add `comment: Optional[str] = None` query param to `get_pay_callback`.
2. `comment_hash = comment if comment is not None and HEX32_PATTERN.match(comment) else None`
   (uses the existing `HEX32_PATTERN` in `views_lnurl.py` — note: the port's
   pattern is case-insensitive `[0-9a-fA-F]{64}` while the source's is
   lowercase-only `[0-9a-f]{64}`. The port's pattern is already used for k1/h/h2
   validation, so reuse it for consistency. A WALLET sending an uppercase hex
   comment hash is still a valid 32-byte hash.)
3. Pass `comment_hash` to `record_mint_record`. Catch `ValueError` from the
   collision check → return `{"status":"ERROR","reason":"comment already in use"}`.
4. Set `verify` URL: `f"{base}/lnurlmint/verify/{mint_id}/{payment_hash}"` when
   `mint.verify_enabled and comment_hash is not None`. Note the port's verify
   path includes `mint_id` (multi-tenancy), unlike the source's `/verify/{payment_hash}`.
5. Return `LnurlPayActionResponse(pr=pr, verify=verify)` — the model already has
   the `verify: Optional[str] = None` field (models.py line 253).

**Note on `LnurlPayActionResponse`:** The current `/p/cb` returns a raw dict
`{"pr": pr, "disposable": False}`. To include `verify`, either return the
`LnurlPayActionResponse` model or add `verify` to the dict. Returning the typed
model is cleaner and matches the source. The model serializes `verify=None` as
omitted (pydantic v1 default behavior excludes None only if configured; the
model does NOT have `class Config` with `exclude_none`, so `verify: null` would
appear in JSON when None). To match the source (which omits `verify` when None),
either add a response model with exclude_none or conditionally build the dict.
**Recommendation:** Return a dict built conditionally — `resp = {"pr": pr,
"disposable": False}` then `if verify: resp["verify"] = verify` — matching the
existing pattern in `/p/cb` and avoiding `null` in the wire response.

---

## Research Topic 6: /verify endpoint

**Source** (`router.py` lines 660-710): `GET /verify/{payment_hash}`:
1. If `not settings.verify_enabled` → 404
2. `pr = notes.mint_pr(payment_hash)` — if found:
   - If `not notes.mint_uses_comment(payment_hash)` → 404 (preimage IS the secret)
   - Else → serve `_verify_response(ph, pr, _mint_settled, _mint_preimage)`
3. `pr = notes.melt_pr(payment_hash)` — if found:
   - Serve unconditionally (melt preimage is harmless)
4. Else → 404

**Port adaptation:** `GET /lnurlmint/verify/{mint_id}/{payment_hash}`:
1. If `not mint.verify_enabled` → 404 (real off-switch). The port uses
   `mint.verify_enabled` (per-mint DB field), not a global setting. Fetch the
   mint first; if mint not found → 404.
2. `pr = await mint_pr(payment_hash)` — if found:
   - If `not await mint_uses_comment(payment_hash)` → 404
   - Else → serve verify response (settled, preimage, pr)
3. `pr = await melt_pr(payment_hash)` — if found:
   - Serve unconditionally
4. Else → 404

**404 format:** The source raises `HTTPException(404, "Not found")` (FastAPI
returns `{"detail":"Not found"}`). The PoC test expects
`{"status":"ERROR","reason":"Not found"}`. **Decision:** Return
`{"status":"ERROR","reason":"Not found"}` with HTTP 404 via
`return JSONResponse(status_code=404, content={"status":"ERROR","reason":"Not found"})`
to match the PoC test's assertions. This is a hybrid: HTTP 404 (real off-switch)
with the LNURL-style error body.

Actually, re-reading the PoC test: `r.json() == {"status": "ERROR", "reason": "Not found"}`.
The source raises `HTTPException(HTTPStatus.NOT_FOUND, "Not found")` which
FastAPI serializes as `{"detail":"Not found"}` — NOT matching the test. So the
source's test client must have a custom exception handler, OR the test is
checking the body differently. Looking at the source test: it uses
`client.get(f"/verify/{victim_ph}")` and asserts `r.json() == {"status": "ERROR",
"reason": "Not found"}`. This means the source has an exception handler that
converts HTTPException to LNURL error format. The port does NOT have that
handler. **Decision:** Return the LNURL error body directly with HTTP 404 via
`JSONResponse` — this matches the PoC test assertions without needing a custom
exception handler.

---

## Research Topic 7: Live Preimage Fetch

**Source:** `_mint_preimage` (`router.py` lines 327-342) calls
`invoice_preimage(payment_hash, funding_source)` — a direct lnd/cln RPC.
`_melt_preimage` (lines 376-389) calls `payment_preimage(payment_hash, funding_source)`.

**Port:** Use LNbits' `get_standalone_payment(payment_hash)` from
`lnbits.core.crud.payments` (lines 35-67). Returns `Payment | None` with a
`.preimage: str | None` field (`core/models/payments.py` line 72).

```python
from lnbits.core.crud.payments import get_standalone_payment

async def _mint_preimage(payment_hash: str, wallet_id: str) -> Optional[str]:
    """Live preimage fetch for verify — never cached (SEC-02)."""
    try:
        payment = await get_standalone_payment(payment_hash, incoming=True)
    except Exception:
        return None
    return payment.preimage if payment else None

async def _melt_preimage(payment_hash: str, wallet_id: str) -> Optional[str]:
    """Live preimage fetch for a melt's outgoing payment."""
    try:
        payment = await get_standalone_payment(payment_hash)
    except Exception:
        return None
    return payment.preimage if payment else None
```

**`incoming=True` for mint:** `get_standalone_payment` with `incoming=True`
filters `amount > 0` (incoming payment = the mint invoice). For melt
(outgoing), `incoming` is left as default (no filter) — the payment_hash is
unique to the outgoing payment, so no ambiguity. The `wallet_id` parameter is
NOT passed (it triggers a `source_wallet_id` cross-wallet check that's not
relevant here — the payment_hash alone is sufficient).

**Store-hashes-not-secrets (SEC-02):** The preimage is fetched live on every
verify call, never persisted. `get_standalone_payment` reads from LNbits'
`apipayments` table (which stores the preimage after settlement). This is a
live DB read, not a cache — LNbits itself persists the preimage in its own
payments table (that's LNbits' design, not ours). Our extension never stores it.

**Settled check:** For mint direction, "settled" = `minted == 1` in
`mints_records` (the compare-and-set flag). But the source's `_mint_settled`
also checks the funding source live and lazily settles. The port's
`_try_settle_mint` already does this. For verify, we can check `minted` flag
directly (if the note was settled, `minted=1`). But to match the source's
"keep reporting True forever once settled" semantics, checking `minted` is
correct — it's a permanent flag. For melt direction, "settled" = `settled == 1`
in `melts` table (set by `mark_melt_settled`).

**Simplification:** The verify endpoint does NOT need to lazily settle (unlike
`/w`). It just reports status. For mint: `settled = (minted flag is 1)`. For
melt: `settled = (settled flag is 1)`. If the mint is not yet settled
(`minted=0`), verify reports `settled=False, preimage=None`. The preimage is
only fetched when `settled=True`.

---

## Research Topic 8: mint_uses_comment query

**Source** (`db.py` lines 167-176):
```python
def mint_uses_comment(self, payment_hash: str) -> bool:
    row = self.conn.execute(
        "SELECT comment_hash FROM mints WHERE payment_hash = ?", (payment_hash,)
    ).fetchone()
    return bool(row and row[0] is not None)
```

**Port:**
```python
async def mint_uses_comment(payment_hash: str) -> bool:
    row = await db.fetchone(
        "SELECT comment_hash FROM lnurlmint.mints_records WHERE payment_hash = :ph",
        {"ph": payment_hash},
    )
    return bool(row and row["comment_hash"] is not None)
```

Note: the source's table is `mints` (pending mint invoices); the port's is
`mints_records`. The `mints` table in the port is the per-wallet config table.

---

## Research Topic 9: mint_pr / melt_pr queries

**Source** (`db.py` lines 178-183, 300-304):
```python
def mint_pr(self, payment_hash): ...  # SELECT pr FROM mints WHERE payment_hash = ?
def melt_pr(self, payment_hash): ...  # SELECT pr FROM melts WHERE payment_hash = ?
```

**Port:**
```python
async def mint_pr(payment_hash: str) -> Optional[str]:
    row = await db.fetchone(
        "SELECT pr FROM lnurlmint.mints_records WHERE payment_hash = :ph",
        {"ph": payment_hash},
    )
    return row["pr"] if row else None

async def melt_pr(payment_hash: str) -> Optional[str]:
    row = await db.fetchone(
        "SELECT pr FROM lnurlmint.melts WHERE payment_hash = :ph",
        {"ph": payment_hash},
    )
    return row["pr"] if row else None
```

**Confirmed:** The port's `melts` table has a `pr` column (`migrations.py` line
112: `pr TEXT NOT NULL`). The `mints_records` table has `pr TEXT NOT NULL`
(line 95). Both queries are straightforward single-row lookups.

---

## Research Topic 10: Verify Race PoC (TEST-07)

**Source:** `tests/test_poc_verify_race.py` (187 lines, 5 test scenarios).

### Scenario 1: `test_theft_chain_closed_by_verify_refusal`
- No-comment mint → verify refused (404) even when VERIFY_ENABLED=true
- Victim's preimage still redeems the note normally
- Asserts: `"verify" not in resp.json()` on `/p/cb`, verify returns error, rotate succeeds

### Scenario 2: `test_theft_chain_closed_because_comment_makes_the_preimage_harmless`
- Comment-protected mint → verify served, preimage disclosed
- Stolen preimage does NOT redeem the note (note keyed by comment_hash, not preimage)
- Victim's own secret redeems normally
- Asserts: `resp.json().get("verify")` truthy, verify returns `settled=True` + `preimage`, rotate with stolen preimage fails, rotate with victim secret succeeds

### Scenario 3: `test_verify_refuses_the_no_comment_fallback_before_and_after_settlement`
- No-comment mint → verify 404s both before settlement AND after settlement
- Asserts: verify returns error both times

### Scenario 4: `test_melt_direction_verify_is_harmless`
- Melt a note → verify on the melt's payment_hash returns settled=True + preimage + pr
- Melt preimage is NOT a bearer secret (rotating with it fails)
- Original note's k1 is also dead (already burned)
- Asserts: `body["settled"] is True`, `body["preimage"] is not None`, `body["pr"] == melt_invoice`, rotate with melt_preimage fails

### Scenario 5: `test_verify_disabled_closes_the_hole`
- VERIFY_ENABLED=false → verify 404s even for a settled mint
- Victim rotates at human speed, unhurried
- Asserts: verify returns error, rotate succeeds

### Port Adaptation

The source test uses FastAPI `TestClient` (synchronous). The port uses
`@pytest.mark.anyio` + async fixtures (per `conftest.py`). The port must:

1. **Replace `TestClient` with async HTTP calls.** The existing PoC tests call
   CRUD/service functions directly (not via HTTP) — e.g., `mint_note()` helper,
   `record_mint_record()`, `_try_settle_mint()`. The verify PoC needs to call
   the `/p/cb` and `/verify` endpoints. Two options:
   - **Option A:** Use `httpx.AsyncClient` with the LNbits app (requires app
     startup, complex).
   - **Option B:** Call the endpoint functions directly
     (`await get_pay_callback(...)` / `await verify_invoice(...)`) — matches the
     existing PoC test pattern (white-box, no HTTP layer).

   **Recommendation:** Option B — call the async endpoint functions directly,
     passing a mock `Request` for `_public_base_url`. This matches the existing
     PoC tests which call `_try_settle_mint`, `record_mint_record`, etc.
     directly. The verify endpoint returns a dict or raises; the test asserts on
     the dict. For 404, the endpoint returns a `JSONResponse` — the test checks
     `response.status_code == 404` and `response.body`.

   Actually, looking at the existing tests more carefully: they call CRUD and
   service functions, NOT the view functions. The view functions take a
   `Request` object. For the verify PoC, the cleanest approach is to call the
   view function directly with a minimal mock request, OR to extract the verify
   logic into a service function that the view calls (testable without a
   Request). **Recommendation:** Extract verify logic into
   `services.py:_verify_mint(payment_hash, mint)` and
   `_verify_melt(payment_hash, mint)` helper functions that the view calls. The
   test calls these helpers directly. The view is a thin wrapper.

2. **`mint.verify_enabled` instead of `settings.verify_enabled`.** The port is
   per-mint. The test creates a mint with `verify_enabled=True` or `False`. The
   `db_setup` fixture creates a mint with `verify_enabled=True` (default). For
   the disabled test, create a mint with `verify_enabled=False` or update it.

3. **`FakeNode.preimages`** (conftest.py line 107): maps `payment_hash →
   preimage hex`. The verify endpoint fetches preimage via
   `get_standalone_payment`. The test must monkeypatch
   `get_standalone_payment` to return a Payment with `.preimage` from the
   FakeNode's `preimages` dict. Add a `_patch_verify` helper to conftest or
   monkeypatch in the test.

4. **`node.last_preimage`** — the source's FakeNode has this; the port's
   FakeNode stores preimages in `self.preimages[payment_hash]`. The test
   recovers the preimage via `node.preimages[payment_hash]`.

5. **Settlement simulation:** The test adds `payment_hash` to
   `node.settled` and calls `_try_settle_mint` to materialize the note (same as
   the existing `mint_note` helper). For comment-protected mints, the
   `mint_note` helper needs a variant that passes `comment_hash` to
   `record_mint_record` and uses the comment_hash as the note_id.

---

## Deviations from Source

| Aspect | Source | Port | Reason |
|--------|--------|------|--------|
| Verify path | `/verify/{payment_hash}` | `/verify/{mint_id}/{payment_hash}` | Multi-tenancy — mint_id in path for per-wallet scoping |
| verify_enabled | Global `settings.verify_enabled` | Per-mint `mint.verify_enabled` | Per-wallet mints — each mint owner controls their own verify |
| m003 migration | `_add_column_if_missing` for `comment_hash` | NOT NEEDED — column exists in m002 | Port added comment_hash proactively in Phase 1 |
| Preimage fetch | `invoice_preimage` / `payment_preimage` (direct lnd/cln RPC) | `get_standalone_payment` (LNbits DB read) | LNbits abstracts Lightning; no direct node RPC |
| 404 format | `HTTPException(404, "Not found")` → custom handler → `{"status":"ERROR","reason":"Not found"}` | `JSONResponse(404, {"status":"ERROR","reason":"Not found"})` | Port has no custom exception handler; direct JSONResponse matches PoC assertions |
| Collision check table | `mints` (pending invoices) | `mints_records` | Port's `mints` is the config table; `mints_records` is pending invoices |
| Test style | `TestClient` (sync HTTP) | Direct async function calls | Matches existing PoC test pattern (white-box) |

---

## Files to Modify

### Plan 04-01 (Comment protection)
- `crud.py` — add collision check to `record_mint_record`; add `mint_uses_comment`, `mint_pr`, `melt_pr` queries; modify `get_pending_mint_record` to match on `payment_hash` OR `comment_hash`
- `services.py` — modify `_try_settle_mint` to use `record.payment_hash` for settlement (not `note_id`)
- `views_lnurl.py` — add `comment` param to `/p/cb`; set `verify` URL in response

### Plan 04-02 (Verify endpoint)
- `models.py` — add `LnurlPayVerifyResponse` model
- `crud.py` — add `mint_settled` (check `minted` flag), `melt_settled` (check `settled` flag) queries
- `services.py` — add `_mint_preimage`, `_melt_preimage` (live fetch via `get_standalone_payment`), `_verify_mint`, `_verify_melt` helpers
- `views_lnurl.py` — add `GET /verify/{mint_id}/{payment_hash}` endpoint

### Plan 04-03 (Verify race PoC)
- `tests/conftest.py` — add `get_standalone_payment` monkeypatch to `_patch_services`; add `mint_note_with_comment` helper
- `tests/test_poc_verify_race.py` — new file, 5 scenarios ported from source

---

## Open Questions Resolved

1. **Does `get_standalone_payment` need `wallet_id`?** No — passing `wallet_id`
   triggers a `source_wallet_id` cross-wallet check. The `payment_hash` alone is
   sufficient to find the payment. Use `incoming=True` for mint direction to
   filter to incoming payments.

2. **Should verify lazily settle?** No — verify reports status only. For mint:
   `settled = (minted == 1)`. If not yet settled, report `settled=False,
   preimage=None`. The `/w` endpoint handles lazy settlement; verify is read-only.

3. **404 body format?** `{"status":"ERROR","reason":"Not found"}` via
   `JSONResponse(status_code=404, ...)` — matches PoC test assertions and
   provides a real HTTP 404 (the off-switch).

4. **HEX32_PATTERN case sensitivity for comment?** Reuse the existing
   case-insensitive pattern (`[0-9a-fA-F]{64}`). A WALLET sending uppercase hex
   is still a valid 32-byte hash. The source's lowercase-only pattern is
   stricter but unnecessary.
