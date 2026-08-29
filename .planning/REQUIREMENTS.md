# Requirements: lnurlmint

**Defined:** 2026-08-28
**Core Value:** A wallet holder can mint a Lightning-funded bearer note, hand it to anyone, and that anyone can redeem it — rotate, split, merge, or melt it back to sats — without an account, against any spec-compliant wallet, with the same security guarantees (no double-spend, no secret leakage, confirm-before-burn) as the standalone `lnurl-mint`.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases. Full LUD-25 behavioral parity with `lnurl-mint`, adapted to the LNbits extension model (per-wallet mints, LNbits wallet funding, LNbits `Database`).

### Extension Scaffold

- [x] **EXT-01**: Extension is discoverable by LNbits' loader — `__init__.py` exports `lnurlmint_ext` (APIRouter prefix `/lnurlmint`), `lnurlmint_start`/`lnurlmint_stop`, `db`, `lnurlmint_static_files`; `manifest.json` and `config.json` are valid
- [x] **EXT-02**: Extension registers static files at `/lnurlmint/static` and serves Vue 3 SFCs via LNbits' vendor bundle system (following `giftcards` pattern)
- [x] **EXT-03**: Extension start/stop lifecycle wires background tasks via `create_permanent_unique_task` (reconcile + health); stop cancels them cleanly
- [x] **EXT-04**: No new Python dependencies are added beyond what LNbits' `pyproject.toml` already declares (`bolt11`, `bech32`, `httpx`, `pyqrcode`, `loguru` are available; `pydantic-settings` v2 is replaced by pydantic v1 + `lnbits.settings.settings`; `qrcode` is replaced by `pyqrcode`)

### Data Model

- [x] **DATA-01**: `m001_initial` migration creates `lnurlmint.mints` table (per-wallet mint config: `id`, `wallet`, `username`, `base_url`, `onion_url`, `base_fee_msat`, `fee_percent_ppm`, `min_sendable_msat`, `max_sendable_msat`, `min_mint_msat`, `verify_enabled`, `sunset_mint`, `mint_privkey` (secp256k1 keypair for offline verification), `created_at`, `updated_at`)
- [x] **DATA-02**: `m001_initial` creates `lnurlmint.notes` table (bearer notes: `id` (sha256(k1) hex), `mint_id` FK → mints, `amount_msat`, `state` (outstanding/pending/spent), `minted` flag, `comment_hash` (nullable, for comment protection), `created_at`)
- [x] **DATA-03**: `m001_initial` creates `lnurlmint.mints_records` table (pending mints: `payment_hash`, `mint_id`, `pr`, `amount_msat`, `comment_hash`, `created_at`) and `lnurlmint.melts` table (pending/settled melts: `payment_hash`, `mint_id`, `note_ids`, `amount_msat`, `settled` flag, `pr`, `created_at`)
- [x] **DATA-04**: All models use pydantic v1 syntax (`validator`/`root_validator`/`class Config`, not v2 `field_validator`/`model_validator`), matching LNbits' pinned pydantic 1.10.26
- [x] **DATA-05**: Every query is scoped by `wallet_id` via JOIN on `mints.wallet` — no cross-wallet note access is possible (giftcards pattern)

### Mint Lifecycle

- [x] **MINT-01**: `GET /lnurlmint/lnurlp/{mint_id}` returns a LUD-06 `LnurlPayResponse` with `callback`, `minSendable` (fee-aware floor), `maxSendable`, `metadata` (mint description + `text/identifier` + optional `Mint fees:` entry), and `withdrawLink` pointing at `/lnurlmint/w/{mint_id}`
- [x] **MINT-02**: `GET /lnurlmint/p/cb/{mint_id}` (LUD-06 callback) creates an invoice via LNbits `create_invoice(wallet_id=mint.wallet, ...)`, records a pending mint in `mints_records` (payment_hash → net amount after fee), and returns `LnurlPayActionResponse` with `pr` and optional `verify` URL
- [x] **MINT-03**: When the mint invoice settles (detected lazily via `check_payment_status` or payment hook), the pending mint is materialized as an outstanding note keyed by `sha256(preimage)` (= payment_hash), credited with `amount - mint_fee` net of the advertised fee
- [x] **MINT-04**: Mint rejects amounts below `min_sendable_msat` or above `max_sendable_msat`, and amounts where `amount - mint_fee < min_mint_msat` (net-of-fee floor)
- [x] **MINT-05**: `disposable: false` is returned in the payRequest action response (LUD-11 — the mint address is meant to be stored and reused)

