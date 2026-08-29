# Phase 07: Full Test Suite Port - Research

**Researched:** 2026-08-29
**Status:** Complete

## Source Test Suite Census

The source `lnurl-mint` test suite lives at `~/lnurl-mint/tests/` (26 test
files, 237 total test functions). The ported test suite lives at
`/home/exedev/lnurlmint/tests/` (13 test files, 64 test functions as of
Phase 6 completion).

### Per-File Breakdown

| Source File | Tests | Status | Ported As | Ported Tests |
|-------------|-------|--------|-----------|-------------|
| test_bearer_threat_suite_poc.py | 8 | **TO PORT (07-01)** | — | — |
| test_lnurlcash.py | 55 | **TO PORT (07-02)** | — | — |
| test_verify.py | 17 | **TO PORT (07-02)** | — | — |
| test_comment_protection.py | 11 | **TO PORT (07-02)** | — | — |
| test_surface_hunter_verification.py | 5 | **TO PORT (07-02)** | — | — |
| test_reconcile.py | 7 | **TO PORT (07-02)** | — | — |
| test_auth_data_hunter_poc.py | 3 | **TO PORT (07-02)** | — | — |
| test_poc_a3_mark_pending.py | 3 | **TO PORT (07-02)** | — | — |
| test_melt_restore_double_payout_poc.py | 3 | Already ported (Phase 2) | test_melt_restore_double_payout_poc.py | 4 |
| test_poc_duplicate_melt.py | 2 | Already ported (Phase 2) | test_poc_duplicate_melt.py | 1 |
| test_poc_a2_settle_race.py | 3 | Already ported (Phase 2) | test_poc_a2_settle_race.py | 1 |
| test_poc_f2_pending_info_leak.py | 2 | Already ported (Phase 2) | test_poc_f2_pending_info_leak.py | 1 |
| test_poc_reconcile_inflight_race.py | 3 | Already ported (Phase 2) | test_poc_reconcile_inflight_race.py | 1 |
| test_poc_fee_conservation.py | 9 | Already ported (Phase 3) | test_poc_fee_conservation.py | 9 |
| test_poc_fee_loop.py | 7 | Already ported (Phase 3) | test_poc_fee_loop.py | 6 |
| test_poc_a1_collision_griefing.py | 4 | Already ported (Phase 3) | test_poc_a1_collision_griefing.py | 4 |
| test_poc_verify_race.py | 5 | Already ported (Phase 4) | test_poc_verify_race.py | 5 |
| test_offline_verification.py | 13 | Already ported (Phase 5) | test_offline_verification.py | 10 |
| test_onion.py | 8 | Already ported (Phase 6) | test_onion.py | 9 |
| test_config.py | 1 | **N/A** | — | — |
| test_errors.py | 3 | **N/A** | — | — |
| test_frontend.py | 24 | **N/A** | — | — |
| test_mint_log.py | 6 | **N/A** | — | — |
| test_node.py | 19 | **N/A** | — | — |
| test_server.py | 8 | **N/A** | — | — |
| test_poc_rpc_census.py | 8 | **N/A** | — | — |

**Summary:**
- Total source tests: 237
- Already ported (Phases 2-6): 59 source tests → 51 ported test functions
- N/A (LNbits extension model): 69 tests across 7 files
- **To port in Phase 7: 109 tests across 8 files**

### Additional ported tests (not from source)
- `test_management_api.py` (5 tests) — Phase 6, new for LNbits management API
- `test_public_api.py` (8 tests) — Phase 6, new for LNbits public API

---

## Detailed Research: Files To Port

### 1. test_bearer_threat_suite_poc.py (8 tests) — Plan 07-01

**Location:** `~/lnurl-mint/tests/test_bearer_threat_suite_poc.py` (298 lines)

**Purpose:** Adversarial threat-suite for the bearer-note transport/exposure
options in the LUD-25 design debate. One executable scenario per scorecard
row, measuring candidate fixes against the same attacks. This is the
integration-level security gate (TEST-09).

**Test functions:**

1. `test_t2_routing_node_race_p_alone_is_sufficient` — A routing node
   learns the preimage P as the settling HTLC propagates back. In the
   no-comment fallback, P alone redeems, so the attacker rotates the note
   before the payer's wallet. **Documents current vulnerable behavior**
   for no-comment mints.

