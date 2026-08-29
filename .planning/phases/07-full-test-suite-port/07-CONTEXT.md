# Phase 07: Full Test Suite Port - Context

**Gathered:** 2026-08-29
**Status:** Ready for planning

<domain>
## Phase Boundary

The complete `lnurl-mint` test suite is ported and passing against LNbits
fixtures — the behavioral parity acceptance gate.

This phase delivers two pieces:

1. **Bearer threat suite** (Plan 07-01) — Ports
   `test_bearer_threat_suite_poc.py` (TEST-09): the full bearer-asset
   threat suite combining all individual PoC guarantees into one
   integration-level security gate. 8 tests covering routing-node races,
   poll-log replay, callback-log replay control, note-at-rest axiom,
   operator correlation, merge URL budget, and comment-silently-ignored.

2. **Remaining tests** (Plan 07-02) — Ports all remaining test files
   (TEST-10) that are not already ported in prior phases and not N/A
   under the LNbits extension model. This includes the main behavioral
   suite (`test_lnurlcash.py`, 55 tests), verify endpoint tests
   (`test_verify.py`), comment protection tests
   (`test_comment_protection.py`), security review tests
   (`test_surface_hunter_verification.py`, `test_auth_data_hunter_poc.py`),
   mark_pending validation (`test_poc_a3_mark_pending.py`), and
   reconcile tests (`test_reconcile.py`). Tests already ported in
   Phases 2-6 are skipped. Tests that don't apply to the LNbits extension
   model (standalone config, server-rendered HTML, direct lnd/cln REST,
   etc.) are documented as N/A with justification.

</domain>

<decisions>
## Implementation Decisions

### Already-ported tests: skip, don't re-port
The following source test files were ported incrementally in Phases 2-6
and are NOT re-ported in Phase 7:
- `test_melt_restore_double_payout_poc.py` → Phase 2 (TEST-03)
- `test_poc_duplicate_melt.py` → Phase 2 (TEST-01)
- `test_poc_a2_settle_race.py` → Phase 2 (TEST-02)
- `test_poc_f2_pending_info_leak.py` → Phase 2 (TEST-05)
- `test_poc_reconcile_inflight_race.py` → Phase 2 (TEST-04)
- `test_poc_fee_conservation.py` → Phase 3 (TEST-06)
- `test_poc_fee_loop.py` → Phase 3 (TEST-06)
- `test_poc_a1_collision_griefing.py` → Phase 3 (TEST-08)
- `test_poc_verify_race.py` → Phase 4 (TEST-07)
- `test_offline_verification.py` → Phase 5 (SIGN-01–04)
- `test_onion.py` → Phase 6 (TOR-01/TOR-02)

These are listed in the PLAN as "already ported — skip" to document
coverage without duplicating work.

### N/A tests: document justification, don't port
The following source test files are N/A under the LNbits extension model:

- **`test_config.py`** (1 test) — Tests that `BASE_URL` is a required
  env-var setting at startup. The port uses per-mint `base_url` DB rows
  (optional, falls back to `request.base_url`) and LNbits extension
  settings. The concept of "base_url is required at startup" doesn't
  apply.

