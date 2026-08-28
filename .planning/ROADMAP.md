# Roadmap: lnurlmint

**Project:** lnurlmint — LNbits extension implementing LUD-25 lnurlcash (Lightning bearer assets), ported from standalone `lnurl-mint` FastAPI app
**Mode:** mvp
**Granularity:** standard (7 phases, 3-5 plans each)
**Created:** 2026-08-28

## Guiding Constraints

These constraints from research inform the phase structure and MUST be preserved:

1. **The confirm-before-burn state machine is inseparable from melt.** Melt + confirm-before-burn + in-flight tracking + background reconcile ship together in Phase 2 — shipping melt without confirm-before-burn is a funds-loss bug.
2. **DB transaction atomicity discipline is designed before any burn/mint code.** LNbits' `Database.execute`/`fetchone` each open a SEPARATE transaction; multi-statement ops (`swap`, `settle_mint`, `mark_pending`) MUST use one `async with db.connect() as conn:` block. This discipline is established in Phase 1 (CRUD layer) and exercised in Phase 2.
3. **Tristate settlement semantics (`paid=None` = leave pending, NOT restore) locked by PoC tests in Phase 2.** The single highest-risk port detail.
4. **Per-wallet multi-tenancy is foundational.** Every table and query is wallet-scoped from Phase 1 — not a layer added later.
5. **Lightning Address is deferred to v2.** v1 ships raw LNURL/QR.
6. **Offline verification uses per-mint keypair (Option B).** Portable across all LNbits backends; `mintPubkey` is the mint's own key.

## Dependency Graph

```
Phase 1 (Foundation)
  ├──> Phase 2 (Mint + Melt + Security Stack)
  │      ├──> Phase 3 (Rotate + Split + Merge + Sunset)
  │      │      └──> Phase 5 (Offline Verification)
  │      ├──> Phase 4 (Comment Protection + Verify)
  │      └──> Phase 6 (Tor + Frontend)  [also depends on Phase 1]
  └──> Phase 7 (Full Test Suite)  [depends on all prior phases]
```

**Parallelization opportunities:**

- Phase 3 and Phase 4 can run in parallel (both depend on Phase 2, neither depends on the other).
- Phase 6 (Tor + Frontend) can start in parallel with Phase 3/4/5 once the Phase 2 API contract is stable (frontend builds against the management API from Phase 1 + LNURL endpoints from Phase 2).

---

### Phase 1: Extension Scaffold + Data Model + Per-Wallet Mint CRUD

**Goal:** The lnurlmint extension loads in LNbits, database migrations run, and a wallet owner can create and configure a per-wallet mint via the management API — the foundation every subsequent phase builds on.
**Mode:** mvp

**Rationale:** Per-wallet multi-tenancy is foundational — every table and query is wallet-scoped from the start. The DB transaction atomicity discipline (`async with db.connect() as conn:` for multi-statement ops) is established here in the CRUD layer, before any burn/mint code in Phase 2. No LNURL endpoints yet; this phase delivers the scaffold + data model + management CRUD vertical slice.

**Requirements:** EXT-01, EXT-02, EXT-04, DATA-01, DATA-02, DATA-03, DATA-04, DATA-05 (8)

**Plans:**
3/3 plans complete

2. **Data model + migrations** ✅ — `migrations.py` (`m001_initial`: `mints`, `notes`, `mints_records`, `melts` tables, all wallet-scoped via `mints.wallet` FK), `models.py` (pydantic v1 `Mint`, `Note`, `MintRecord`, `MeltRecord` + LNURL wire models). Establish the `async with db.connect() as conn:` transaction discipline in CRUD stubs. *(Plan 01-02 complete — 2026-08-28)*
3. **Per-wallet mint CRUD + management API** ✅ — `crud.py` (get_mint, update_mint, count_outstanding_notes, delete_mint — all wallet-scoped), `views_api.py` (GET/PUT/DELETE /{mint_id} via `require_admin_key`/`require_invoice_key`), UpdateMint partial-update model, outstanding-notes delete guard (409) with atomic check-and-delete, Vue create-mint form + delete button. Cross-wallet isolation E2E-verified. *(Plan 01-03 complete — 2026-08-28)*