2. `test_t2b_comment_protected_note_defeats_the_routing_node_race` — A
   WALLET that attaches LUD-25 comment protection is immune: P was never
   the note's k1. The routing node's rotate attempt fails; the
   WALLET-held secret redeems normally.

3. `test_t3_informational_poll_leaks_the_live_note` — Checking a note's
   value via GET /w?k1=<live bearer secret> leaves the spendable k1 in
   logs. An attacker reading the logged URL can rotate the note.
   **INVERTS WHEN option D (hash-keyed informational GET) lands.**

4. `test_t4_callback_log_replay_fails_control` — **Control test**: a k1
   captured from a MUTATING callback's URL was burned by the very
   request it rode in on, so replaying it can never work. Must hold
   under every option.

5. `test_t5_note_at_rest_is_cash_control` — **Control test**: the bearer
   axiom — a note URL in a chat log/screenshot/QR IS the money. Expected
   to "fail" under every option forever. Pinned so the scorecard's
   all-minus row stays deliberate.

6. `test_t6_operator_can_link_rotate_to_later_spend` — At rotate time
   WALLET discloses h = sha256(new_k1), and the mint keys its storage by
   exactly that h — so a later spend of new_k1 links to issuance. Only
   blinded signatures (option E) win this row.

7. `test_t10_merge_url_budget_plaintext_fits_encrypted_does_not` — Pure
   URL arithmetic: 25 plaintext k1s fit in a GET URL; 25 encrypted blobs
   (option C) don't. No endpoints involved.

8. `test_t9_comment_is_silently_ignored_today` — A malformed comment
   falls back cleanly to the preimage-keyed note, but verify is NOT
   advertised (unlike the old silent-downgrade behavior). **Documents
   option B landing.**

**Fixtures needed:** `client`, `node`, `mint_note`, `fresh_secret`,
`monkeypatch` (for `settings.verify_enabled`).

**Adaptation for LNbits:**
- Replace `client.get("/w/cb?k1=...&h=...")` with
  `await get_withdraw_callback(TEST_MINT_ID, _mock_request(),
  BackgroundTasks(), k1=[k1], h=h)`
- Replace `client.get("/w?k1=...")` with
  `await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)`
- Replace `client.get("/p/cb?amount=...&comment=...")` with
  `await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=...,
  comment=...)`
- Replace `notes.note_amount(h)` with `await get_note(h)` and check
  `.amount_msat`
- T2b needs `mint_note_with_comment` helper (already in conftest.py)
- T9 needs `verify_enabled` toggle via `update_mint` instead of
  monkeypatching `settings`
- T10 is pure URL arithmetic — no adaptation needed (no endpoint calls)

---

### 2. test_lnurlcash.py (55 tests) — Plan 07-02

**Location:** `~/lnurl-mint/tests/test_lnurlcash.py` (710 lines)

**Purpose:** The main behavioral test suite. Tests the full
mint/melt/rotate/split/merge lifecycle, fee math, sunset mode, pending
states, k1 validation, and edge cases.

**Test functions (grouped by feature):**

**PayRequest + pay callback (minting):**
- `test_pay_request_advertises_withdraw_link` — payRequest has
  withdrawLink, minSendable <= maxSendable
- `test_paid_invoice_preimage_becomes_a_bearer_note` — preimage becomes
  note after settlement; informational GET never consumes
- `test_pay_callback_advertises_the_lnaddress_as_not_disposable` —
  disposable: false (LUD-11)
- `test_pay_callback_enforces_sendable_bounds` — rejects too-low/too-high
  amounts
- `test_pay_callback_rejects_while_sunsetting` — sunset rejects /p/cb
- `test_pay_response_omits_mint_fee_when_free` — no "Mint fees:" in
  metadata when fees are 0
- `test_pay_response_advertises_mint_fee_when_configured` — "Mint fees:
  base,ppm" in metadata
- `test_pay_response_advertises_fee_inclusive_min_sendable` — minSendable
  is fee-aware (raised so paying minimum nets min_mint_msat)
- `test_mint_credits_note_net_of_configured_fee` — note value = amount -
  fee (rounded up to sat)
- `test_mint_fee_rounds_up_to_the_nearest_sat` — 0.1 sat fee rounds up
  to 1 sat
- `test_pay_callback_rejects_amount_that_cannot_cover_the_fee` — amount
  too low after fee
- `test_pay_callback_rejects_amount_below_min_mint` — amount clears
  min_sendable but not min_mint after fee