- **`test_errors.py`** (3 tests) — Tests `log_internal_error` in
  `errors.py` (file-based error logging with unwritable-directory
  fallback). The port uses loguru (LNbits' logger) and FastAPI exception
  handlers. No `errors.py` module exists in the port.

- **`test_frontend.py`** (24 tests) — Tests server-rendered HTML
  (Jinja2 templates: title, QR SVG, node info, explorer links, sunset
  variants, Swagger UI). The port uses a Vue SPA served via LNbits'
  `index`/`index_public` generic views. The API endpoints backing the
  SPA are already tested by `test_public_api.py` (8 tests, Phase 6) and
  `test_management_api.py` (5 tests, Phase 6). The `.well-known/lnurlp/`
  and `.well-known/lnurlw/` Lightning Address routes are deferred to v2.

- **`test_mint_log.py`** (6 tests) — Tests `mint_log.py` (file-based
  mint/melt activity logging with gross/fee/net fields, exactly-once
  logging, unwritable-directory resilience). The port logs via loguru
  and exposes activity via `api_get_mint_activity` (tested in
  `test_management_api.py`). No `mint_log.py` module exists.

- **`test_node.py`** (19 tests) — Tests `node.py`'s direct lnd/cln REST
  client (getinfo, payment status, failure reason mapping, node info
  caching). The port replaces `node.py` with LNbits' `Wallet` abstraction
  (`create_invoice`, `pay_invoice`, `check_transaction_status`). The
  tristate payment status behavior is tested via `FakeNode`/`HodlNode`/
  `InFlightNode` in `conftest.py` (Phases 2-4). The lnd/cln REST
  implementation details don't exist in the port.

- **`test_server.py`** (8 tests) — Tests `server.py`'s lifespan (CORS
  middleware, uvicorn access logger disabling, funding source health
  monitoring with boot check + periodic monitor). The port uses LNbits'
  lifecycle (`lnurlmint_start`/`lnurlmint_stop` +
  `create_permanent_unique_task`). CORS is handled by LNbits core.
  Access logger disabling is an LNbits-level concern. Funding source
  health is replaced by LNbits' wallet health.

- **`test_poc_rpc_census.py`** (8 tests) — Tests per-endpoint
  funding-source RPC counts (create_invoice, is_invoice_settled,
  invoice_preimage, pay_invoice, is_payment_complete, payment_preimage,
  fetch_node_info, sign_message). The port uses LNbits' payment services
  which have different call patterns (no `is_invoice_settled` — lazy
  settlement via `check_payment_status`; no `fetch_node_info` — per-mint
  keypair; no `sign_message` — coincurve `sign_note`). The RPC census
  concept doesn't map to the LNbits model.

### Adaptation pattern: direct endpoint calls, not TestClient
All ported tests follow the established pattern from Phases 2-6:
- `@pytest.mark.anyio` for async tests
- Direct endpoint function calls (e.g., `await get_withdraw(...)`,
  `await get_withdraw_callback(...)`) instead of FastAPI `TestClient`
- `_mock_request()` returns a `MagicMock` with `base_url` set
- `BackgroundTasks()` passed explicitly to callback endpoints
- `node` fixture (FakeNode) and `db_setup` fixture from `conftest.py`
- `mint_note(node, amount)` async helper for minting settled notes
- `fresh_secret()` for (k1, h) pairs
- `fake_invoice(amount)` for BOLT11 invoices
- `mint_note_with_comment(node, amount)` for comment-protected notes

### test_lnurlcash.py adaptation: largest file, broadest coverage
The source `test_lnurlcash.py` (55 tests) is the main behavioral test
suite. It tests payRequest advertisement, pay callback (minting),
withdraw informational endpoint, melt/rotate/split/merge callbacks, fee
math, sunset mode, pending states, k1 validation, and more. Many of
these behaviors are already covered by the ported PoC tests, but the
full suite provides broader edge-case coverage. The adaptation:
- Replace `client.get("/p/cb?amount=...")` with direct
  `await get_pay_callback(...)` calls
- Replace `client.get("/w?k1=...")` with `await get_withdraw(...)`
- Replace `client.get("/w/cb?k1=...&h=...")` with
  `await get_withdraw_callback(...)`
- Replace `notes.note_amount(h)` with `await get_note(note_id)` and
  checking `.amount_msat`
- Replace `monkeypatch.setattr(settings, ...)` with
  `await update_mint(TEST_MINT_ID, TEST_WALLET, ...)`
- The source's `note_value(client, k1)` helper maps to a direct
  `get_withdraw` call checking `maxWithdrawable`

### test_reconcile.py adaptation: function exists, lifecycle differs
The source `test_reconcile.py` (7 tests) tests `reconcile_pending_melts`
and the server lifespan's boot/periodic monitor. The port has
`reconcile_pending_melts` in `services.py` and `boot_reconcile` in
`__init__.py`. The function-level tests (finalize, restore,
leave-pending, single-attempt, error logging) can be ported. The
server-lifespan tests (boot reconciliation via `TestClient(app)`,
periodic monitor) are adapted to call `reconcile_pending_melts` directly
and test `boot_reconcile` as a function call.

