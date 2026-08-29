# Phase 06 Verification — Tor + Frontend

**Date:** 2026-08-29
**Status:** PASSED

## Requirements Coverage

| Requirement | Status | Evidence |
|-------------|--------|----------|
| TOR-01 | ✅ | `_public_base_url` in `services.py` checks `mint.onion_url`: when the request Host matches the onion hostname, the onion URL is used as the base for callback/withdrawLink URLs. All 4 LNURL endpoints (`/lnurlp`, `/p/cb`, `/w`, `/w/cb`) use `_public_base_url` transparently. The public one-pager API (`api_get_public_mint_info`) also uses it for LNURL generation. Tests: `test_public_base_url_prefers_onion_for_matching_host`, `test_pay_response_uses_onion_url_when_reached_via_onion`, `test_public_mint_info_lnurl_uses_onion_when_on_tor`. |
| TOR-02 | ✅ | `_public_base_url` is spoof-proof: the base is built from the operator's per-mint `base_url` / `onion_url` settings, never from a raw request Host header. The onion match is against `mint.onion_url` (operator-configured), so an attacker cannot inject an arbitrary base URL. The X-Forwarded-Host trusted-proxy assumption is documented in the docstring. Tests: `test_public_base_url_spoofed_onion_host_does_not_trigger`, `test_public_base_url_ignores_onion_for_other_hosts`. |
| UI-01 | ✅ | Management SPA at `/lnurlmint/` (Vue 3 + Quasar, `static/js/index.vue` + `index.js`): "Create Mint" dialog collects all config fields (username, base_fee_msat, fee_percent_ppm, min_sendable_msat, max_sendable_msat, min_mint_msat, verify_enabled, sunset_mint, base_url, onion_url). Each mint row expands to show outstanding notes (`GET /{mint_id}/notes`) and recent activity (`GET /{mint_id}/activity`). Served via `index` generic view with `check_user_exists`. |
| UI-02 | ✅ | Management SPA: "Edit" action opens a dialog pre-filled with the mint's config and submits a PUT (partial update). "Delete" sends DELETE with 409 guard for outstanding notes (user-friendly error banner). API endpoints: `PUT /api/v1/mints/{mint_id}` (require_admin_key), `DELETE /api/v1/mints/{mint_id}` (require_admin_key, 409 guard). Tests: `test_get_notes_returns_outstanding_notes`, `test_get_activity_returns_mint_and_melt_records`. |
| UI-03 | ✅ | Public one-pager at `/lnurlmint/m/{mint_id}` (Vue 3 + Quasar, `static/js/public.vue` + `public.js`): shows mint QR code (LNURL via `lnbits-qrcode` component), mint limits (min_mint_msat, max_mintable_msat fee-aware), node info (alias, color swatch, pubkey with mempool.space/amboss.space links, capacity, channels, peers) or "Node info unavailable", Tor address section (shown when not already on onion), sunset notice (replaces QR when sunsetting). Data from `GET /api/v1/public/{mint_id}` (unauthenticated). Tests: `test_public_mint_info_returns_lnurl_and_limits`, `test_public_mint_info_lnurl_uses_base_url`, `test_public_mint_info_includes_onion_url`, `test_public_mint_info_includes_sunset`, `test_public_mint_info_node_info_null_without_funding_source`, `test_public_mint_info_includes_mint_pubkey`. |
| UI-04 | ✅ | Public one-pager served via `index_public` (no `check_user_exists` dependency) — `views.py` route `/m/{mint_id}`. Public API endpoint `GET /api/v1/public/{mint_id}` has no auth decorator. Management SPA served via `index` with `Depends(check_user_exists)`; all management API endpoints use `require_invoice_key` (GET) or `require_admin_key` (POST/PUT/DELETE). Tests: `test_get_notes_404_for_cross_wallet`, `test_get_activity_404_for_cross_wallet` (wallet scoping), `test_public_mint_info_404_for_unknown_mint`. |

## Test Results

```
66 passed in 25.76s
```

- 44 existing tests (Phase 2 + Phase 3 + Phase 4 + Phase 5) — no regressions
- 22 new Phase 6 tests:
  1. `test_public_base_url_prefers_onion_for_matching_host` — PASSED
  2. `test_public_base_url_ignores_onion_for_other_hosts` — PASSED
  3. `test_public_base_url_ignores_request_when_onion_unset` — PASSED
  4. `test_public_base_url_still_prefers_base_url_over_request` — PASSED
  5. `test_public_base_url_falls_back_to_request_when_base_url_empty` — PASSED
  6. `test_public_base_url_onion_with_empty_base_url` — PASSED
  7. `test_public_base_url_spoofed_onion_host_does_not_trigger` — PASSED
  8. `test_pay_response_uses_onion_url_when_reached_via_onion` — PASSED
  9. `test_pay_response_uses_base_url_for_clearnet` — PASSED
  10. `test_get_notes_returns_outstanding_notes` — PASSED
  11. `test_get_notes_404_for_cross_wallet` — PASSED
  12. `test_get_notes_empty_for_new_mint` — PASSED
  13. `test_get_activity_returns_mint_and_melt_records` — PASSED
  14. `test_get_activity_404_for_cross_wallet` — PASSED
  15. `test_public_mint_info_returns_lnurl_and_limits` — PASSED
  16. `test_public_mint_info_404_for_unknown_mint` — PASSED
  17. `test_public_mint_info_includes_onion_url` — PASSED
  18. `test_public_mint_info_lnurl_uses_base_url` — PASSED
  19. `test_public_mint_info_lnurl_uses_onion_when_on_tor` — PASSED
  20. `test_public_mint_info_includes_sunset` — PASSED
  21. `test_public_mint_info_node_info_null_without_funding_source` — PASSED
  22. `test_public_mint_info_includes_mint_pubkey` — PASSED

## Code Review

**Verdict:** REVIEW WITH FIXES → all fixes applied

- **W1 (fixed):** `get_mint_activity` double-LIMIT merge → replaced with UNION ALL + single ORDER BY ... LIMIT
- **W2 (fixed):** node_info silent except → now logs exception at debug level
- **W3 (non-blocking):** Spoof-proof doc nuance — trusted proxy assumption already documented in docstring
- **W4 (fixed):** `mintId()` fragile path parsing → now prefers `$route.params.mint_id` with regex fallback
- **W5 (non-blocking):** Test coverage gaps noted (spent/pending note states, node_info with real funding source, index_public route) — deferred to Phase 7 (TEST-10)

## Artifacts Produced

| File | Purpose |
|------|---------|
| `services.py` | Onion-aware `_public_base_url` (Tor substitution) |
| `crud.py` | `get_outstanding_notes`, `get_mint_activity` (wallet-scoped JOIN, UNION ALL) |
| `views_api.py` | `api_get_mint_notes`, `api_get_mint_activity`, `api_get_public_mint_info` endpoints + `lnurlmint_public_router` |
| `views.py` | `index_public` route at `/m/{mint_id}` |
| `__init__.py` | `lnurlmint_public_router` registration |
| `static/routes.json` | `PageLnurlmintPublic` route entry |
| `static/js/index.vue` | Full management SPA template |
| `static/js/index.js` | `PageLnurlmint` component (create/edit/delete/notes/activity) |
| `static/js/public.vue` | Public one-pager template (QR, limits, node info, Tor, sunset) |
| `static/js/public.js` | `PageLnurlmintPublic` component |
| `tests/test_onion.py` | 9 Tor substitution tests |
| `tests/test_management_api.py` | 5 management API tests |
| `tests/test_public_api.py` | 8 public API tests |