**Success Criteria:**

1. LNbits loads the lnurlmint extension without errors; it appears in the extensions list with valid metadata.
2. A wallet owner can create a mint via `POST /lnurlmint/api/v1/mints` (setting fees, limits, username, verify toggle, sunset, onion_url) and retrieve it via `GET`.
3. Database migrations run successfully creating all four tables (`mints`, `notes`, `mints_records`, `melts`) on both SQLite and Postgres.
4. A wallet owner cannot see, access, or modify another wallet's mints — every management query is wallet-scoped.

---

### Phase 2: Mint + Melt Vertical MVP

**Goal:** A user can mint a Lightning-funded bearer note by paying an invoice and melt it back to sats via the withdraw callback — with the full confirm-before-burn state machine, in-flight melt tracking, background reconciliation, store-hashes-not-secrets discipline, and all five critical security PoCs passing against LNbits fixtures.
**Mode:** mvp

**Rationale:** This is the hardest phase. The confirm-before-burn state machine, DB transaction atomicity, in-flight melt tracking, and background reconciliation are one inseparable mechanism — a pending note left by a crashed melt must be resolvable or it's a permanent funds freeze. Shipping melt without confirm-before-burn is a funds-loss bug. The tristate settlement semantics (`paid=None` = leave pending, NOT restore) is the single highest-risk port detail and must be locked by the ported PoC tests in this same phase. Mint fee math (formula, fee-aware bounds, melt fee limit) is included because both the payRequest advertisement and the melt fee budget depend on it.

**Requirements:** EXT-03, MINT-01, MINT-02, MINT-03, MINT-04, MINT-05, REDEEM-01, REDEEM-02, REDEEM-06, SEC-01, SEC-02, SEC-03, SEC-04, SEC-05, SEC-06, SEC-07, REC-01, REC-02, REC-03, ECON-01, ECON-02, ECON-03, ECON-04, TEST-01, TEST-02, TEST-03, TEST-04, TEST-05 (28)

**Plans:**

5/5 plans complete

