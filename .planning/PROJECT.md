# lnurlmint

## What This Is

An LNbits extension that implements **lnurlcash** (LUD-25, Lightning bearer assets) — a port of the standalone [`lnurl-mint`](https://github.com/dni/lnurl-mint) FastAPI app into the LNbits extension model. Each LNbits wallet can run its own mint, issuing bearer notes that circulate offline as `lnurlw://` withdraw links and can be rotated, split, merged, or melted back to a BOLT-11 payment. For LNbits users who want to issue transferable, offline-circulating sats-backed bearer notes.

## Core Value

A wallet holder can mint a Lightning-funded bearer note, hand it to anyone, and that anyone can redeem it — rotate, split, merge, or melt it back to sats — without an account, against any spec-compliant wallet, with the same security guarantees (no double-spend, no secret leakage, confirm-before-burn) as the standalone `lnurl-mint`.

## Business Context

- **Customer**: LNbits node operators and their users who want to issue transferable sats-denominated bearer instruments (gifts, vouchers, offline cash).
- **Revenue model**: Optional per-mint mint fee (`BASE_FEE_MSAT` / `FEE_PERCENT_PPM`) withheld at mint time to cover routing cost on eventual melt.
- **Success metric**: Full LUD-25 behavioral parity with `lnurl-mint`, verified by the ported security/race PoC test suite.
- **Strategy notes**: `lnurl-mint` README and `lnurl-wallet` README are the canonical references.

## Requirements

### Validated

- ✓ LNbits extension scaffold (`lnurlmint` package, `__init__.py` with `lnurlmint_ext` router + start/stop lifecycle, `manifest.json`, `config.json`, static files registration) — Phase 1
- ✓ Per-wallet mint model: each LNbits wallet can create and own a mint instance with its own fees, limits, and identity — Phase 1
- ✓ Note store backed by LNbits `Database` abstraction (SQLite + Postgres) replacing `lnurl-mint`'s standalone sqlite + module-level lock — Phase 1 (schema) + Phase 2 (state machine)
- ✓ No-new-dependencies discipline (LNbits extension docs forbid adding deps not already in LNbits' `pyproject.toml`) — Phase 1
- ✓ Mint funding via the LNbits wallet abstraction (`create_invoice` / `pay_invoice` from `lnbits.core.services.payments`) — Phase 2
- ✓ LUD-06 payRequest + callback (`/p/cb`): minting a bearer note by paying an invoice whose preimage becomes the note — Phase 2
- ✓ LUD-03 withdrawRequest (`/w`) and the mutating melt callback (`/w/cb`): melt with confirm-before-burn tristate settlement — Phase 2 (rotate/split/merge in Phase 3)
- ✓ Mint fees (`BASE_FEE_MSAT` / `FEE_PERCENT_PPM`), `MIN_MINT_MSAT` net-of-fee floor, fee-aware `minSendable`/`maxSendable` advertisement — Phase 2
- ✓ Background melt reconciliation task wired into LNbits' `create_permanent_unique_task` lifecycle (replaces `server.py` lifespan + monitor) — Phase 2
- ✓ Raw LNURL/QR payRequest + callback (mint serves its own `/lnurlmint/lnurlp/{mint_id}` and `/lnurlmint/p/cb/{mint_id}` — no `.well-known` route, no Lightning Address for v1) — Phase 2
- ✓ Full test suite ported from `lnurl-mint` (5 critical PoCs: double-spend, race, preimage leak, fee conservation, reconcile-inflight) adapted to LNbits test fixtures and wallet mocks — Phase 2 (5 critical PoCs; remaining ~20 tests in Phase 7)

### Active

- [ ] LUD-21 verify (`/verify/{payment_hash}`) with the same `VERIFY_ENABLED` off-switch and comment-protection gating as `lnurl-mint`
- [ ] LUD-25 comment protection (note keyed by WALLET-supplied `comment` hash, not the payment preimage)
- [ ] Offline verification (`mintPubkey`, `sig`/`sig2` on rotate/split/merge) — per-mint secp256k1 keypair stored in DB (Option B: portable across all LNbits backends; `mintPubkey` is the mint's own key, not the node's)
- [ ] Sunset mode (`SUNSET_MINT`) — reject mint and split, allow rotate/merge/melt
- [ ] Public one-pager frontend (mint QR, lightning address, mint limits, node info) ported from `lnurl-mint`'s `frontend.py`
- [ ] Management SPA: wallet owner can create/configure their mint (fees, limits, sunset, verify toggle), view outstanding notes, see mint activity
- [ ] Tor / `ONION_URL` base-URL substitution for callback URLs (preserve `public_base_url` semantics)

### Out of Scope

- Bundling `lnurl-wallet` (the SolidJS bearer-note holder SPA) into this extension — it is a separate, mint-agnostic static app; a later phase may serve it as an extension sub-route, but v1 is mint-only
- Direct lnd/cln REST funding (`node.py`'s own backend client) — LNbits already abstracts Lightning per-wallet; a "standalone lnd/cln" mode is explicitly rejected for v1 to keep the extension idiomatic
- A single global mint — per-wallet multi-tenancy is the model; a global admin-only mint is not supported
- Serving the mint's own `.well-known/lnurlp/{username}` / `.well-known/lnurlw/{username}` routes from the extension — Lightning Address (LUD-16) is deferred to v2; it requires structural changes to LNbits' `lnurlp` extension (adding a `withdrawLink` field to `LnurlPayResponse` + a delegation hook in its callback), not just requiring lnurlp to be running. v1 ships raw LNURL/QR instead.
- Node-direct `signmessage` for offline verification (Option A) — rejected in favor of per-mint keypair (Option B) for portability across all LNbits backends (FakeWallet, VoidWallet, etc.); the tradeoff is `mintPubkey` is the mint's key, not the node's, so holders can't cross-verify against the Lightning node pubkey

## Context

**Source implementation** — `~/lnurl-mint` (cloned from `github.com/dni/lnurl-mint`):
- `lnurl_mint/router.py` — all LNURL endpoints, the melt/rotate/split/merge callback, fee math, in-flight melt tracking, reconcile
- `lnurl_mint/db.py` — `NoteStore`: sqlite-backed, module-level lock, store-hashes-not-secrets policy, pending/finalize/restore state machine
- `lnurl_mint/node.py` — lnd/cln REST client (`create_invoice`, `pay_invoice`, `invoice_preimage`, `is_invoice_settled`, `is_payment_complete`, `payment_preimage`, `cached_fetch_node_info`, `signmessage`)
- `lnurl_mint/signing.py` — `mint_pubkey`, `sign_note` (recoverable sig over `h`/`h2`)
- `lnurl_mint/config.py` — settings + `public_base_url` (Tor-aware, Host-header-spoof-proof)
- `lnurl_mint/models.py`, `errors.py`, `mint_log.py`, `error_handler.py`, `frontend.py`, `server.py`
- `tests/` — ~25 tests incl. PoCs (`test_poc_*`) encoding the security guarantees that MUST survive the port

**Reference LNbits extension** — `~/giftcards` (cloned from `github.com/bitkarrot/giftcards`):
- `__init__.py` — `giftcards_ext` router, `giftcards_static_files`, `giftcards_start`/`giftcards_stop` with `create_permanent_unique_task`
- `crud.py` — `Database("ext_giftcards")` pattern, parameterized queries, wallet isolation
- `migrations.py` — `m001_initial`, `m002_*` style migrations
- `views.py` / `views_api.py` — `index`/`index_public` generic views, `WalletTypeInfo` + `require_admin_key`/`require_invoice_key` decorators, LNURL-withdraw endpoint shape
- `services.py` — `pay_invoice(wallet_id=..., payment_request=...)`, `update_wallet_balance` — the LNbits-native funding primitives that replace `node.py`
- `models.py` — pydantic models with validators
- `tasks.py` — background sweep task pattern

**LNbits core** — `~/lnbits`:
- `lnbits/core/services/payments.py` — `create_invoice`, `pay_invoice`, `update_wallet_balance`
- `lnbits/db.py` — `Database` abstraction (SQLite + Postgres)
- `lnbits/tasks.py` — `create_permanent_unique_task`
- `lnbits/decorators.py` — `check_user_exists`, `require_admin_key`, `require_invoice_key`, `WalletTypeInfo`
- `lnbits/core/views/generic.py` — `index`, `index_public`
- `lnbits/extensions/lnurlp/` — the existing Lightning Address / payRequest extension we integrate with
- `docs/devs/extensions.md` — extension structure guide; **forbids adding new dependencies**

**LNURLcash protocol** — LUD-25 draft (`github.com/lnurl/luds/blob/lnurlcash/25.md`): bearer note = LUD-03 withdrawRequest whose `k1` is the asset; mint via LUD-06 payRequest; rotate/split/merge via callback with WALLET-supplied `h`/`h2`; offline verification via `mintPubkey` + recoverable `sig`; melt is async (respond OK, pay in background, burn on settle, restore on confirmed failure, leave pending on unconfirmable).

**Key architectural shifts in the port:**
1. `node.py` (direct lnd/cln REST) → LNbits `create_invoice`/`pay_invoice` (per-wallet). Signing (`signmessage`) needs a new path — LNbits' funding node may not expose it the same way; investigate during research.
2. Standalone sqlite + module-level lock → LNbits `Database` (async, SQLite/Postgres). The confirm-before-burn state machine and in-flight melt tracking must be preserved; the lock discipline likely becomes DB-transaction-based.
3. `server.py` lifespan + monitor → `lnurlmint_start`/`lnurlmint_stop` + `create_permanent_unique_task` for reconcile.
4. Root `.well-known/lnurlp/{user}` → delegate to LNbits `lnurlp` extension; the mint's payRequest is advertised through LNbits' address system.
5. Single global mint → per-wallet mints (each LNbits wallet owns its own mint row + notes).
6. `config.py` settings → per-mint DB rows + extension settings; `public_base_url` Tor-awareness preserved via LNbits' request/base_url handling.

## Constraints

- **Tech stack**: Python (LNbits extension), FastAPI `APIRouter`, pydantic, LNbits `Database` abstraction. Frontend: LNbits-conventional SPA framework (confirm during research — giftcards uses Vue/React via `index`/`index_public` + `static/`).
- **Dependencies**: NO new Python dependencies beyond what LNbits' `pyproject.toml` already provides (per `docs/devs/extensions.md`). `bolt11`, `qrcode`, `bech32`, `coincurve` are used by `lnurl-mint` — verify each is already in LNbits before assuming.
- **Compatibility**: Must run against the LNbits version in `~/lnbits` (check `min_lnbits_version` against giftcards' `1.5.4`).
- **Security**: The confirm-before-burn discipline, store-hashes-not-secrets policy, no-secret-logging rule, and every PoC in `lnurl-mint`'s test suite MUST hold after the port. This is a bearer-asset system — a regression here is a funds-loss bug.
- **Protocol**: LUD-25 draft conformance — endpoint shapes, callback semantics, `h`/`h2` rules, `disposable: false`, `mintPubkey`/`sig` behavior, verify gating all match `lnurl-mint`'s README.
- **Multi-tenancy**: Per-wallet isolation — a wallet's notes are its own; no cross-wallet leakage.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Extension name `lnurlmint` (not `lnurlcash`) | `lnurlcash` is the LUD-25 protocol name; reserving it for the broader ecosystem leaves `lnurlmint` as the clear mint/issuer implementation, matching the existing `lnurl-mint` repo | ✓ Phase 1 — shipped |
| LNbits wallet as funding source (native) | LNbits already abstracts Lightning per-wallet; idiomatic fit, drops `node.py`'s lnd/cln REST client | ✓ Phase 2 — create_invoice/pay_invoice/check_transaction_status used |
| Per-wallet mints | Fits LNbits' multi-tenant model; each user owns their mint instance, fees, limits, notes | ✓ Phase 1 — shipped |
| Integrate with LNbits `lnurlp` for Lightning Address | Avoids `.well-known` route conflicts with LNbits' built-in lnurlp extension; reuses existing address resolution | ⚠️ Revisit — research found lnurlp needs structural changes (withdrawLink field + delegation hook); deferred to v2, v1 ships raw LNURL/QR |
| Full feature parity with `lnurl-mint` for v1 | Mint/melt/rotate/split/merge + verify + offline signing + fees + sunset + comment protection + Tor — all of it | — In progress |
| Port full test suite (security PoCs included) | The PoCs encode real funds-loss guarantees; a line-by-line-behavior port adapted to LNbits fixtures is non-negotiable | — In progress (5 critical PoCs passing in Phase 2, remaining ~20 tests in Phase 7) |
| Management SPA + public one-pager | Per-wallet mints require a UI to create/configure; the public one-pager is the note-holder's face of the mint | — In progress (Phase 1 placeholder, Phase 6 full) |
| `lnurl-wallet` stays separate for v1 | It is a mint-agnostic static SPA in a different framework (SolidJS); bundling it doubles frontend scope and conflates issuer/holder trust domains | — Pending |
| Per-mint keypair for offline verification (Option B) | LNbits' `Wallet` abstraction doesn't expose `signmessage`; a per-mint secp256k1 keypair is portable across all backends (FakeWallet, VoidWallet). Tradeoff: `mintPubkey` is the mint's key, not the node's — holders can't cross-verify against the Lightning node pubkey | ✓ Phase 1 — mint_privkey stored, never exposed in API responses |
| Store-hashes-not-secrets (no preimage column) | Bearer-asset security: never store the note secret, only its hash. Schema enforces this — no preimage/secret/k1 column in any table | ✓ Phase 1 — verified by grep + schema introspection |
| Cross-wallet isolation on every query | Multi-tenancy: a wallet's mints and notes are its own; all CRUD queries are wallet-scoped (WHERE wallet = :wallet or JOIN on mints.wallet) | ✓ Phase 1 — E2E verified on all 5 endpoints |
| DB transaction atomicity via `async with db.connect()` | LNbits `Database` methods open separate transactions; multi-statement ops (e.g., delete_mint check-and-delete) need explicit connection blocks | ✓ Phase 1 — delete_mint uses atomic transaction |
| `MintResponse` excludes `mint_privkey` from API | Code review C-01: mint signing key must never leave the server after creation. Dedicated response model omits it from all 4 endpoints | ✓ Phase 1 — code review fix applied |
| `_UPDATABLE_FIELDS` whitelist in update_mint | Code review W-01: prevent SQL column-name injection by whitelisting updatable columns at the CRUD layer | ✓ Phase 1 — code review fix applied |
| Tristate settlement (paid=True/False/None) | Confirm-before-burn: never restore on `pay_invoice` exception without `_confirm_payment`. `paid=None` leaves pending for reconcile. Uses `status.success`/`status.failed`/`status.paid is None` (NOT `status.pending`) | ✓ Phase 2 — 5 PoC tests passing |
| In-flight melt tracking (asyncio.Lock refcount) | Prevents reconcile from restoring notes whose `pay_invoice` RPC hasn't landed yet. Module-level dict, register after mark_pending, clear in finally | ✓ Phase 2 — TEST-04 passing |
| Background reconcile (60s + boot one-shot) | Resolves stranded notes from crashed processes. Skips in-flight, logs+leaves pending for unconfirmable. `create_permanent_unique_task` + `run_interval` | ✓ Phase 2 — wired in lnurlmint_start/stop |
| Fee math (round up to sat, melt fee limit) | `_mint_fee_msat` rounds UP, `_melt_fee_limit_msat` = max(0.5%, 5000, mint_fee). Protocol contract, not implementation detail | ✓ Phase 2 — verified in services.py |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-08-28 after Phase 2 (mint + melt vertical MVP shipped; tristate settlement, in-flight tracking, background reconcile, 5 PoC tests passing)*