### Redeem Lifecycle

- [x] **REDEEM-01**: `GET /lnurlmint/w/{mint_id}?k1=...` (LUD-03 withdrawRequest) is purely informational — never burns or alters the note; returns `LnurlWithdrawResponse` with `callback`, `k1` (echoed verbatim), `minWithdrawable` = `maxWithdrawable` = note value, `mintPubkey` (if signing configured); rejects pending notes with `{"status":"ERROR","reason":"pending"}`; rejects unknown/spent notes appropriately
- [x] **REDEEM-02**: `GET /lnurlmint/w/cb/{mint_id}` (the mutating callback) implements melt: single `k1` + `pr` → note reserved (mark_pending), `{"status":"OK"}` returned immediately, `pr` paid asynchronously via `pay_invoice`, note burned on positive settlement, restored on positive failure, left pending if unconfirmable
- [x] **REDEEM-03**: `GET /lnurlmint/w/cb/{mint_id}` implements rotate: single `k1` + `h` (no `pr`, no `amount`) → note burned, new note keyed by `h` minted (same value)
- [x] **REDEEM-04**: `GET /lnurlmint/w/cb/{mint_id}` implements split: one or many `k1` + `amount` + `h` + `h2` → all burned, two notes minted (`amount` keyed by `h`, remainder keyed by `h2`)
- [x] **REDEEM-05**: `GET /lnurlmint/w/cb/{mint_id}` implements merge: many `k1` + `h` (no `pr`, no `amount`) → all burned, one note worth the sum keyed by `h`
- [x] **REDEEM-06**: `pr` MUST NOT combine with multiple `k1`s or with `amount`; `h` is required whenever `pr` is absent; `h2` additionally required whenever `amount` is present — malformed requests fail with `{"status":"ERROR","reason":"missing h"}` or `"missing h2"`
- [x] **REDEEM-07**: The callback response for rotate/split/merge carries no secret (just `{"status":"OK"}` + optional `sig`/`sig2`); the informational `/w` endpoint ignores any `amount` query param (never authoritative; `maxWithdrawable` is)

### Security Invariants

- [x] **SEC-01**: Confirm-before-burn: a bearer note is NEVER burned on a guess — only on positive settlement confirmation (`check_payment_status` → `paid=True`); only restored on positive failure confirmation (`paid=False`); left pending if unconfirmable (`paid=None` or raise). A naive `except PaymentError: restore` without `check_payment_status` is the vulnerable shape and MUST NOT exist
- [x] **SEC-02**: Store-hashes-not-secrets: notes are stored keyed by `sha256(k1)`, never the raw `k1`/preimage; no preimage column exists in any table; verify fetches preimage live from the funding source on every call, never cached
- [x] **SEC-03**: In-flight melt tracking: an in-process `_in_flight_melts` refcount map (registered before the background task starts, cleared in `finally`) prevents reconcile from restoring a note whose melt is still live — reconcile skips any `payment_hash` in this map without consulting the funding source
- [x] **SEC-04**: Pending rejection: every mutating callback AND the informational `/w` endpoint reject pending notes with `{"status":"ERROR","reason":"pending"}` — a pending note is never advertised as withdrawable (sell-during-melt scam prevention)
- [x] **SEC-05**: No-secret-logging: query strings on `/w`, `/w/cb`, and `/verify` are never logged (a bearer note's `k1` can sit in a URL far longer than an ephemeral LUD-03 `k1`)
- [x] **SEC-06**: Duplicate-melt / collision-griefing prevention: a melt against an already-used `payment_hash` is rejected before reservation; `swap` (rotate/split/merge) collision-checks both `notes` and `mints_records` tables
- [x] **SEC-07**: Cross-wallet isolation: every management and reconcile query carries `WHERE wallet_id = :wallet` — a user can never see, spend, or restore another user's notes

### Settlement & Reconcile

- [x] **REC-01**: `_melt_pay` background task preserves the tristate settlement semantics: `pay_invoice` raises → call `check_payment_status` → `paid=True` → finalize (burn), `paid=False` → restore, `paid=None` → leave pending (with `_confirm_payment` retry-with-backoff for the `None`/raise case)
- [x] **REC-02**: `reconcile_pending_melts` runs at boot (via `lnurlmint_start`) and on every healthy tick (via `create_permanent_unique_task` + `run_interval`), resolving notes left pending by crashes/restarts; skips in-flight melts; uses single-attempt confirmation (`delays=()`)
- [x] **REC-03**: Multi-statement DB ops (`swap`, `settle_mint`, `mark_pending`) use one `async with db.connect() as conn:` block to preserve atomicity — LNbits' `db.execute`/`db.fetchone` each open a separate transaction, so the compare-and-set pattern (`UPDATE ... WHERE minted=0` + `INSERT notes`) MUST be in one connection

### Verify

- [x] **VER-01**: `GET /lnurlmint/verify/{mint_id}/{payment_hash}` (LUD-21) reports settlement status for a mint invoice or a melt's outgoing payment, keyed by `payment_hash`
- [x] **VER-02**: `VERIFY_ENABLED=false` disables the endpoint entirely (404), not just its advertisement — the preimage is a bearer secret, so an operator who doesn't want it served gets a real off-switch
- [x] **VER-03**: Verify refuses to serve a preimage for a no-comment mint's `payment_hash` (there the preimage IS the bearer note's spend secret) — returns 404; only serves preimages for comment-protected mints (where the preimage redeems nothing)
- [x] **VER-04**: Verify response includes `settled` (bool), `preimage` (hex, only if settled + comment-protected + funding source available), `pr` (the BOLT-11 invoice)

