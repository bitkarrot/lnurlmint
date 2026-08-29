# Phase 05 Verification — Offline Verification

**Date:** 2026-08-29
**Status:** PASSED

## Requirements Coverage

| Requirement | Status | Evidence |
|-------------|--------|----------|
| SIGN-01 | ✅ | Each mint has a secp256k1 keypair (mint_privkey populated at creation by `crud._generate_mint_privkey` since Phase 1). No migration needed. |
| SIGN-02 | ✅ | `GET /w/{mint_id}` includes `mintPubkey` derived via `mint_pubkey(mint)` (compressed pubkey from `mint.mint_privkey`). Test: `test_mint_pubkey_matches_derived_pubkey`. |
| SIGN-03 | ✅ | `/w/cb` rotate/merge response includes `sig` (over `LNURLcash:<merged_amount>:<h>`); split includes `sig` (over amount) + `sig2` (over change_amount). Signing failures swallowed (None → key omitted). Tests: rotate/split/merge verify, signing-failure-swallowed. |
| SIGN-04 | ✅ | `verify_note` (coincurve `PublicKey.from_signature_and_message`) in `signing.py`, test-only (no production import). Tests use it for round-trip verification. |

## Test Results

```
44 passed in 23.77s
```

- 34 existing tests (Phase 2 + Phase 3 + Phase 4) — no regressions
- 10 new offline verification tests:
  1. `test_mint_pubkey_matches_derived_pubkey` — PASSED
  2. `test_rotate_returns_a_valid_signature` — PASSED
  3. `test_split_returns_valid_signatures_for_both_notes` — PASSED
  4. `test_merge_returns_a_valid_signature` — PASSED
  5. `test_melt_carries_no_signature` — PASSED
  6. `test_signature_does_not_verify_against_wrong_amount` — PASSED
  7. `test_signature_does_not_verify_against_wrong_k1` — PASSED
  8. `test_signature_does_not_verify_against_wrong_pubkey` — PASSED
  9. `test_signing_failure_is_swallowed_not_raised` — PASSED
  10. `test_signing_failure_is_still_logged` — PASSED

## Code Review

**REVIEW CLEAN** — no Critical or Warning findings.

- No privkey leakage (mint_pubkey returns only the public key)
- sign_note catches all Exception subclasses, returns None, logs warning
- verify_note is test-only (no production import)
- Signature message format: `LNURLcash:<amount_msat>:<note_id_hex>`
- sig/sig2 amounts match the values swap minted
- Response is plain dict (None sig keys omitted)
- coincurve API correct (`from_secret`, `sign_recoverable`, `from_signature_and_message`)

## Files Modified

- `signing.py` — NEW: `mint_pubkey`, `sign_note` (recoverable ECDSA), `verify_note` (test-only)
- `services.py` — `sign_note` stub replaced with re-export from `signing.py`
- `views_lnurl.py` — `/w` advertises `mintPubkey`; `/w/cb` split and rotate/merge include `sig`/`sig2` conditionally
- `tests/test_offline_verification.py` — NEW: 10 ported tests