1. **DB transaction discipline + note CRUD core** ✅ — `crud.py` note operations (`settle_mint`, `mark_pending`, `finalize_melt`, `restore`, `pending_melts`, `record_melt`, `mark_melt_settled`, `get_note`, `get_mint_by_id`, `get_pending_mint_record`, `mint_record_exists`, `melt_record_exists`, `get_mint_id_for_note`) + `PendingNoteError` using single `async with db.connect() as conn:` blocks for atomicity. Compare-and-set pattern (`UPDATE ... WHERE minted=0` + `rowcount==1`). Store-hashes-not-secrets: note IDs are `sha256(k1)`, no preimage column. 4 LNURL wire models (`LnurlPayResponse`, `LnurlPayActionResponse`, `LnurlWithdrawResponse`, `WithdrawSuccessResponse`). All verified against SQLite. *(Plan 02-01 complete — 2026-08-28)*
2. **Mint flow (LUD-06 payRequest + callback)** ✅ — `views_lnurl.py`: `GET /lnurlmint/lnurlp/{mint_id}` (payRequest with fee-aware `minSendable`/`maxSendable`, `withdrawLink`, `disposable: false`), `GET /lnurlmint/p/cb/{mint_id}` (callback: `create_invoice` via LNbits, record pending mint, return `pr`). `services.py`: fee math (`_mint_fee_msat` rounding up, fee-aware bounds, `_melt_fee_limit_msat`), lazy settlement materialization (`_try_settle_mint` on first `/w` poll after settlement). `record_mint_record` CRUD helper. Router registered in `__init__.py`. *(Plan 02-02 complete — 2026-08-28)*
3. **Informational /w + melt callback** ✅ — `GET /lnurlmint/w/{mint_id}` (LUD-03 withdrawRequest, purely informational, rejects pending/spent/unknown notes, lazily settles via `_try_settle_mint`, echoes `k1` verbatim), `GET /lnurlmint/w/cb/{mint_id}` (melt: validate `pr` via `bolt11.decode`, reject duplicate/self-mint payment hashes SEC-06, `mark_pending` atomically, `_track_melt_start` after reservation SEC-03, reply `{"status":"OK"}` immediately, schedule background `_melt_pay`). Callback validation rules (`pr` MUST NOT combine with multiple `k1`s or `amount`; `h` required when `pr` absent — Phase 2 returns "Rotate/split/merge not yet implemented."). In-flight melt refcount registry (`_in_flight_melts` dict + `asyncio.Lock`) + `_melt_pay` stub in `services.py` (Plan 04 implements tristate settlement). *(Plan 02-03 complete — 2026-08-28)*
4. **Confirm-before-burn + in-flight tracking + reconcile** ✅ — `services.py`: `_melt_pay` background task with full tristate settlement (`pay_invoice` raises OR returns pending → `_confirm_payment` → `paid=True` finalize, `paid=False` restore, `paid=None` leave pending; every restore goes through `_confirm_payment` first — SEC-01). `_confirm_payment` retry-with-backoff (default `1,2,4,8,16`s; `delays=()` single-attempt for reconcile) using `status.success`/`status.failed`/`status.paid is None` (NOT `.pending`). `finally:` block always clears in-flight registry (SEC-03). `reconcile_pending_melts` (skips in-flight, resolves stranded notes with single-attempt confirm, logs+leaves pending for unconfirmable — REC-02). `boot_reconcile` one-shot at startup. `tasks.py` (`wait_for_melt_reconcile` via `run_interval(60, ...)`). `lnurlmint_start`/`lnurlmint_stop` wiring via `create_permanent_unique_task` (EXT-03). No-secret-logging on `/w`/`/w/cb`/reconcile paths (SEC-05). *(Plan 02-04 complete — 2026-08-28)*
5. **Critical PoC tests** ✅ — Ported and passing against LNbits fixtures: `test_poc_duplicate_melt.py` (TEST-01 — double-melt rejected with "pending"), `test_poc_a2_settle_race.py` (TEST-02 — compare-and-set produces exactly one note), `test_melt_restore_double_payout_poc.py` (TEST-03 — tristate: paid=None leaves pending NOT restored, paid=True finalizes, paid=False restores), `test_poc_reconcile_inflight_race.py` (TEST-04 — reconcile skips in-flight melts), `test_poc_f2_pending_info_leak.py` (TEST-05 — /w rejects pending notes, no value leaked). FakeNode/HodlNode/InFlightNode fixtures monkeypatch services.py + views_lnurl.py payment imports with controllable tristate (paid=None via PaymentPendingStatus). All 7 tests pass in 0.68s, stable across 3 runs. `@pytest.mark.anyio`, per-test DB isolation, `_CONFIRMATION_RETRY_DELAYS_SECONDS=()`. *(Plan 02-05 complete — 2026-08-28)*

**Success Criteria:**

1. A user can mint a bearer note by paying an invoice via the LUD-06 payRequest flow; the note materializes lazily on settlement, credited with `amount - mint_fee` net of the advertised fee.
2. A user can melt a bearer note back to sats via the LUD-03 withdraw callback; the note is burned only on positive settlement confirmation (`paid=True`), restored only on positive failure (`paid=False`), and left pending if unconfirmable (`paid=None`).
3. A note melted twice is rejected (pending state prevents second melt); a pending note is never advertised as withdrawable by the informational `/w` endpoint.
4. A crashed/restarted process reconciles pending melts correctly — in-flight melts are skipped (refcount registry), stranded notes are resolved on the next healthy tick.
5. All five critical PoC tests pass against LNbits test fixtures with a fake backend returning `paid=None`.

---

### Phase 3: Rotate + Split + Merge + Sunset

**Goal:** A note holder can rotate, split, and merge bearer notes using WALLET-supplied `h`/`h2` secret hashes, with sunset mode gating new issuance — completing the full redeem lifecycle.
**Mode:** mvp

**Rationale:** Rotate/split/merge build on the `swap` state machine and DB transaction atomicity from Phase 2. The `swap` operation (burn N notes + mint M notes atomically in one `db.connect()` block) is the core primitive. Fee conservation (split collects `base_fee` once, merge refunds `(n-1) * base_fee`) and collision-griefing prevention (`swap` collision-checks both `notes` and `mints_records`) are locked by PoC tests here. Sunset mode rejects mint+split (increases outstanding liability) while allowing rotate/merge/melt.