- `test_mint_succeeds_at_exactly_min_mint` — boundary: exactly min_mint
  succeeds

**Withdraw callback URL + verify URL (Host-header spoofing):**
- `test_withdraw_callback_url_ignores_a_spoofed_host_header` — callback
  URL from settings.base_url, not req.url_for
- `test_verify_url_ignores_a_spoofed_host_header` — verify URL from
  settings.base_url

**Rotate:**
- `test_rotate_burns_and_replaces_the_note` — old note burned, new note
  same value, no k1 in response

**Split:**
- `test_split_mints_amount_and_change` — amount + change = total
- `test_split_merges_multiple_k1s_first` — split with multiple k1s
  merges then splits
- `test_split_rejects_amount_out_of_range` — 0, total, >total rejected
- `test_split_rejects_while_sunsetting` — sunset rejects split
- `test_split_deducts_base_fee_from_change_when_mint_charges_fees` —
  base_fee from change, not amount
- `test_split_does_not_reapply_fee_percent_ppm` — fee_percent only at
  mint time
- `test_split_rejects_when_change_cannot_cover_the_base_fee` —
  insufficient change
- `test_split_rejects_a_zero_value_change_note` — change == base_fee
  rejected (no 0-value note)
- `test_split_ignores_min_mint_msat_on_both_sides` — min_mint is /p/cb
  only, not split

**Merge:**
- `test_merge_burns_all_and_mints_the_sum` — sum of inputs
- `test_merge_refunds_base_fee_for_every_extra_note` — (n-1)*base_fee
  refund
- `test_rotate_is_unaffected_by_mint_fees` — rotate = merge of 1, no
  refund

**Melt:**
- `test_melt_pays_invoice_of_exactly_the_notes_value` — pays invoice,
  note burned, no new note
- `test_melt_fee_limit_defaults_to_the_baseline_when_mint_fee_is_low` —
  max(0.5%, 5000) baseline
- `test_melt_fee_limit_follows_a_higher_configured_mint_fee` — mint fee
  > baseline → higher budget
- `test_melt_rejects_multiple_k1s` — pr + multiple k1s rejected
- `test_melt_rejects_invoice_of_wrong_amount` — pr amount != note value
- `test_failed_payment_restores_the_notes` — PaymentFailed → restore
- `test_pending_note_rejects_concurrent_operations` — pending → "pending"
  reason
- `test_pending_note_is_released_if_the_payment_fails` — failed payment
  releases pending
- `test_payment_failed_still_confirms_before_restoring` — PaymentFailed
  still calls is_payment_complete (confirm-before-burn)
- `test_pending_note_is_released_if_funding_source_becomes_unavailable` —
  no funding source → ERROR, note not stuck pending
- `test_melt_rejects_own_pending_invoice` — self-payment rejected (SEC-06)
- `test_melt_rejects_already_settled_own_invoice` — settled own invoice
  also rejected
- `test_ambiguously_failed_payment_that_actually_succeeded_does_not_restore` —
  pay raised but payment completed → burn, NOT restore
- `test_undeterminable_payment_status_leaves_the_note_pending` —
  paid=None → pending
- `test_hodl_invoice_attack_leaves_the_note_pending_instead_of_restoring` —
  hodl invoice → pending (not restored)
- `test_undeterminable_payment_status_retries_before_giving_up` —
  _confirm_payment retries before pending
- `test_rotate_merge_and_melt_are_unaffected_by_sunsetting` — sunset
  allows rotate/merge/melt

**Validation + edge cases:**
- `test_any_invalid_k1_fails_the_whole_request` — one bad k1 aborts all
- `test_duplicate_k1_cannot_be_double_counted` — duplicate k1 rejected
- `test_too_many_k1s_is_rejected` — max_k1s limit
- `test_amount_cannot_be_combined_with_pr` — amount + pr rejected
- `test_withdraw_response_echoes_the_literal_secret` — k1 echoed verbatim
- `test_withdraw_requires_k1` — missing k1 → ERROR
- `test_withdraw_reports_unknown_k1_distinctly_from_spent` — "Unknown
  note." vs "Note already spent."
- `test_withdraw_ignores_the_declared_amount` — amount param ignored,
  maxWithdrawable authoritative
- `test_no_bearer_secret_is_ever_persisted` — only hashes stored, never
  raw secrets