### Comment Protection

- [x] **COMM-01**: `GET /lnurlmint/p/cb/{mint_id}` accepts a `comment` query param (LUD-12); if it's a bare hex-encoded 32-byte hash, the resulting note is credited as `k1=<secret>` (keyed by the comment hash, not the payment preimage) — closing the routing-node preimage race
- [x] **COMM-02**: A non-hex32 `comment` or no `comment` falls back to the plain preimage-keyed note (never rejected outright — a wallet with no LNURLcash support may send an ordinary LUD-12 comment)
- [x] **COMM-03**: `verify` is only advertised in `/p/cb`'s response when `comment` was used (spec: SERVICE MUST NOT offer verify in the no-comment fallback)

### Offline Verification

- [x] **SIGN-01**: Each mint has a secp256k1 keypair generated at creation and stored in `mints.mint_privkey` (Option B — portable across all LNbits backends)
- [x] **SIGN-02**: `GET /lnurlmint/w/{mint_id}` advertises `mintPubkey` (the mint's own public key, not the node's) when the mint has a keypair
- [x] **SIGN-03**: Rotate/split/merge responses carry `sig`/`sig2` — recoverable ECDSA signatures over `LNURLcash:<amount>:<note_id_hex>` using the mint's private key; signing failures are swallowed (never block a rotate/split/merge)
- [x] **SIGN-04**: `verify_note` (recoverable signature verification) is available for test-only use (using `coincurve` — transitive dep, confined to tests)

### Mint Economics

- [x] **ECON-01**: Mint fee is `base_fee_msat + (amount_msat * fee_percent_ppm) // 1_000_000`, rounded UP to the nearest whole sat (never short a sat); advertised as `["text/plain", "Mint fees: <base_fee_msat>,<fee_percent_ppm>"]` in metadata when either is non-zero
- [x] **ECON-02**: `minSendable` is fee-aware: walks amount up from `max(min_sendable_msat, min_mint_msat)` until `amount - mint_fee >= min_mint_msat` (so paying the advertised minimum always succeeds)
- [x] **ECON-03**: `maxSendable` net of fee is advertised correctly (`max_sendable_msat - mint_fee(max_sendable_msat)`)
- [x] **ECON-04**: Melt fee limit is `max(round(amount * 0.005), 5000, mint_fee(amount))` — the mint fee withheld at mint time covers routing at melt, never less than the 0.5%/5000msat floor
- [x] **ECON-05**: Sunset mode (`sunset_mint=true`): `/p/cb` and split branch reject with `{"status":"ERROR","reason":"This mint is sunsetting - ..."}`; rotate, merge, and melt are left alone (none increase outstanding liability)

### Tor