**Requirements:** REDEEM-03, REDEEM-04, REDEEM-05, REDEEM-07, ECON-05, TEST-06, TEST-08 (7)

**Plans:**

1. **Rotate + merge** ✅ — `views_lnurl.py` callback: rotate (single `k1` + `h`, no `pr`/`amount` → burn old, mint new same value), merge (many `k1` + `h` → burn all, mint one worth sum + `(n-1)*base_fee` refund). `crud.swap` atomic burn+mint in one `db.connect()` block with validate-then-burn-then-mint and two-table collision check (mints_records + notes). `sign_note` stub in services.py (Phase 5 implements real signing). `_MAX_K1S=100` rejects too many k1s. `h` required when `pr` absent. Temporary "Split not available." guard until Plan 02. All 8 Phase 2 tests still pass. *(Plan 03-01 complete — 2026-08-28)*
2. **Split** ✅ — callback: split (one/many `k1` + `amount` + `h` + `h2` → burn all, mint two notes: `amount` keyed by `h`, `change = total - amount - base_fee_msat` keyed by `h2`). Fee arithmetic: `change = total - amount - base_fee_msat`; reject `change_before_fee < base_fee` (negative change) and `change < 1` (zero-value note). `h2` required when `amount` present, validated against `HEX32_PATTERN`. Shared k1 resolution loop extracted before split/rotate/merge branching. `sign_note` called for both `h` and `h2` (stub). Temporary "Split not available." guard removed. All 8 Phase 2 tests still pass. *(Plan 03-02 complete — 2026-08-28)*
3. **Sunset mode + collision griefing + fee conservation PoCs** — Sunset: `/p/cb` and split branch reject with `{"status":"ERROR","reason":"This mint is sunsetting - ..."}`; rotate/merge/melt unaffected. Port `test_poc_fee_conservation.py` + `test_poc_fee_loop.py` (TEST-06: fee rounding up, fee-aware bounds, conservation identity, no inflation). Port `test_poc_a1_collision_griefing.py` (TEST-08: `swap` collision-checks both `notes` and `mints_records`).

**Success Criteria:**

1. A note holder can rotate a note (burn old, mint new with same value) and merge multiple notes into one (sum + fee refund).
2. A note holder can split a note into two notes with correct fee arithmetic (no zero-value notes, no inflation).
3. Sunset mode rejects new mints and splits while allowing rotate, merge, and melt.
4. Fee conservation and collision-griefing PoC tests pass — no fee rounding or swap arithmetic allows attacker gain.

---

### Phase 4: Comment Protection + Verify

**Goal:** A mint can use LUD-25 comment protection (note keyed by WALLET-supplied comment hash, closing the routing-node preimage race), and the LUD-21 verify endpoint reports settlement status with a real off-switch and comment-protection gating.
**Mode:** mvp

**Rationale:** Comment protection and verify are paired: verify is only safe when comment protection is in play (for no-comment mints, the preimage IS the bearer secret). The verify observer race PoC (`test_poc_verify_race.py`) locks both together. `VERIFY_ENABLED=false` must produce a real 404 (not just a hidden advertisement), because the preimage is a bearer secret and the URL shape is guessable.

**Requirements:** COMM-01, COMM-02, COMM-03, VER-01, VER-02, VER-03, VER-04, TEST-07 (8)

**Plans:**

1. **Comment protection** — `GET /lnurlmint/p/cb/{mint_id}` accepts `comment` query param (LUD-12); if bare hex-encoded 32-byte hash, note credited as `k1=<secret>` keyed by comment hash (not payment preimage). Non-hex32 or no `comment` falls back to plain preimage-keyed note (never rejected). `verify` URL advertised in `/p/cb` response only when `comment` was used.
2. **Verify endpoint (LUD-21)** — `GET /lnurlmint/verify/{mint_id}/{payment_hash}`: if `!verify_enabled` → 404 (real off-switch). If mint payment_hash found and `!comment_protected` → 404 (preimage IS the secret). If comment-protected → serve `settled`, live-fetched `preimage`, `pr`. If melt payment_hash found → serve unconditionally (melt preimage is harmless). Preimage fetched live from funding source on every call, never cached.
3. **Verify race PoC** — Port `test_poc_verify_race.py` (TEST-07): verify refuses preimage for no-comment mints, serves it for comment-protected mints, `VERIFY_ENABLED=false` = 404. Port `test_surface_hunter_verification.py` if in scope.

