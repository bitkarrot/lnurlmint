# Phase 04 Verification — Comment Protection + Verify

**Date:** 2026-08-29
**Status:** PASSED

## Requirements Coverage

| Requirement | Status | Evidence |
|-------------|--------|----------|
| COMM-01 | ✅ | `/p/cb` accepts `comment` query param; hex64 hash keys note by `comment_hash` (not payment preimage). `record_mint_record` stores `comment_hash`; `settle_mint` uses it as note ID. |
| COMM-02 | ✅ | Non-hex64 or no comment → `comment_hash = None` → falls back to preimage-keyed note. Never rejected. |
| COMM-03 | ✅ | `verify` URL in `/p/cb` response only when `comment_hash is not None AND mint.verify_enabled`. Test 1 asserts `"verify" not in resp`; Test 2 asserts `resp.get("verify")`. |
| VER-01 | ✅ | `GET /verify/{mint_id}/{payment_hash}` reports `{settled, preimage, pr}` for mints and melts. |
| VER-02 | ✅ | `mint.verify_enabled=False` → `JSONResponse(status_code=404)`. Test 5 pins this. |
| VER-03 | ✅ | `_verify_mint` returns `None` (→ 404) for no-comment mints. `mint_uses_comment` gate. Tests 1 + 3 pin this. |
| VER-04 | ✅ | `LnurlPayVerifyResponse(settled, preimage, pr)`. Preimage fetched live via `get_standalone_payment`, only when settled. |
| TEST-07 | ✅ | 5 PoC scenarios ported: verify refusal, comment-protected harmless, before/after settlement, melt harmless, off-switch. |

## Test Results

```
34 passed in 22.04s
```

- 29 existing tests (Phase 2 + Phase 3) — no regressions
- 5 new verify race PoC tests (TEST-07):
  1. `test_theft_chain_closed_by_verify_refusal` — PASSED
  2. `test_theft_chain_closed_because_comment_makes_the_preimage_harmless` — PASSED
  3. `test_verify_refuses_the_no_comment_fallback_before_and_after_settlement` — PASSED
  4. `test_melt_direction_verify_is_harmless` — PASSED
  5. `test_verify_disabled_closes_the_hole` — PASSED

## Code Review

- **C1 (Critical)**: Fixed — `record_mint_record` collision check now includes `mints_records.payment_hash`; m003 migration adds UNIQUE index on `comment_hash`; INSERT (not INSERT OR IGNORE) for comment-protected path.
- **W1 (Warning)**: Fixed — `_verify_mint` / `_verify_melt` scope all lookups by `mint_id` (prevents cross-mint verify bypass).
- **I1 (Info)**: `payment_hash` in logs — accepted (payment_hash is a non-secret hash per SEC-05 policy).

## Files Modified

- `crud.py` — `record_mint_record` (collision check), `get_pending_mint_record` (payment_hash OR comment_hash), `mint_uses_comment`, `mint_pr`, `melt_pr`, `mint_settled`, `melt_settled` (all scoped by mint_id)
- `services.py` — `_try_settle_mint` (uses `record.payment_hash`), `_mint_preimage`, `_melt_preimage`, `_verify_mint`, `_verify_melt`
- `views_lnurl.py` — `get_pay_callback` (comment param, verify URL), `verify_invoice` endpoint
- `models.py` — `LnurlPayVerifyResponse` model
- `migrations.py` — `m003_comment_hash_unique` (UNIQUE index on comment_hash)
- `tests/conftest.py` — `FakeNode.get_standalone_payment`, `mint_note_with_comment`, m003 in `_reset_db`
- `tests/test_poc_verify_race.py` — 5 PoC scenarios