- [x] **TOR-01**: Per-mint `onion_url` field: when set, the public one-pager advertises it as an alternative address, and LNURL/callback URLs use it as the base instead of `base_url` for Tor visitors (prevents a fixed clearnet `base_url` leaking into a Tor visitor's QR code)
- [x] **TOR-02**: `public_base_url` derivation is Host-header-spoof-proof (built from per-mint `base_url` setting, not `req.url_for` which is Host-header-derived and spoofable via a plain Host header even behind a proxy)

### Frontend

- [x] **UI-01**: Management SPA (Vue 3 + Quasar): wallet owner can create a new mint (set fees, limits, username, verify toggle, sunset, onion_url), view outstanding notes, see mint activity log
- [x] **UI-02**: Management SPA: wallet owner can update mint config (fees, limits, sunset, verify toggle) and delete a mint (only if no outstanding notes)
- [x] **UI-03**: Public one-pager (`/lnurlmint/{mint_id}`): shows mint QR code (LNURL of the payRequest), mint limits, node info (alias, color, capacity, channel/peer counts) with mempool.space/amboss.space links, Tor address if configured
- [x] **UI-04**: Public one-pager is served without authentication (index_public pattern); management SPA requires wallet auth (check_user_exists + require_admin_key/require_invoice_key)

### Testing

- [x] **TEST-01**: `test_poc_duplicate_melt.py` ported: a note melted twice is rejected (pending state prevents second melt)
- [x] **TEST-02**: `test_poc_a2_settle_race.py` ported: compare-and-set `UPDATE ... WHERE minted=0` + `INSERT` is atomic (no double-mint on concurrent settlement)
- [x] **TEST-03**: `test_melt_restore_double_payout_poc.py` ported: `pay_invoice` raising `PaymentError` with `paid=None` leaves the note pending, NOT restored (confirm-before-burn tristate)
- [x] **TEST-04**: `test_poc_reconcile_inflight_race.py` ported: reconcile skips in-flight melts (no double-spend from restore-during-live-payment)
- [x] **TEST-05**: `test_poc_f2_pending_info_leak.py` ported: `/w` rejects pending notes with `"pending"` reason (no sell-during-melt scam)
- [x] **TEST-06**: `test_poc_fee_conservation.py` + `test_poc_fee_loop.py` ported: fee rounding is up, fee-aware bounds are correct, no fee-loop or short-a-sat
- [x] **TEST-07**: `test_poc_verify_race.py` ported: verify refuses preimage for no-comment mints, serves it for comment-protected mints, `VERIFY_ENABLED=false` = 404
- [x] **TEST-08**: `test_poc_a1_collision_griefing.py` ported: `swap` collision-checks both `notes` and `mints_records` (no reuse of a payment hash)
- [ ] **TEST-09**: `test_bearer_threat_suite_poc.py` ported: the full bearer-asset threat suite passes against LNbits fixtures
- [ ] **TEST-10**: Remaining tests ported: `test_lnurlcash.py`, `test_config.py`, `test_errors.py`, `test_frontend.py`, `test_mint_log.py`, `test_node.py`, `test_reconcile.py`, `test_verify.py`, `test_offline_verification.py`, `test_onion.py`, `test_comment_protection.py`, `test_surface_hunter_verification.py`, `test_auth_data_hunter_poc.py` — all adapted to LNbits test fixtures and wallet mocks

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Lightning Address

- **LADDR-01**: Mint payRequest resolves via LUD-16 Lightning Address (`user@host`) through LNbits' `lnurlp` extension — requires a coordinated PR to lnurlp adding a `withdrawLink` field to `LnurlPayResponse` and a delegation hook in its callback so lnurlmint payRequests are served through `/.well-known/lnurlp/{user}`
- **LADDR-02**: Mint-address endpoint (`/.well-known/lnurlw/{user}`) — the withdraw-side mirror of the LUD-16 address (theoretical, no LUD number; informational only)

### lnurl-wallet Integration

- **WALLET-01**: Serve `lnurl-wallet` (the SolidJS bearer-note holder SPA) as a static sub-route of the extension, with its own build pipeline — all-in-one mint + holder experience

### Optimizations

- **OPT-01**: Node info caching (1h TTL) for the public one-pager and mint-address endpoint — avoids a fresh getinfo + capacity RPC on every page view
- **OPT-02**: Node capacity from public graph (lnd `GetNodeInfo` / cln `listchannels` filtered to `source=<own id>`)

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Bundling `lnurl-wallet` into v1 | It is a mint-agnostic SolidJS static SPA; bundling doubles frontend scope and conflates issuer/holder trust domains. v2 may serve it as a sub-route. |
| Direct lnd/cln REST funding (`node.py`'s own backend) | LNbits already abstracts Lightning per-wallet; a standalone lnd/cln mode is rejected to keep the extension idiomatic. |
| Single global mint | Per-wallet multi-tenancy is the model; a global admin-only mint is not supported. |
| Serving own `.well-known/lnurlp/{user}` routes | LNbits' `lnurlp` extension owns that path via redirect middleware; serving it from lnurlmint would conflict. Lightning Address deferred to v2 (requires lnurlp PR). |
| Node-direct `signmessage` for offline verification (Option A) | Rejected in favor of per-mint keypair (Option B) for portability across all LNbits backends. Tradeoff: `mintPubkey` is the mint's key, not the node's. |
| Custodial user accounts | The mint custodies bearer notes, never per-user accounts; a note's `k1` IS the asset, no account needed. |
| Multi-process / multi-worker deployment | Note reservation, burning, and melt reconciliation are coordinated inside a single process (in-process `_in_flight_melts` + single `Database` connection); a second process voids those guarantees. |
| Caching preimages locally | Preimages are bearer secrets; they are fetched live from the funding source on every verify call, never persisted. |
| Adding new Python dependencies | LNbits extension docs forbid it (`docs/devs/extensions.md` line 43); all deps must already be in LNbits' `pyproject.toml`. |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| EXT-01 | 1 | Complete |
| EXT-02 | 1 | Complete |
| EXT-03 | 2 | Complete |
| EXT-04 | 1 | Complete |
| DATA-01 | 1 | Complete |
| DATA-02 | 1 | Complete |
| DATA-03 | 1 | Complete |
| DATA-04 | 1 | Complete |
| DATA-05 | 1 | Complete |
| MINT-01 | 2 | Complete |
| MINT-02 | 2 | Complete |
| MINT-03 | 2 | Complete |
| MINT-04 | 2 | Complete |
| MINT-05 | 2 | Complete |
| REDEEM-01 | 2 | Complete |
| REDEEM-02 | 2 | Complete |
| REDEEM-03 | 3 | Complete |
| REDEEM-04 | 3 | Complete |
| REDEEM-05 | 3 | Complete |
| REDEEM-06 | 2 | Complete |
| REDEEM-07 | 3 | Complete |
| SEC-01 | 2 | Complete |
| SEC-02 | 2 | Complete |
| SEC-03 | 2 | Complete |
| SEC-04 | 2 | Complete |
| SEC-05 | 2 | Complete |
| SEC-06 | 2 | Complete |
| SEC-07 | 2 | Complete |
| REC-01 | 2 | Complete |
| REC-02 | 2 | Complete |
| REC-03 | 2 | Complete |
| VER-01 | 4 | done |
| VER-02 | 4 | done |
| VER-03 | 4 | done |
| VER-04 | 4 | done |
| COMM-01 | 4 | done |
| COMM-02 | 4 | done |
| COMM-03 | 4 | done |
| SIGN-01 | 5 | done |
| SIGN-02 | 5 | done |
| SIGN-03 | 5 | done |
| SIGN-04 | 5 | done |
| ECON-01 | 2 | Complete |
| ECON-02 | 2 | Complete |
| ECON-03 | 2 | Complete |
| ECON-04 | 2 | Complete |
| ECON-05 | 3 | Complete |
| TOR-01 | 6 | Complete |
| TOR-02 | 6 | Complete |
| UI-01 | 6 | Complete |
| UI-02 | 6 | Complete |
| UI-03 | 6 | Complete |
| UI-04 | 6 | Complete |
| TEST-01 | 2 | Complete |
| TEST-02 | 2 | Complete |
| TEST-03 | 2 | Complete |
| TEST-04 | 2 | Complete |
| TEST-05 | 2 | Complete |
| TEST-06 | 3 | Complete |
| TEST-07 | 4 | done |
| TEST-08 | 3 | Complete |
| TEST-09 | 7 | pending |
| TEST-10 | 7 | pending |

**Coverage:**

- v1 requirements: 63 total (EXT=4, DATA=5, MINT=5, REDEEM=7, SEC=7, REC=3, VER=4, COMM=3, SIGN=4, ECON=5, TOR=2, UI=4, TEST=10)
- Mapped to phases: 63 (100%)
- Unmapped: 0

**Per-phase distribution:**

- Phase 1 (Extension Scaffold + Data Model + Per-Wallet Mint CRUD): 8 requirements
- Phase 2 (Mint + Melt Vertical MVP): 28 requirements
- Phase 3 (Rotate + Split + Merge + Sunset): 7 requirements
- Phase 4 (Comment Protection + Verify): 8 requirements
- Phase 5 (Offline Verification): 4 requirements
- Phase 6 (Tor + Frontend): 6 requirements
- Phase 7 (Full Test Suite Port): 2 requirements

> **Note:** The original count of 52 was incorrect; the actual count across 13 categories is 63. This has been corrected in the traceability table and ROADMAP.md.

---
*Requirements defined: 2026-08-28*
*Last updated: 2026-08-28 after roadmap creation (traceability populated, count corrected to 63)*
