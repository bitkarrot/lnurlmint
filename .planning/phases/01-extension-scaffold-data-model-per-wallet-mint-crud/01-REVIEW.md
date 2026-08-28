---
phase: 01
status: fixes_applied
depth: standard
reviewed_at: 2026-08-28
---

# Phase 1 Code Review

## Critical

### C-01: Mint private key exposed in all API responses

**File:** `views_api.py`, lines 53, 62, 78, 98
**Also:** `models.py` line 50 (`mint_privkey` field on `Mint` model)

Every management API endpoint returns `mint.dict()`, which includes the
`mint_privkey` field — the mint's secp256k1 private signing key. This was
confirmed at runtime: `Mint(...).dict()` yields `{'mint_privkey': 'deadbeef...', ...}`.

The exposure is worst on the GET endpoints (lines 56–78), which are protected
by `require_invoice_key` — a read-only key that LNbits users routinely share
with third-party services for invoice creation. Any invoice-key holder can
retrieve every mint's private key.

**Impact:** The `mint_privkey` is the mint's signing identity for offline
verification (SIGN-01 through SIGN-03). Leaking it allows forging `sig`/`sig2`
signatures on rotate/split/merge responses, completely breaking the offline
verification trust model (a holder could be tricked into accepting a forged
note). This is a bearer-asset security violation.

**Suggested fix:** Exclude `mint_privkey` from all API responses. The cleanest
approach is a dedicated response model (or a `MintPublic` projection) that
omits `mint_privkey`:

```python
class MintResponse(BaseModel):
    """API response model — mint config without the signing key."""
    id: str
    wallet: str
    username: str
    base_url: str = ""
    onion_url: Optional[str] = None
    base_fee_msat: int = 0
    fee_percent_ppm: int = 0
    min_sendable_msat: int = 1000
    max_sendable_msat: int = 1_000_000_000
    min_mint_msat: int = 10_000
    verify_enabled: bool = True
    sunset_mint: bool = False
    created_at: datetime
    updated_at: datetime
```

Then return `MintResponse(**mint.dict()).dict()` (or `mint.dict(exclude={'mint_privkey'})`)
from every endpoint. The private key should never leave the server after
creation.

---

## Warning

### W-01: `update_mint` builds SQL column names from caller-supplied keys

**File:** `crud.py`, line 65

```python
set_clauses = ", ".join(f"{k} = :{k}" for k in fields)
```

Field names are interpolated directly into the SQL string (values are
parameterized, but column names are not). This is currently safe because the
only caller (`api_update_mint`, `views_api.py` line 94) passes keys from
`UpdateMint.dict()`, which are constrained to the model's fixed field names.
However, `update_mint` is a public CRUD function with no internal guard — a
future caller passing user-controlled keys would enable SQL injection via
column-name interpolation.

**Suggested fix:** Add an explicit whitelist of updatable column names and
filter at the CRUD layer:

```python
_UPDATABLE_FIELDS = frozenset({
    "username", "base_url", "onion_url", "base_fee_msat",
    "fee_percent_ppm", "min_sendable_msat", "max_sendable_msat",
    "min_mint_msat", "verify_enabled", "sunset_mint",
})

async def update_mint(mint_id: str, wallet_id: str, **fields) -> Optional[Mint]:
    fields = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}
    if not fields:
        return await get_mint(mint_id, wallet_id)
    ...
```

This also prevents accidentally updating immutable fields (`id`, `wallet`,
`mint_privkey`, `created_at`) if a caller mistakenly includes them.

---

## Info

### I-01: `api_delete_mint` has a TOCTOU window between existence check and delete

**File:** `views_api.py`, lines 118–121

`get_mint` (one transaction) is called before `delete_mint` (a separate
transaction). If the mint is deleted by a concurrent request between these two
calls, `delete_mint` deletes 0 rows and returns `True`, so the API returns
`{"success": True}` for a mint that was already gone. The end state is
consistent (the mint is deleted), so this is not security-critical, but the
response is misleading. Moving the existence check inside the
`delete_mint` transaction (or having `delete_mint` return a tri-state:
deleted / has-outstanding / not-found) would make the response accurate.

### I-02: `Mint` model lacks `created_at`/`updated_at` pre-validators

**File:** `models.py`, lines 35–52

`Note`, `MintRecord`, and `MeltRecord` all have `@validator("created_at", pre=True)`
calling `_parse_created_at`, but `Mint` does not. This is not a bug — `Mint` is
only constructed from DB rows (where `dict_to_model` handles datetime
conversion) or in `api_create_mint` (which passes explicit `datetime` objects).
The validators on the other models exist for test/fixture construction with
date-only strings. The inconsistency is intentional but worth documenting to
prevent confusion.