**Success Criteria:**

1. A mint using comment protection keys the note by the WALLET-supplied comment hash, not the payment preimage — closing the routing-node preimage race.
2. The LUD-21 verify endpoint reports settlement status with live-fetched preimage for comment-protected mints; `VERIFY_ENABLED=false` produces a real 404.
3. Verify refuses to serve preimages for no-comment mints (where the preimage is the bearer secret) and serves them for comment-protected mints and melt directions.
4. The verify race PoC passes against LNbits fixtures.

---

### Phase 5: Offline Verification

**Goal:** Each mint signs rotate/split/merge notes with a per-mint secp256k1 keypair, advertising `mintPubkey` and returning `sig`/`sig2` so holders can verify notes offline without trusting the mint online.
**Mode:** mvp

**Rationale:** Offline verification uses Option B (per-mint keypair) for portability across all LNbits backends (FakeWallet, VoidWallet, etc.). `mintPubkey` is the mint's own key, not the node's. `coincurve` is used for signing (transitive dep, already imported by LNbits' own `nwc.py`/`nostr.py`). Signing failures are swallowed (never block a rotate/split/merge). `verify_note` (coincurve recovery) is test-only. Depends on Phase 3 (rotate/split/merge must exist to sign).

**Requirements:** SIGN-01, SIGN-02, SIGN-03, SIGN-04 (4)

**Plans:**

1. **Per-mint keypair + mintPubkey advertisement** — `signing.py`: secp256k1 keypair generated at mint creation, stored in `mints.mint_privkey`. `GET /lnurlmint/w/{mint_id}` advertises `mintPubkey` (the mint's own public key) when keypair exists. `verify_note` (coincurve `PublicKey.from_signature_and_message`) for test-only use.
2. **sign_note + sig/sig2 on rotate/split/merge** — Recoverable ECDSA signature over `LNURLcash:<amount>:<note_id_hex>` using mint's private key. Rotate/split/merge responses carry `sig`/`sig2`. Signing failures swallowed (return `None`, never raise). Port `test_offline_verification.py` against LNbits fixtures.

**Success Criteria:**

1. Each mint has a secp256k1 keypair; the public key is advertised as `mintPubkey` in the withdrawRequest response.
2. Rotate/split/merge responses carry `sig`/`sig2` signatures verifiable offline against `mintPubkey` using `verify_note`.
3. Signing failures are swallowed — a signing error never blocks a rotate/split/merge operation.

---

### Phase 6: Tor + Frontend

**Goal:** Tor visitors get onion-base-URL callback URLs (no clearnet leak), wallet owners can create and configure their mint via a management SPA, and visitors can view a public one-pager showing the mint QR, limits, and node info.
**Mode:** mvp

**Rationale:** Tor base-URL substitution preserves the spoof-proof `public_base_url` semantics from `lnurl-mint` (derived from per-mint `base_url`/`onion_url`, never raw request Host). The management SPA is the largest new-for-LNbits surface (per-wallet mints require a UI to create/configure). The public one-pager is the note-holder's face of the mint. Both use Vue 3 + Quasar SFCs via LNbits vendor bundles (following `giftcards` pattern). Can start in parallel with Phase 3/4/5 once the Phase 2 API contract is stable.

**Requirements:** TOR-01, TOR-02, UI-01, UI-02, UI-03, UI-04 (6)

**Plans:**

1. **Tor base URL substitution** — Per-mint `onion_url` field: when request Host matches onion hostname, callback URLs use `onion_url` as base. `public_base_url` derivation is Host-header-spoof-proof (built from per-mint `base_url`, not `req.url_for`). `X-Forwarded-Host` only behind trusted proxy (documented assumption).
2. **Management SPA** — Vue 3 + Quasar: wallet owner can create a new mint (fees, limits, username, verify toggle, sunset, onion_url), update config, delete mint (only if no outstanding notes), view outstanding notes, see mint activity. Served via `index` generic view (`check_user_exists` + `require_admin_key`/`require_invoice_key`).
3. **Public one-pager** — Vue 3 + Quasar: mint QR code (LNURL of payRequest via `pyqrcode`), mint limits, node info (alias, color, capacity, channel/peer counts) with mempool.space/amboss.space links, Tor address if configured. Served via `index_public` (no authentication).

**Success Criteria:**

1. A Tor visitor's callback URLs use the mint's `onion_url` as the base — no clearnet `base_url` leaks into a Tor visitor's QR code.
2. A wallet owner can create, configure, and delete their mint via the management SPA; can view outstanding notes and mint activity.
3. A visitor can view the public one-pager showing the mint QR, limits, and node info without authentication.

---

### Phase 7: Full Test Suite Port

**Goal:** The complete `lnurl-mint` test suite is ported and passing against LNbits fixtures — the behavioral parity acceptance gate.
**Mode:** mvp

**Rationale:** The PoC tests encoding funds-loss guarantees were ported incrementally in Phases 2-4 (TEST-01 through TEST-08). This phase ports the remaining tests (the full bearer-asset threat suite + all non-PoC tests adapted to LNbits fixtures and wallet mocks) and runs the complete suite as an integration gate, verifying no regressions across all features.

**Requirements:** TEST-09, TEST-10 (2)

**Plans:**

1. **Bearer threat suite** — Port `test_bearer_threat_suite_poc.py` (TEST-09): the full bearer-asset threat suite passes against LNbits fixtures. This is the integration-level security gate combining all individual PoC guarantees.
2. **Remaining tests** — Port all remaining tests (TEST-10): `test_lnurlcash.py`, `test_config.py`, `test_errors.py`, `test_frontend.py`, `test_mint_log.py`, `test_node.py`, `test_reconcile.py`, `test_verify.py`, `test_offline_verification.py`, `test_onion.py`, `test_comment_protection.py`, `test_surface_hunter_verification.py`, `test_auth_data_hunter_poc.py` — all adapted to LNbits test fixtures and wallet mocks. Run full suite as regression gate.

**Success Criteria:**

1. The full bearer-asset threat suite passes against LNbits fixtures — no funds-loss regression across all security guarantees.
2. All remaining tests from `lnurl-mint` are ported and pass, completing behavioral parity verification.

---

## Coverage Summary

| Phase | Requirements | Count |
|-------|-------------|-------|
| 1: Extension Scaffold + Data Model + Per-Wallet Mint CRUD | EXT-01, EXT-02, EXT-04, DATA-01, DATA-02, DATA-03, DATA-04, DATA-05 | 8 |
| 2: Mint + Melt Vertical MVP | EXT-03, MINT-01–05, REDEEM-01, REDEEM-02, REDEEM-06, SEC-01–07, REC-01–03, ECON-01–04, TEST-01–05 | 28 |
| 3: Rotate + Split + Merge + Sunset | REDEEM-03, REDEEM-04, REDEEM-05, REDEEM-07, ECON-05, TEST-06, TEST-08 | 7 |
| 4: Comment Protection + Verify | COMM-01–03, VER-01–04, TEST-07 | 8 |
| 5: Offline Verification | SIGN-01–04 | 4 |
| 6: Tor + Frontend | TOR-01, TOR-02, UI-01–04 | 6 |
| 7: Full Test Suite Port | TEST-09, TEST-10 | 2 |
| **Total** | | **63** |

> **Note:** REQUIREMENTS.md stated 52 requirements; actual count is 63 (13 categories: EXT=4, DATA=5, MINT=5, REDEEM=7, SEC=7, REC=3, VER=4, COMM=3, SIGN=4, ECON=5, TOR=2, UI=4, TEST=10). The traceability table in REQUIREMENTS.md has been updated with the correct count.

---
*Created: 2026-08-28*
*Mode: mvp | Granularity: standard*
