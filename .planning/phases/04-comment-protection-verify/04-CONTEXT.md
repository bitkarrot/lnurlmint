# Phase 04: Comment Protection + Verify - Context

**Gathered:** 2026-08-28
**Status:** Ready for planning

<domain>
## Phase Boundary

A mint can use LUD-25 comment protection (note keyed by WALLET-supplied comment hash, closing the routing-node preimage race), and the LUD-21 verify endpoint reports settlement status with a real off-switch and comment-protection gating.

This phase delivers comment protection in the mint callback (/p/cb), the verify endpoint (/verify/{mint_id}/{payment_hash}), and the verify race PoC test. Comment protection and verify are paired: verify is only safe when comment protection is in play (for no-comment mints, the preimage IS the bearer secret).

</domain>

<decisions>
## Implementation Decisions

### Comment Hash Storage & Note ID
- Add `comment_hash TEXT` column to `mints_records` table (migration m003) — tracks whether a mint used comment protection.
- `settle_mint` uses `comment_hash` as note ID when present, otherwise `payment_hash` (port source pattern). The note is keyed by the WALLET-supplied hash, not the payment preimage.
- `record_mint_record` accepts optional `comment_hash` parameter. Collision check: reject if `comment_hash` already exists in `notes.id` or `mints_records.comment_hash`.
- `mint_uses_comment(payment_hash)` query: returns True if the mint record has a non-null `comment_hash`.

### Live Preimage Fetch from LNbits
- Use LNbits `get_standalone_payment(payment_hash)` for both mint and melt preimages — returns a `Payment` object with `.preimage` field. Live fetch, never cached.
- Preimage is fetched live on every /verify call, never stored in DB (store-hashes-not-secrets).
- If preimage is not available (payment not found, funding source unreachable), return `preimage: null` in the verify response.

### Claude's Discretion
- Exact verify response model fields (settled, preimage, pr) — port source's `LnurlPayVerifyResponse`.
- Whether to add `verify` URL to `LnurlPayActionResponse` — already has the field, just needs to be set when comment protection is used and verify_enabled is True.
- Error handling for get_standalone_payment — catch exceptions, return null preimage.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Phase 2 /p/cb callback** — `views_lnurl.py` mint callback already creates invoices and records pending mints. Comment param will be added here.
- **Phase 2 settle_mint** — `crud.py` settle_mint already does compare-and-set UPDATE + INSERT. Will be modified to use comment_hash as note ID when present.
- **Phase 2 record_mint_record** — `crud.py` records pending mint. Will accept comment_hash parameter.
- **Phase 2 _try_settle_mint** — `services.py` lazy settlement helper. Will pass comment_hash through.
- **Phase 2 test fixtures** — `tests/conftest.py` FakeNode pattern. Verify race PoC will reuse.
- **Phase 2 LnurlPayActionResponse** — Already has `verify` field (optional, set to None).

### Established Patterns
- **Migration format** — `async def m003_comment_hash(db)` in `migrations.py` (m001, m002 pattern).
- **DB transaction atomicity** — `async with db.connect() as conn:` for multi-statement ops.
- **LNURL error format** — `{"status":"ERROR","reason":"..."}` with HTTP 200 for callback errors; HTTP 404 for verify endpoint.
- **No-secret-logging** — no preimage/k1/comment_hash in logs.
- **pydantic v1** — BaseModel, validator, class Config.

### Integration Points
- **`/p/cb` callback** — `views_lnurl.py` — comment param added, verify URL in response.
- **`/verify/{mint_id}/{payment_hash}`** — New endpoint in `views_lnurl.py`.
- **`crud.py`** — `record_mint_record` accepts comment_hash, `settle_mint` uses comment_hash as note ID, `mint_uses_comment` query added.
- **`migrations.py`** — m003 adds comment_hash column to mints_records.
- **`services.py`** — `_try_settle_mint` passes comment_hash through.

</code_context>

<specifics>
## Specific Ideas

- Comment protection closes the routing-node preimage race: a routing node that sees the preimage cannot redeem the note because the note is keyed by the WALLET-supplied comment hash, not the payment preimage.
- The verify endpoint is only safe when comment protection is in play. For no-comment mints, the preimage IS the bearer secret — verify must return 404 to prevent preimage leakage.
- `VERIFY_ENABLED=false` must produce a real 404 (not just a hidden advertisement), because the preimage is a bearer secret and the URL shape is guessable.
- The verify race PoC (TEST-07) tests 5 scenarios: no-comment mint verify refused, comment-protected mint verify served, verify refused before and after settlement, melt direction verify harmless, verify disabled closes the hole.
- The `verify` URL is advertised in `/p/cb` response only when `comment` was used AND `verify_enabled` is True.

</specifics>

<deferred>
## Deferred Ideas

- `test_surface_hunter_verification.py` — additional verify tests; may port if time permits but not required by ROADMAP.
- Comment protection for rotate/split/merge — the source supports comment-protected swaps but this is not in Phase 4 scope (Phase 3 already shipped rotate/split/merge without comment protection).

</deferred>