### test_verify.py adaptation: endpoint exists, some overlap
The source `test_verify.py` (17 tests) tests the LUD-21 verify endpoint.
The port has `GET /lnurlmint/verify/{mint_id}/{payment_hash}` in
`views_lnurl.py`. Some tests overlap with `test_poc_verify_race.py`
(already ported — 5 tests covering the race aspect). The remaining tests
(verify URL advertisement, settled/unsettled states, preimage
withholding, melt verify, migration tests) need porting. The migration
tests (test_mints_table_migrates_from_before_lud21,
test_melts_table_migrates_from_before_mark_melt_settled) are N/A — the
port uses LNbits' migration framework from the start.

### Claude's Discretion
- Whether to split `test_lnurlcash.py` into multiple test files by
  feature area (mint, melt, rotate, split, merge, fees, sunset) or keep
  it as one large file — keeping it as one file matches the source
  structure and is simpler.
- Whether to port `test_reconcile.py`'s server-lifespan tests as
  function-level tests or skip them — function-level tests are preferred
  (calling `reconcile_pending_melts` directly).
- Exact test function names — follow source names where possible for
  traceability, with `_lnbits` suffix if needed to distinguish.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`tests/conftest.py`** (418 lines) — The shared test fixture file
  with `FakeNode`, `HodlNode`, `InFlightNode`, `_patch_services`,
  `db_setup`, `mint_note`, `mint_note_with_comment`, `fresh_secret`,
  `fake_invoice`. All ported tests reuse these fixtures. The `db_setup`
  fixture drops and re-migrates all tables per test, clears
  `_in_flight_melts`, and creates a test mint with `TEST_MINT_ID`.
- **`TEST_MINT_ID`** / **`TEST_WALLET`** — Constants in `conftest.py`
  used by all ported tests.
- **`mint_note(node, amount_msat)`** — Async helper that mints a settled
  note and returns `(k1, note_id, mint)`. Used by all tests needing a
  pre-existing note.
- **`mint_note_with_comment(node, amount_msat)`** — Async helper that
  mints a comment-protected settled note and returns
  `(victim_secret, comment_hash, note_id, payment_hash, mint)`. Used by
  comment protection and verify tests.
- **`_mock_request()`** — Returns `MagicMock` with `base_url` set to
  `"http://test/"`. Used for direct endpoint calls that need a `Request`
  object.
- **`update_mint(mint_id, wallet_id, **kwargs)`** in `crud.py` — Used to
  change mint settings (fees, sunset, verify_enabled) per-test instead
  of monkeypatching `settings`.
- **`get_payrequest(mint_id, request)`** in `views_lnurl.py` — The
  payRequest endpoint function.
- **`get_pay_callback(mint_id, request, amount, comment)`** in
  `views_lnurl.py` — The pay callback endpoint function.
- **`get_withdraw(mint_id, request, k1)`** in `views_lnurl.py` — The
  informational withdraw endpoint function.
- **`get_withdraw_callback(mint_id, request, background_tasks, k1, ...)`
  in `views_lnurl.py` — The mutating withdraw callback endpoint function.
- **`get_verify(mint_id, payment_hash)`** in `views_lnurl.py` — The
  LUD-21 verify endpoint function.
- **`reconcile_pending_melts()`** in `services.py` — The reconcile
  function (called directly in tests, not via server lifespan).
- **`signing.verify_note(mint_pubkey, h, amount_msat, sig)`** — Test-only
  signature verification (Phase 5).
- **`signing.mint_pubkey(mint)`** — Derives the mint's public key from
  `mint.mint_privkey`.

### Established Patterns
- **Direct endpoint calls** — All ported tests call endpoint functions
  directly with mock `Request` objects, not via `TestClient`. This
  avoids needing a running LNbits server and keeps tests fast.
- **`@pytest.mark.anyio`** — All async tests use this marker (LNbits
  test convention).
- **Per-test DB isolation** — `db_setup` fixture drops and re-migrates
  all tables per test. No test leaks state.
- **`_CONFIRMATION_RETRY_DELAYS_SECONDS=()`** — Monkeypatched to `()` in
  `_patch_services` so confirmation is a single attempt with no sleep.