### I-03: `UpdateMint` cannot clear nullable fields to `null`

**File:** `views_api.py`, line 94

```python
fields = {k: v for k, v in data.dict().items() if v is not None}
```

Sending `{"onion_url": null}` to clear the field is treated as "not provided"
and skipped — the `None`-means-absent pattern makes it impossible to
explicitly null out a field. This is a known limitation for Phase 1 (the
placeholder UI doesn't expose field clearing) but will matter for UI-02
(Phase 6). A common workaround is a sentinel value or `model.dict(exclude_unset=True)`
to distinguish "not sent" from "sent as null."

### I-04: `count_outstanding_notes` duplicates the SQL query in `delete_mint`

**File:** `crud.py`, lines 84–90 vs. 106–111

The outstanding-notes COUNT query is written twice — once in
`count_outstanding_notes` (using `db.fetchone`) and once in `delete_mint`
(using `conn.fetchone` inside a transaction). They can't trivially share a
helper because one uses `Database` and the other uses `Connection`. If the
query changes in Phase 2, both must be updated in sync. Low priority for
Phase 1 since `count_outstanding_notes` is not yet called by any endpoint.

### I-05: No uniqueness constraint on `mints.username`

**File:** `migrations.py`, line 20

`username TEXT NOT NULL` has no UNIQUE constraint, so multiple mints in the
same wallet (or across wallets) can share the same username. This is fine for
Phase 1 (no LNURL resolution yet), but Lightning Address resolution
(LADDR-01, deferred to v2) would be ambiguous with duplicate usernames.
No action needed now — flagging for future awareness.

### I-06: `index.js` `fetchMints` falls back to `adminkey` unnecessarily

**File:** `static/js/index.js`, line 29

```javascript
const key = wallet.inkey || wallet.adminkey
```

Every LNbits wallet has an `inkey` (invoice key), so the `|| wallet.adminkey`
fallback is redundant. Not a bug — the GET endpoint accepts invoice keys —
but the fallback to the more privileged admin key is unnecessary and slightly
confusing. Using `wallet.inkey` directly would be clearer and more
principle-of-least-privilege.

---

## Security Invariant Verification

| Invariant | Status | Notes |
|-----------|--------|-------|
| Store-hashes-not-secrets (SEC-02) | **PASS** | No `preimage`/`secret`/`k1` column in any table. `notes.id` is `sha256(k1)` hex. `mint_privkey` is a signing key, not a bearer credential — but see C-01 for its exposure via API. |
| Cross-wallet isolation (SEC-07/DATA-05) | **PASS** | Every query in `crud.py` includes `WHERE wallet = :wallet` (get_mints_by_wallet, get_mint, update_mint) or JOINs on `mints.wallet` (count_outstanding_notes, delete_mint). No cross-wallet note access is possible. |
| DB transaction atomicity (REC-03) | **PASS** | `delete_mint` uses `async with db.connect() as conn:` for the outstanding-notes check + delete. Single-statement ops correctly use `db.insert`/`db.fetchone`/`db.execute`. |
| Pydantic v1 syntax (DATA-04) | **PASS** | All models use `BaseModel`, `validator`, `root_validator`, `Field`. No v2 `field_validator`/`model_validator` anywhere. |
| No new dependencies (EXT-04) | **PASS** | Only `coincurve` (transitive via LNbits nostr/nwc), stdlib (`time`, `uuid`, `datetime`), and LNbits core imports. |
| Auth decorators | **PASS** | `require_admin_key` on POST/PUT/DELETE; `require_invoice_key` on GET. Matches giftcards pattern. |
| Delete guard (409 on outstanding notes) | **PASS** | `delete_mint` returns `False` when outstanding notes exist; `api_delete_mint` raises 409. Atomic check inside transaction. |
| Input validation (mint config bounds) | **PASS** | `CreateMint` and `UpdateMint` enforce `ge`/`le` bounds via `Field`. `root_validator` checks `min_sendable_msat <= max_sendable_msat`. `fee_percent_ppm` capped at 100,000 (10%). |

---

## Summary

Phase 1 is well-structured and the core security invariants (cross-wallet
isolation, store-hashes-not-secrets, transaction atomicity, pydantic v1
syntax, auth decorators, delete guard) are all correctly implemented.

One **critical** finding: the mint's secp256k1 private signing key
(`mint_privkey`) is returned in every API response via `mint.dict()`,
including read-only endpoints protected by `require_invoice_key`. This must
be fixed before Phase 5 (offline verification) lands, and ideally before any
deployment, as it allows forging mint signatures.

One **warning**: `update_mint` interpolates caller-supplied keys as SQL
column names without a whitelist — safe today but a latent injection vector
for future callers.

Six **info**-level items are non-blocking observations for future phases.