- `test_spent_k1_cannot_be_replayed` — spent k1 → ERROR

**Adaptation for LNbits:**
- All `client.get(...)` calls → direct endpoint function calls
- `notes.note_amount(h)` → `await get_note(h)` + check `.amount_msat`
- `monkeypatch.setattr(settings, ...)` → `await update_mint(...)`
- `note_value(client, k1)` helper → async `note_value(k1)` calling
  `get_withdraw` and checking `maxWithdrawable`
- `melt_in_background` → `inflight_node` fixture or asyncio equivalent
- `node.last_fee_limit_msat` → FakeNode already tracks this (check
  conftest.py's FakeNode.pay_invoice — it doesn't currently expose
  `last_fee_limit_msat`; may need to add this to conftest.py's FakeNode)
- `node.is_payment_complete_called` → check FakeNode's
  `check_transaction_status` call count (may need to add tracking)
- `settings.max_k1s` → `_MAX_K1S` constant in `views_lnurl.py`

**Overlap with existing ported PoCs:**
- Melt tristate (paid=True/False/None) → covered by
  test_melt_restore_double_payout_poc.py (4 tests)
- Duplicate melt → covered by test_poc_duplicate_melt.py (1 test)
- Settle race → covered by test_poc_a2_settle_race.py (1 test)
- Pending info leak → covered by test_poc_f2_pending_info_leak.py (1 test)
- Reconcile in-flight → covered by test_poc_reconcile_inflight_race.py (1 test)
- Fee conservation → covered by test_poc_fee_conservation.py (9 tests)
- Fee loop → covered by test_poc_fee_loop.py (6 tests)
- Collision griefing → covered by test_poc_a1_collision_griefing.py (4 tests)

Despite overlap, the full suite provides broader edge-case coverage
(split fee arithmetic, merge fee refund, self-payment rejection,
Host-header spoofing, k1 validation, etc.) that the PoCs don't cover.

---

### 3. test_verify.py (17 tests) — Plan 07-02

**Location:** `~/lnurl-mint/tests/test_verify.py` (265 lines)

**Purpose:** Tests the LUD-21 verify endpoint (mint and melt directions).

**Test functions:**

**Verify URL advertisement:**
- `test_verify_url_absent_by_default` — verify_enabled=false → no verify
  in /p/cb response
- `test_verify_url_advertised_when_enabled` — verify_enabled=true +
  comment → verify URL in response
- `test_verify_url_absent_without_comment` — verify_enabled=true but no
  comment → no verify (preimage IS the secret)

**Verify settlement status:**
- `test_verify_reports_unsettled_before_payment` — settled=false, no
  preimage
- `test_verify_reports_settled_after_payment` — settled=true, preimage
  served
- `test_verify_withholds_the_preimage_before_settlement` — preimage not
  in response body before settlement
- `test_verify_unknown_payment_hash_is_not_found` — unknown hash → "Not
  found"
- `test_verify_stays_settled_after_the_note_is_spent` — verify reports
  settled even after note is rotated (LUD-21 answers "was this invoice
  ever paid")

**Verify off-switch:**
- `test_verify_endpoint_is_disabled_entirely_when_verify_enabled_is_false` —
  VERIFY_ENABLED=false → 404 even with known payment_hash

**Melt verify:**
- `test_melt_response_carries_no_verify_by_default` — no verify in melt
  response when disabled
- `test_melt_response_carries_pr_and_verify_url_when_enabled` — melt
  response includes pr + verify when enabled
- `test_melt_verify_reports_settled_and_a_matching_preimage_once_paid` —
  melt verify serves preimage (harmless — melt preimage is not a bearer
  secret)
- `test_melt_verify_reports_unsettled_while_genuinely_pending` —
  in-flight melt → settled=false
- `test_melt_verify_reports_settled_immediately_once_finalized_even_if_the_node_lags` —
  settled from local state, not re-asked from funding source
- `test_melt_verify_is_also_disabled_when_verify_enabled_is_false` —
  melt verify also 404s when disabled

**Migration tests (N/A for port):**
- `test_mints_table_migrates_from_before_lud21` — hand-migration from
  pre-LUD-21 schema. N/A — port uses LNbits migrations from start.
- `test_melts_table_migrates_from_before_mark_melt_settled` —
  hand-migration from pre-settled schema. N/A — same reason.

**Adaptation for LNbits:**
- `client.get("/verify/{payment_hash}")` → `await get_verify(TEST_MINT_ID,
  payment_hash)`
- `client.get("/p/cb?...")` → `await get_pay_callback(...)`
- `client.get("/w/cb?...")` → `await get_withdraw_callback(...)`
- `monkeypatch.setattr(settings, "verify_enabled", True)` →
  `await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)`
- Migration tests: skip (N/A)
- `node.last_preimage` → `node.preimages[payment_hash]` (FakeNode stores
  preimages by payment_hash)
- `node.melt_preimages` → need to add melt preimage tracking to
  conftest.py's FakeNode (or use `get_standalone_payment` mock)

**Overlap with existing ported PoCs:**
- Verify race (no-comment → 404, comment → served, disabled → 404) →
  covered by test_poc_verify_race.py (5 tests). But test_verify.py has
  broader coverage (unsettled states, melt verify, settled-after-spend).

---

### 4. test_comment_protection.py (11 tests) — Plan 07-02

**Location:** `~/lnurl-mint/tests/test_comment_protection.py` (158 lines)

**Purpose:** Tests LUD-25 comment protection mechanics: valid/invalid/absent
comment behavior, informational-GET resolution by secret, commentAllowed
advertisement, comment-hash collisions.

**Test functions:**

- `test_pay_response_advertises_comment_allowed` — commentAllowed >= 64
- `test_valid_comment_credits_the_note_under_the_secret_not_the_preimage` —
  note resolves under secret, not preimage
- `test_valid_comment_note_redeems_normally_by_secret` — comment-protected
  note rotates normally
- `test_missing_comment_falls_back_to_preimage_keyed_note` — no comment
  → preimage-keyed note
- `test_malformed_comment_falls_back_to_preimage_keyed_note` — non-hex32
  comment → preimage-keyed note
- `test_verify_advertised_only_with_a_valid_comment` — verify only with
  valid hex32 comment
- `test_informational_get_lazily_settles_a_comment_protected_mint_without_verify` —
  /w?k1=secret lazily materializes comment-protected note
- `test_unsettled_comment_protected_mint_is_not_yet_a_note` — unsettled
  → "Unknown note."
- `test_comment_colliding_with_an_outstanding_note_is_rejected` —
  comment hash == existing note id → "comment already in use"
- `test_comment_colliding_with_another_pending_mint_is_rejected` —
  duplicate comment → "comment already in use"
- `test_comment_protected_note_can_split_rotate_and_merge_like_any_other` —
  comment-protected note supports all redeem operations

**Adaptation for LNbits:**
- Uses `mint_note_with_comment` helper (already in conftest.py)
- `client.get("/p/cb?...&comment=...")` → `await get_pay_callback(...,
  comment=comment)`
- `notes.note_amount(h)` → `await get_note(h)` + check `.amount_msat`
- `notes.pending_mint(ph)` → `await get_pending_mint_record(ph)`
- `notes.mint_settled(ph)` → check note exists via `get_note`

---

### 5. test_surface_hunter_verification.py (5 tests) — Plan 07-02

**Location:** `~/lnurl-mint/tests/test_surface_hunter_verification.py` (202
lines)

**Purpose:** Regression tests from the surface lane of the 2026-08-17
security review.

**Test functions:**

- `test_p3_rotate_onto_pending_mint_is_rejected` — attacker rotates onto
  victim's pending mint payment_hash → rejected atomically; victim's
  mint materializes. (Overlaps with test_poc_a1_collision_griefing.py
  but tests the canonical case.)
- `test_p1_verify_no_longer_hands_out_the_no_comment_fallback_secret` —
  verify refuses preimage for no-comment mints. (Overlaps with
  test_poc_verify_race.py but tests the full rotate-after-verify flow.)
- `test_p1b_verify_is_harmless_once_comment_protection_is_used` — verify
  serves preimage for comment-protected mints, but stolen preimage
  redeems nothing.
- `test_p2_rpc_amplification_getinfo_now_cached_mint_pubkey_still_not` —
  RPC caching census. **Partially N/A** — the port doesn't have
  `cached_fetch_node_info` (node info is via `get_funding_source()` or
  null for FakeWallet). The mint_pubkey caching aspect is N/A (per-mint
  keypair, no RPC). This test may be skipped or heavily adapted.
- `test_p6_pathological_ppm_raises_instead_of_hanging` — fee_percent_ppm
  beyond bound → RuntimeError. (Overlaps with test_poc_fee_loop.py but
  tests the threading/timeout aspect.)

**Adaptation for LNbits:**
- P3: `await get_withdraw_callback(...)` with h=victim_ph → check ERROR
- P1/P1b: `await get_verify(...)` + `await get_withdraw_callback(...)`
- P2: **N/A or heavily adapted** — the RPC census concept doesn't map.
  The mint_pubkey derivation is local (coincurve), not an RPC. Skip or
  replace with a test that mint_pubkey is derived locally (no funding
  source call).
- P6: `_min_sendable_msat` is in `services.py` — test directly with
  pathological ppm values. The threading/timeout aspect may be
  simplified (the function is synchronous, so a timeout isn't needed
  if it raises immediately).

---

### 6. test_auth_data_hunter_poc.py (3 tests) — Plan 07-02

**Location:** `~/lnurl-mint/tests/test_auth_data_hunter_poc.py` (106 lines)

**Purpose:** Regression tests from the auth-data lane of the 2026-08-17
security review.

**Test functions:**

- `test_f1_verify_disclosure_requires_verify_enabled` —
  VERIFY_ENABLED=false → /verify 404s, no preimage leaked. (Overlaps
  with test_poc_verify_race.py.)
- `test_f3_withdraw_rejects_pending_note_with_spec_reason` — pending
  note → /w rejects with "pending" reason. (Overlaps with
  test_poc_f2_pending_info_leak.py but tests the threading-based
  in-flight window.)
- `test_f4_rotate_onto_pending_mint_rejected_victim_unharmed` —
  rotate onto pending mint → rejected; victim's mint materializes.
  (Overlaps with test_poc_a1_collision_griefing.py and
  test_surface_hunter_verification.py's P3.)

**Adaptation for LNbits:**
- F1: `await get_verify(...)` with verify_enabled=False → check 404
- F3: Use `inflight_node` fixture instead of threading. Melt via
  `get_withdraw_callback`, then concurrent `get_withdraw` → "pending"
- F4: `await get_withdraw_callback(...)` with h=victim_ph → check ERROR

**Overlap note:** All 3 tests overlap significantly with existing ported
PoCs. They are ported for completeness and to document the security
review coverage, but the core guarantees are already locked by prior
PoC tests.

---

### 7. test_poc_a3_mark_pending.py (3 tests) — Plan 07-02

**Location:** `~/lnurl-mint/tests/test_poc_a3_mark_pending.py` (127 lines)

**Purpose:** PoC for A3 (auth-data lane): "NoteStore.mark_pending validates
only k1s[0]; later k1s go unvalidated." **FALSIFIED** — mark_pending
validates every note_id at any position.

**Test functions:**

- `test_a3_http_melt_rejects_multiple_k1s_before_any_reservation` — melt
  with multiple k1s + pr → rejected before any reservation
- `test_a3_mark_pending_validates_every_id_at_any_position` — garbage id
  at any position aborts the whole reservation; spent id aborts; pending
  id aborts with PendingNoteError
- `test_a3_finalize_and_restore_on_never_reserved_ids_are_noops_for_unknown_ids` —
  finalize_melt/restore on unknown ids are silent no-ops; control shows
  finalize_melt WILL burn a never-reserved outstanding note (code
  fragility note, not a vulnerability)

**Adaptation for LNbits:**
- `notes.mark_pending([ids], ph)` → `await mark_pending([ids], ph,
  mint_id)` (port's signature includes mint_id)
- `notes.finalize_melt([ids])` → `await finalize_melt([ids])`
- `notes.restore([ids])` → `await restore([ids])`
- `notes.conn.execute(...)` → `await db.execute(...)` for direct DB
  inspection
- `PendingNoteError` → imported from `lnurlmint.crud`
- The HTTP-level test (multiple k1s + pr) → `await
  get_withdraw_callback(...)` with k1=[k1a, k1b], pr=pr → check ERROR

---

### 8. test_reconcile.py (7 tests) — Plan 07-02

**Location:** `~/lnurl-mint/tests/test_reconcile.py` (162 lines)

**Purpose:** Tests `reconcile_pending_melts` and the server lifespan's
boot/periodic monitor.

**Test functions:**

- `test_reconcile_finalizes_a_pending_note_once_confirmed_paid` —
  pending note → reconcile confirms paid → burned
- `test_reconcile_restores_a_pending_note_once_confirmed_not_paid` —
  pending note → reconcile confirms not paid → restored
- `test_reconcile_leaves_still_unconfirmable_notes_pending_without_retrying` —
  unconfirmable → stays pending, single attempt (no retry)
- `test_reconcile_writes_still_unconfirmed_notes_to_error_log` —
  unconfirmable → error log with "still unconfirmed at boot" (no k1 in
  log)
- `test_app_boot_reconciles_a_note_left_pending_by_a_previous_process` —
  server lifespan boot calls reconcile. **Partially N/A** — port uses
  `boot_reconcile()` function, not server lifespan. Adapt to call
  `boot_reconcile()` directly.
- `test_periodic_monitor_reconciles_a_note_that_resolves_after_boot` —
  periodic monitor picks up notes that resolve after boot. **Partially
  N/A** — port uses `run_interval` in `tasks.py`. Adapt to call
  `reconcile_pending_melts()` directly (the periodic aspect is wired by
  `create_permanent_unique_task`, tested separately).
- `test_periodic_monitor_survives_a_reconcile_failure` — uncaught
  exception doesn't kill the background loop. **Partially N/A** — port's
  `run_interval` handles this. Adapt to test that
  `reconcile_pending_melts` raising doesn't crash the task.

**Adaptation for LNbits:**
- `_leave_a_note_pending` helper → use `HodlNode` with
  `fail_payments=True` + `is_payment_complete_raises=True` (or
  `check_transaction_status` returning `PaymentPendingStatus`)
- `asyncio.run(router_module.reconcile_pending_melts(...))` →
  `await reconcile_pending_melts()` (port's function takes no funding
  source arg — it uses the module-level payment imports)
- `server_module.reconcile_pending_melts` → `services_module.reconcile_pending_melts`
- `TestClient(app)` lifespan tests → call `boot_reconcile()` or
  `reconcile_pending_melts()` directly
- Error log test → use loguru sink capture (as in
  test_offline_verification.py's `test_signing_failure_is_still_logged`)

---

## Detailed Research: N/A Files

### test_config.py (1 test)
- `test_base_url_is_required` — Tests that `Settings()` raises
  `ValidationError` when `BASE_URL` env var is unset. The port uses
  per-mint `base_url` DB rows (optional, defaults to `""`, falls back
  to `request.base_url`). LNbits extension settings don't have a
  required `BASE_URL`. **N/A — covered by LNbits extension model.**

### test_errors.py (3 tests)
- Tests `log_internal_error` in `errors.py` (file-based error logging).
  The port uses loguru (LNbits' logger) and FastAPI exception handlers.
  No `errors.py` module exists. **N/A — covered by loguru + FastAPI
  exception handlers.**

### test_frontend.py (24 tests)
- Tests server-rendered HTML (Jinja2 templates): title, description, QR
  SVG, lightning address, node info, explorer links, capacity/limits,
  sunset variants, Swagger UI, OpenAPI version. The port uses a Vue SPA
  served via LNbits' `index`/`index_public` generic views. The API
  endpoints backing the SPA are tested by `test_public_api.py` (8 tests)
  and `test_management_api.py` (5 tests). The `.well-known/lnurlp/` and
  `.well-known/lnurlw/` Lightning Address routes are deferred to v2.
  **N/A — covered by Vue SPA + API endpoint tests (Phase 6).**

### test_mint_log.py (6 tests)
- Tests `mint_log.py` (file-based mint/melt activity logging with
  gross/fee/net fields, exactly-once logging, unwritable-directory
  resilience). The port logs via loguru and exposes activity via
  `api_get_mint_activity` (tested in `test_management_api.py`). No
  `mint_log.py` module exists. **N/A — covered by loguru + activity API
  tests.**

### test_node.py (19 tests)
- Tests `node.py`'s direct lnd/cln REST client: getinfo, payment status
  (lnd SUCCEEDED/FAILED/IN_FLIGHT, cln complete/failed/pending), failure
  reason mapping, node info caching (TTL, failure not cached). The port
  replaces `node.py` with LNbits' `Wallet` abstraction. The tristate
  payment status behavior is tested via `FakeNode`/`HodlNode`/
  `InFlightNode` in `conftest.py` (Phases 2-4). **N/A — replaced by
  LNbits wallet abstraction; tristate behavior tested via FakeNode
  fixtures.**

### test_server.py (8 tests)
- Tests `server.py`'s lifespan: CORS middleware, uvicorn access logger
  disabling (SEC-05), funding source health monitoring (boot check,
  periodic monitor, recovery logging, no-repeat-same-state). The port
  uses LNbits' lifecycle (`lnurlmint_start`/`lnurlmint_stop` +
  `create_permanent_unique_task`). CORS is handled by LNbits core.
  Access logger disabling is an LNbits-level concern. **N/A — covered
  by LNbits lifecycle.**

### test_poc_rpc_census.py (8 tests)
- Tests per-endpoint funding-source RPC counts (create_invoice,
  is_invoice_settled, invoice_preimage, pay_invoice, is_payment_complete,
  payment_preimage, fetch_node_info, sign_message). The port uses
  LNbits' payment services with different call patterns (no
  `is_invoice_settled` — lazy settlement via `check_payment_status`; no
  `fetch_node_info` — per-mint keypair; no `sign_message` — coincurve
  `sign_note`). **N/A — RPC patterns differ in LNbits model.**

---

## Adaptation Patterns Summary

### Common patterns for all ported tests:
1. **`@pytest.mark.anyio`** — All async tests use this marker
2. **Direct endpoint calls** — `await get_withdraw(...)`, `await
   get_withdraw_callback(...)`, `await get_pay_callback(...)`, `await
   get_verify(...)` instead of `TestClient`
3. **`_mock_request()`** — `MagicMock` with `base_url` set for Request
   objects
4. **`BackgroundTasks()`** — Passed explicitly to `get_withdraw_callback`
5. **`node` / `db_setup` fixtures** — From `conftest.py`, provide
   FakeNode + fresh DB per test
6. **`mint_note(node, amount)`** — Async helper for minting settled notes
7. **`mint_note_with_comment(node, amount)`** — For comment-protected notes
8. **`fresh_secret()`** — (k1, h) pair for rotate/split/merge
9. **`fake_invoice(amount)`** — BOLT11 invoice for melt targets
10. **`update_mint(...)` instead of `monkeypatch.setattr(settings, ...)`** —
    Change mint config per-test via DB update

### Conftest.py additions needed:
- **`note_value(k1)` async helper** — Calls `get_withdraw` and returns
  `maxWithdrawable` or `None` (replaces source's `note_value` helper)
- **FakeNode: `last_fee_limit_msat` tracking** — The port's FakeNode
  doesn't currently expose `last_fee_limit_msat` for fee limit tests.
  May need to add this to `FakeNode.pay_invoice`.
- **FakeNode: `check_transaction_status` call count** — For tests
  asserting a bounded number of confirmation attempts. May need to add
  a counter.
- **`_leave_a_note_pending` async helper** — Creates a pending note via
  HodlNode with unconfirmable payment status (for reconcile tests)

### Key import mapping:
| Source | Port |
|--------|------|
| `from lnurl_mint.config import settings` | `from lnurlmint.crud import update_mint` |
| `from lnurl_mint.db import notes` | `from lnurlmint.crud import get_note, mark_pending, finalize_melt, restore` |
| `from lnurl_mint.router import ...` | `from lnurlmint.views_lnurl import ...` + `from lnurlmint.services import ...` |
| `from lnurl_mint.signing import verify_note` | `from lnurlmint.signing import verify_note, mint_pubkey` |
| `from tests.conftest import fresh_secret` | `from lnurlmint.tests.conftest import fresh_secret` |
| `client.get("/w?k1=...")` | `await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)` |
| `client.get("/w/cb?k1=...&h=...")` | `await get_withdraw_callback(TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], h=h)` |
| `client.get("/p/cb?amount=...")` | `await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=amount)` |
| `client.get("/verify/{ph}")` | `await get_verify(TEST_MINT_ID, ph)` |
| `notes.note_amount(h)` | `await get_note(h)` → check `.amount_msat` |
| `notes.pending_melts()` | `await get_pending_melts()` |
| `monkeypatch.setattr(settings, "verify_enabled", True)` | `await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)` |
| `monkeypatch.setattr(settings, "sunset_mint", True)` | `await update_mint(TEST_MINT_ID, TEST_WALLET, sunset_mint=True)` |
| `monkeypatch.setattr(settings, "base_fee_msat", 1000)` | `await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=1000)` |