- **`BackgroundTasks()`** — Passed explicitly to `get_withdraw_callback`
  for the background `_melt_pay` task.
- **`update_mint` for config changes** — Instead of monkeypatching
  `settings`, tests call `await update_mint(TEST_MINT_ID, TEST_WALLET,
  base_fee_msat=1000, ...)` to change mint config per-test.

### Integration Points
- **`tests/test_bearer_threat_suite.py`** — New test file (Plan 07-01).
- **`tests/test_lnurlcash.py`** — New test file (Plan 07-02).
- **`tests/test_verify.py`** — New test file (Plan 07-02).
- **`tests/test_comment_protection.py`** — New test file (Plan 07-02).
- **`tests/test_surface_hunter.py`** — New test file (Plan 07-02).
- **`tests/test_auth_data_hunter.py`** — New test file (Plan 07-02).
- **`tests/test_mark_pending.py`** — New test file (Plan 07-02).
- **`tests/test_reconcile.py`** — New test file (Plan 07-02).
- **`tests/conftest.py`** — May need new helpers (e.g.,
  `note_value` equivalent, `melt_in_background` equivalent for async).

</code_context>

<specifics>
## Specific Ideas

- The bearer threat suite (Plan 07-01) is the integration-level security
  gate. Its 8 tests combine all individual PoC guarantees: T2
  (routing-node race, no-comment fallback), T2b (comment-protected
  defeats the race), T3 (informational poll leaks live note — documents
  current behavior, INVERTS WHEN option D), T4 (callback log replay
  fails — control), T5 (note at rest is cash — control), T6 (operator
  correlation), T10 (merge URL budget), T9 (comment silently ignored —
  documents option B landing). These tests use both `mint_note` and
  `mint_note_with_comment` helpers and test the full rotate/melt flow
  via direct endpoint calls.

- `test_lnurlcash.py` is the largest single test file (55 tests). It
  covers the full mint/melt/rotate/split/merge lifecycle, fee math,
  sunset mode, pending states, k1 validation, and edge cases. Many
  behaviors are already covered by ported PoCs, but the full suite
  provides broader coverage (e.g., `test_split_ignores_min_mint_msat`,
  `test_merge_refunds_base_fee_for_every_extra_note`,
  `test_melt_rejects_own_pending_invoice`,
  `test_ambiguously_failed_payment_that_actually_succeeded_does_not_restore`).

- The `note_value` helper from the source (reads a note's value via
  GET /w) maps to calling `get_withdraw` and checking `maxWithdrawable`.
  A similar async helper should be added to `conftest.py` or inline in
  the test file.

- The `melt_in_background` pattern from the source (threading-based) is
  replaced by the `InFlightNode` fixture (asyncio.Event-based) in the
  port's `conftest.py`. Tests needing the pending window use
  `inflight_node` fixture instead of threads.

- `test_reconcile.py`'s `_leave_a_note_pending` helper creates a note
  that's pending due to unconfirmable payment status. The port version
  uses `HodlNode` with `fail_payments=True` and
  `is_payment_complete_raises=True` (or `check_transaction_status`
  returning `PaymentPendingStatus`).

</specifics>

<deferred>
## Deferred Ideas

- **Lightning Address tests** — The source's `.well-known/lnurlp/` and
  `.well-known/lnurlw/` tests are deferred to v2 (Lightning Address
  requires lnurlp extension PR).
- **Node info caching tests** — The source's `cached_fetch_node_info`
  TTL tests are N/A (the port doesn't have a node info cache; node info
  is fetched via `get_funding_source()` on each call or returned as null
  for FakeWallet).
- **Swagger UI / docs page tests** — The source serves self-hosted
  Swagger UI. LNbits has its own API docs infrastructure.
- **CORS tests** — Handled by LNbits core, not the extension.
- **Uvicorn access logger disabling** — LNbits-level concern, not
  extension-level.
- **Migration tests** — The source tests hand-migrating from pre-LUD-21
  and pre-mark_melt_settled schemas. The port uses LNbits' migration
  framework from the start; no hand-migration needed.

</deferred>
