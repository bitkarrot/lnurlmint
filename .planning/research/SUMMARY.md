# Project Research Summary

**Project:** lnurlmint — LNbits extension implementing LUD-25 lnurlcash (Lightning bearer assets), ported from standalone `lnurl-mint` FastAPI app
**Domain:** Lightning bearer-asset mint / LNbits extension (per-wallet multi-tenant)
**Researched:** 2026-08-28
**Confidence:** HIGH

## Executive Summary

`lnurlmint` is a per-wallet LNbits extension that ports the standalone `lnurl-mint` FastAPI app — a LUD-25 lnurlcash mint issuing Lightning-funded bearer notes that circulate offline as `lnurlw://` withdraw links and can be rotated, split, merged, or melted back to a BOLT-11 payment. The port is a faithful behavioral translation, not a rewrite: every security PoC in `lnurl-mint`'s ~25-test suite must pass against LNbits fixtures. The dominant theme across all four research docs is that the *idiomatic LNbits* path (async `Database`, per-wallet `create_invoice`/`pay_invoice`, `create_permanent_unique_task`, Vue/Quasar SPA) cleanly replaces `lnurl-mint`'s standalone scaffolding — but three load-bearing primitives (the confirm-before-burn tristate, multi-statement DB atomicity, and the `signmessage` signing primitive) require careful, non-obvious translation because LNbits' abstractions were not built with bearer-asset state machines in mind.

The recommended approach is a single-process, per-wallet extension using LNbits core payment APIs and the `Database` abstraction, with the confirm-before-burn state machine reimplemented over `async with db.connect() as conn:` transactions and an in-process `_in_flight_melts` refcount map. The highest-risk port detail is preserving the tristate settlement semantics: LNbits' `pay_invoice` raises `PaymentError` on failure, but the pending/failed/success distinction must be recovered via `check_payment_status` returning `PaymentStatus(paid: bool|None)` — treating `paid=None` as "leave pending," NOT "restore," is the single most security-critical translation. The main open decisions are (1) the offline-verification signing primitive (node-direct `signmessage` vs. per-mint keypair vs. drop for v1), and (2) Lightning Address integration, which both the architecture and pitfalls researchers found is blocked by LNbits' `lnurlp` extension owning `/.well-known/lnurlp` and the `lnurl` library's `LnurlPayResponse` lacking the LUD-25 `withdrawLink` field.

Key risks are all funds-loss class: burning a note without positive settlement confirmation, losing transaction atomicity across the `Database` migration, and cross-wallet note leakage. Mitigation is structural — the confirm-before-burn discipline and in-flight melt registry must be designed before any melt code is written and locked by the ported PoC tests; multi-statement atomic ops must use one `db.connect()` block; every note query must be wallet-scoped. Two dependencies are gray-area: `coincurve` (importable transitively via `pynostr`/`bolt11`/`pyln` but NOT declared in LNbits `pyproject.toml`) and `pydantic-settings` v2 (incompatible — LNbits pins pydantic v1 1.10.26). See Key Tensions below for the decisions that must be reconciled at the requirements/roadmap stage.

## Key Findings

### Recommended Stack

The stack is almost entirely dictated by LNbits: the extension is a Python package discovered by LNbits' loader, contributing an `APIRouter(prefix="/lnurlmint")` — never its own FastAPI app or uvicorn server. All Lightning interaction goes through `lnbits.core.services.payments` (`create_invoice`/`pay_invoice`/`update_wallet_balance`), all persistence through `lnbits.db.Database("ext_lnurlmint")` (async, SQLite + Postgres), all background work through `lnbits.tasks.create_permanent_unique_task` + `run_interval`, and the frontend is Vue 3 + Quasar SFCs served via LNbits' precompiled vendor bundles (NOT SolidJS/React — that's the separate `lnurl-wallet` holder app, out of scope). The no-new-dependencies rule (`docs/devs/extensions.md` line 43) forces four replacements: `uvicorn` → dropped (LNbits hosts); `pydantic-settings` v2 → `lnbits.settings.settings` (pydantic v1) + per-mint DB rows; `qrcode` → `pyqrcode` (already in LNbits, used by `giftcards`); `coincurve` → gray-area (transitive, see Key Tensions). `bolt11`, `bech32`, `httpx`, `loguru` are all already declared in LNbits and reused directly.

**Core technologies:**
- **LNbits extension model (1.5.4)** — hosts the extension as an `APIRouter`; `__init__.py` exports `lnurlmint_ext`, `lnurlmint_start`/`lnurlmint_stop`, `db`. This *is* the project.
- **LNbits `Database` abstraction** — replaces `lnurl-mint`'s raw `sqlite3` + `threading.Lock`. Async, SQLite+Postgres dual support, migration runner. Transaction discipline is the critical port detail (see Key Tensions #4).
- **LNbits core payments API** — `create_invoice`/`pay_invoice`/`update_wallet_balance` replace `node.py`'s direct lnd/cln REST. Per-wallet, backend-agnostic. Tristate settlement recovery is the critical port detail (see Key Tensions #3).
- **pydantic v1 (1.10.26)** — request/response models, DB row models. MUST be v1 syntax (`validator`/`root_validator`, not `field_validator`/`model_validator`). See Key Tensions #6.
- **Vue 3 + Quasar** — management SPA + public one-pager, served as SFCs via LNbits vendor bundles. `giftcards/static/` is the reference.
- **`bolt11` (~=2.1.1)** — BOLT-11 decode for preimage/hash verification (same lib LNbits core uses).
- **`pyqrcode` (~=1.2.1)** — QR generation for the public one-pager (replaces `qrcode`, which is absent from the venv).

### Expected Features

Full LUD-25 behavioral parity with `lnurl-mint` is the v1 target per PROJECT.md — mint, melt, rotate, split, merge, informational `/w`, LUD-21 verify, LUD-25 comment protection, offline verification, mint fees, sunset mode, Tor base-URL substitution, and the full security PoC test suite. The per-wallet multi-tenant model and management SPA are new-for-LNbits surfaces (no equivalent in standalone `lnurl-mint`), with `giftcards` as the structural template.

**Must have (table stakes):**
- Mint (LUD-06 payRequest + `/p/cb`) — core issuance; preimage becomes the note.
- Melt (LUD-03 `/w/cb` with `pr`) + confirm-before-burn + async + background reconciliation — core redemption; highest risk.
- Rotate / Split / Merge (`/w/cb` without `pr`) + `h`/`h2` WALLET-supplied secret hashes — core transforms.
- Informational `/w` (never burns, rejects pending with `"pending"` reason) — spec-required distinct-from-callback.
- Store-hashes-not-secrets + no-secret-logging — security invariants; a regression is silent theft.
- Per-wallet mint model + LNbits `Database` note store (SQLite + Postgres) — the foundation everything sits on.
- LNbits wallet funding (`create_invoice`/`pay_invoice`) — replaces `node.py`.
- Mint fees (`BASE_FEE_MSAT`/`FEE_PERCENT_PPM`, `MIN_MINT_MSAT`, fee-aware bounds) — revenue model + spec.
- LUD-25 comment protection (note keyed by WALLET-supplied `comment` hash) — required for safe verify.
- LUD-21 verify (`VERIFY_ENABLED` off-switch = real 404, comment-protection gating) — PROJECT.md requires full parity.
- Sunset mode — reject mint+split, allow rotate/merge/melt.
- Full test suite (security PoCs) — the acceptance gate for behavioral parity.

**Should have (competitive):**
- Offline verification (`mintPubkey` + recoverable `sig`/`sig2`) — differentiator; contingent on signing-primitive decision (see Key Tensions #2).
- Tor / `ONION_URL` base-URL substitution — full-parity; spoof-proof base URL derivation.
- Management SPA (create/configure mint, view notes) — largest new-for-LNbits surface.
- Public one-pager frontend (mint QR, address, limits, node info) — the note-holder's face of the mint.
- Lightning Address (LUD-16) — BLOCKED for v1 (see Key Tensions #1); both researchers recommend deferring to v2.

**Defer (v2+):**
- Lightning Address via LNbits `lnurlp` — requires an lnurlp-side extension point (`withdrawLink` field + delegation hook); ship raw LNURL/QR for v1.
- Mint-address endpoint (`/.well-known/lnurlw/{user}`) — theoretical, no LUD number; depends on lnurlp routing.
- Node info caching (1h TTL) — optimization; ship live fetch first.
- Serving `lnurl-wallet` as an extension sub-route (static only) — explicitly a later phase.

### Architecture Approach

One module per source counterpart where the boundary survives the port (`signing`, `models`, `errors`), merged where LNbits conventions collapse them (`server.py` lifespan → `__init__.py` start/stop + `tasks.py`; `node.py` → `services.py` + LNbits core; `config.py` → `models.Mint` row + LNbits settings; `frontend.py` → `static/` + `views.py`). Public LNURL wire endpoints (`views_lnurl.py`) are split from key-authenticated management API (`views_api.py`) — different auth model, different router prefix, mirroring `giftcards`' `giftcards_lnurl_router` vs `giftcards_api_router`. `crud.py` is pure wallet-scoped SQL; `services.py` is the funding/orchestration layer (the boundary that replaces `node.py`), keeping the DB lock discipline in one place and Lightning calls in another. Four architectural patterns carry over verbatim: per-wallet multi-tenancy via `wallet_id` scoping; confirm-before-burn state machine over async DB transactions; store-hashes-not-secrets; LNbits lifecycle hooks (`start`/`stop` + `create_permanent_unique_task`).

**Major components:**
1. `__init__.py` — `lnurlmint_ext` APIRouter, `lnurlmint_start`/`lnurlmint_stop` (register reconcile + health tasks), `db`, static files. The LNbits loader contract.
2. `crud.py` — `Database("ext_lnurlmint")`, wallet-scoped SQL. NoteStore-equivalent ops (`create_mint`, `settle_mint`, `swap`, `mark_pending`, `finalize_melt`, `restore`, `pending_melts`) as async functions, each multi-statement op inside one `db.connect()` block.
3. `services.py` — funding via LNbits wallet (`create_invoice`/`pay_invoice`/`check_payment_status`), `_melt_pay` background task, `reconcile_pending_melts`, fee math, in-flight melt registry.
4. `views_lnurl.py` — public LUD-06/03/21/25 endpoints + verify; LUD-01 error shape via route_class.
5. `views_api.py` — management SPA API (`require_admin_key`/`require_invoice_key` decorators).
6. `signing.py` — `mint_pubkey`/`sign_note` (signing primitive TBD — see Key Tensions #2); `verify_note` (coincurve recovery, test-only).
7. `migrations.py` — `m001_initial` (mints + notes + mints_records + melts tables, wallet-scoped).
8. `static/` — Vue 3 + Quasar SPA (management + public one-pager).

### Critical Pitfalls

The top pitfalls are all funds-loss class — a regression is not a crash but silent destruction or duplication of real sats. Full detail in PITFALLS.md; the five most load-bearing:

1. **Burning a note on a guess instead of positive settlement confirmation (confirm-before-burn)** — never restore on `PaymentFailed`/`PaymentError` alone; never finalize on "probably paid"; never destroy on "can't tell." Three terminal states only, gated on positive confirmation via `check_payment_status`. Locked by `test_melt_restore_double_payout_poc.py`. (See Key Tensions #3.)
2. **Reconcile restoring a note whose melt is still live in-process (in-flight melt tracking)** — preserve the `_in_flight_melts` refcount registry; reconcile skips any `payment_hash` in it *without consulting the funding source*. Registry is in-process (not DB), registered before the background task starts, cleared in `finally`. Locked by `test_poc_reconcile_inflight_race.py`.
3. **Persisting a raw secret instead of `sha256(k1)` (store-hashes-not-secrets)** — no preimage column, ever; verify fetches preimage live from the funding source on every call, never cached; disable access logging on `/w`/`/w/cb`/`/verify`. Locked across the whole suite + `test_poc_verify_race.py`.
4. **The informational `/w` advertising a pending note as withdrawable (sell-during-melt scam)** — `/w` must reject pending notes with `{"status":"ERROR","reason":"pending"}` (same string as `/w/cb`); `note_amount`/`note_pending`/`note_spent` are three distinct queries. Locked by `test_poc_f2_pending_info_leak.py`.
5. **Losing transaction atomicity across the `Database` migration** — `db.execute`/`db.fetchone` each open a SEPARATE transaction; multi-statement ops (`swap`, `settle_mint`, `mark_pending`) MUST use one `async with db.connect() as conn:` block or atomicity is lost. Keep the compare-and-set pattern (`UPDATE ... WHERE minted=0` + `rowcount==1`). Locked by `test_poc_a2_settle_race.py`. (See Key Tensions #4.)

Additional pitfalls (full detail in PITFALLS.md): mint fee rounding down / melt fee budget too tight (fee conservation, `test_poc_fee_conservation.py`); the verify observer race (`/verify` handing the preimage to any invoice holder — gated on comment protection + `VERIFY_ENABLED=false` = real 404); duplicate-melt / collision-griefing (reject reused payment hashes before reservation; `swap` collision-checks `mints` table too); cross-wallet note leakage (every query carries `WHERE wallet_id = :wallet`).

## Key Tensions to Reconcile at Requirements/Roadmap Stage

Six tensions emerged across the four research docs and must be reconciled before implementation. They are decisions, not research gaps — the research is conclusive about the *constraints*, but the *choice* belongs to the requirements/roadmap stage.

### 1. Lightning Address / `.well-known/lnurlp` — BLOCKED for v1

The PROJECT.md key decision was "Integrate with LNbits `lnurlp`." But BOTH the architecture and pitfalls researchers found this is blocked for v1:
- LNbits' `lnurlp` extension owns `/.well-known/lnurlp/{user}` via `lnurlp_redirect_paths` middleware (`from_path: /.well-known/lnurlp` → `redirect_to_path: /api/v1/well-known`). Two extensions on the same route = undefined behavior.
- The `lnurl` library's `LnurlPayResponse` (used by `lnurlp`) has **no `withdrawLink` field** — LUD-25's extension to LUD-06 is not part of the standard model. A plain lnurlp PayLink cannot advertise the mint's withdraw side without modifying lnurlp's response model (cross-extension coupling, fragile).

**Both researchers recommend:** defer Lightning Address to v2 / a later phase; ship raw LNURL/QR for v1 (the public one-pager shows the raw LNURL/QR, not a `user@host` address). Lightning Address becomes the schedule-critical path only if it's a v1 must-have — it requires an lnurlp-side PR (a `withdrawLink` field + a delegation hook). **This contradicts the PROJECT.md "Integrate with LNbits lnurlp" decision and the FEATURES.md MVP list (which includes "Lightning Address via LNbits lnurlp" as P1).** Reconcile at requirements stage: either (a) accept v1 ships without Lightning Address (raw LNURL/QR), or (b) accept the lnurlp PR dependency and make it the critical path.

### 2. Offline verification signing primitive — open design decision

LUD-25 offline verification needs `mintPubkey` (the funding node's identity pubkey) + `sign_note` (a recoverable ECDSA signature over `LNURLcash:<amount>:<note_id_hex>` via the node's `signmessage` RPC). **Both researchers verified that LNbits' `Wallet` abstraction does NOT expose `signmessage`** (`lnbits/wallets/base.py:108-154` — `create_invoice`, `pay_invoice`, `status`, `get_invoice_status`, `get_payment_status`, hold-invoice methods, no signing; the `Node` ABC similarly has no signing). The only `signmessage` references in `~/lnbits` are generated lnd gRPC stubs, not wired into the abstraction.

**Option A (Stack researcher recommends):** node-direct `signmessage` via `httpx` using LNbits' lnd/cln REST credentials (`settings.lnd_rest_endpoint`/`settings.lnd_rest_macaroon` or `settings.clnrest_*` rune, all present in `lnbits/settings.py`). Re-implement `node.py`'s small `_sign_message_lnd`/`_sign_message_cln`. `mintPubkey` = node identity via `Node.get_id()`. **Highest spec fidelity** — matches `lnurl-mint` exactly. Uses only `httpx` (declared) for signing; `coincurve` confined to test-only `verify_note`. Tradeoff: re-introduces a sliver of direct node REST (against the "idiomatic LNbits" goal), only works for lndrest/clnrest backends.

**Option B (Architecture researcher recommends):** per-mint keypair in DB (generate a secp256k1 key per mint row, store privkey, sign with `coincurve`/`embit` locally). `mintPubkey` = the mint's own key, not the node's. Fully portable across all LNbits backends (FakeWallet, VoidWallet, etc.). Tradeoff: **deviates from LUD-25** — spec recommends the node identity key so notes verify against the same key that signs BOLT-11s; a holder can't cross-verify against the Lightning node pubkey. Needs `coincurve` (transitive — see Key Tensions #5).

**Option C (fallback):** drop offline verification for v1 — omit `mintPubkey`/`sig`/`sig2`. Spec-conformant (offline verification is optional in LUD-25) but feature-reduced. No dep risk.

**This is a requirements-stage decision.** Both options agree signing failures are swallowed (never block a rotate/split/merge). The choice is between spec fidelity + lnd/cln-only (A) vs. portability + spec deviation (B) vs. feature reduction (C).

### 3. `pay_invoice` tristate fidelity — the single highest-risk port detail

`lnurl-mint`'s `is_payment_complete` returns a **tristate**: `True`/`False`/`None`-or-raise — and `None`/raise means "can't tell yet," NEVER `False`. This is the whole point: a hodl-invoice HTLC held open must raise, not resolve to `False`, or the confirm-before-burn discipline breaks (double-payout bug).

LNbits' `pay_invoice` (`lnbits.core.services.payments.pay_invoice`) raises `PaymentError` on failure and returns `Payment` with `.status` (`PaymentState.PENDING`/`SUCCESS`/`FAILED`) on success. The tristate must be **recovered** via `check_payment_status(payment)` → `funding_source.get_payment_status(checking_id)` → `PaymentStatus(paid: bool|None, fee_msat, preimage)`: `paid=True` → confirmed paid; `paid=False` → confirmed not paid; `paid=None` → pending.

**The critical discipline:** on any `pay_invoice` raise, the port MUST call `check_payment_status` and treat `paid is None` (pending) as **"leave the note pending,"** NOT "restore." Treating `PaymentError` as "confirmed not paid" and restoring reintroduces the double-payout bug from Pitfall 1 — a hodl-invoice attacker captures the note's value *and* still settles the held HTLC later. A naive `except PaymentError: notes.restore(...)` with no `check_payment_status` follow-up is the vulnerable shape. Preserve the `_confirm_payment` retry-with-backoff for the `paid is None`/raise case. **Research spike first:** confirm `PaymentStatus.paid` tristate behavior against the actual `~/lnbits` funding source (VoidWallet won't exercise it; test against a fake backend returning `paid=None`). Locked by porting `test_melt_restore_double_payout_poc.py` against LNbits payment mocks that return `paid=None`.

### 4. DB transaction atomicity — replaces the module-level lock

`lnurl-mint`'s `NoteStore` does every burn+mint op under `with self._lock, self.conn:` — one `threading.Lock` + one sqlite connection = one atomic transaction. `swap` burns N notes and mints M in one transaction; `settle_mint` does compare-and-set `UPDATE ... WHERE minted=0` + `INSERT notes` in one transaction; `mark_pending` reserves N notes in one transaction.

LNbits' `Database` is different in a way that **silently breaks this**: `db.execute`/`db.fetchone`/`db.insert`/`db.update` each call `async with self.connect()` — **each call is its own lock acquisition + its own connection + its own transaction.** Calling `db.execute(...)` three times in a row is three separate transactions with the lock released between them. The atomic burn+mint of `swap` becomes non-atomic; a concurrent request can interleave and double-spend/double-mint.

**The fix:** for every multi-statement atomic operation (`swap`, `settle_mint`, `mark_pending`, `finalize_melt`, `restore`, `create_mint` with collision check), open **one** `async with db.connect() as conn:` block and run all statements via `conn.execute(...)` / `conn.fetchone(...)` (the `Connection` methods), NOT `db.execute(...)`. This holds the `asyncio.Lock` and the DB connection/transaction for the whole operation. Keep the compare-and-set pattern (`UPDATE ... WHERE minted=0` with `rowcount==1`) — it's the real protection against the settle race and survives multi-process Postgres (the asyncio.Lock alone does not). For Postgres, consider `SELECT ... FOR UPDATE` on note rows in `swap`/`mark_pending`. Do NOT add a second `threading.Lock` — LNbits is async-single-loop; mixing risks deadlock. The `_in_flight_melts` registry stays in-process (an `asyncio.Lock`-guarded `dict`), NOT in the DB. **This replaces `lnurl-mint`'s module-level `threading.Lock` + sqlite `with conn:`.** Locked by `test_poc_a2_settle_race.py` and `test_poc_a3_mark_pending.py`.

### 5. `coincurve` availability — transitive, not declared

`coincurve` 20.0.0 is importable in the LNbits venv (transitive via `pynostr` per `poetry.lock`, and also pulled by `bolt11`/`pyln`), and LNbits' own code imports it directly (`wallets/nwc.py`, `wallets/sparkl2.py`, `utils/nostr.py`). But it is **NOT declared** in LNbits `pyproject.toml`. Using it directly violates the spirit of the no-new-deps rule and is fragile — removing `pynostr` (or the transitive chain) would break us. **No declared LNbits dep can replace it for recoverable-signature verification** — `embit`'s `Signature` has no recovery id; `embit.PublicKey` has no `from_signature_and_message`.

`coincurve` is needed for: (a) `verify_note` in the test suite (pubkey recovery from signature+message — `PublicKey.from_signature_and_message`), and (b) Option B signing (per-mint keypair). If Option A (node-direct signing) is chosen, `coincurve` use is confined to test-only `verify_note` — production signing is done by the node, verification by the holder's wallet. The transitive-dep risk is then real but bounded: (1) LNbits' own `nwc.py`/`nostr.py` already hard-depend on `coincurve`, so it won't be removed lightly; (2) if it is, only `verify_note` (test-only) breaks, not production. If the no-new-deps rule is interpreted strictly (transitive deps forbidden too), Option C (drop offline verification for v1) is the safe fallback and Option B the upgrade path. **Flag this as a Key Decision.**

### 6. pydantic v1 (NOT v2) — LNbits pins 1.10.26

LNbits pins **pydantic v1**: `pydantic~=1.10.26` (`pyproject.toml` line 17, verified `pydantic.VERSION == 1.10.26` in venv), with `[tool.pydantic-mypy]` v1 config. `lnurl-mint` uses `pydantic-settings` (a v2 library) and v2 syntax (`field_validator`/`model_validator`/`SettingsConfigDict`) in `config.py`. **All models and settings must be rewritten to pydantic v1 syntax.** Use `BaseModel` + `validator` (not `field_validator`) + `root_validator` (not `model_validator`) + `class Config` (not `SettingsConfigDict`). For settings, use `from lnbits.settings import settings` (a pydantic-v1 `BaseSettings` already instantiated) — per-mint config (fees, limits, sunset, verify toggle, username, onion_url) becomes DB rows on the `mints` table, not env vars. `pydantic-settings` v2 is **NOT AVAILABLE** and cannot be added (incompatible with the v1 pin). This is a mechanical but pervasive port task.

## Implications for Roadmap

Based on research, suggested phase structure. The defining constraint is that the confirm-before-burn state machine, DB transaction discipline, and in-flight melt registry are one inseparable mechanism — they must land together in Phase 1 or not at all. The signing primitive and Lightning Address are the two open decisions that shape whether Phase 1 includes offline verification / LUD-16 or defers them.

### Phase 1: Core Mint — Scaffold, Per-Wallet Model, Mint + Melt + Transforms + Security PoCs
**Rationale:** The confirm-before-burn state machine, DB transaction atomicity, and in-flight melt tracking are the load-bearing funds-loss guards. They must be designed before any melt code is written and locked by the ported PoC suite. Everything else depends on the per-wallet mint model + `Database` note store existing first.
**Delivers:** A working LUD-25 mint: extension scaffold, per-wallet mint rows, `Database` note store with correct transaction discipline, mint (`/p/cb`), informational `/w`, melt (`/w/cb` with `pr`) + confirm-before-burn + async + background reconciliation, rotate/split/merge (`/w/cb` without `pr`) + `h`/`h2`, store-hashes-not-secrets, mint fees, sunset mode, LUD-25 comment protection, LUD-21 verify (gated), and the full ported security PoC test suite as the acceptance gate.
**Addresses:** All table-stakes features; the five critical pitfalls (1–5) + pitfalls 6–10.
**Avoids:** Funds-loss bugs by locking the state machine with PoCs before any feature surface is added; atomicity loss by designing the `db.connect()` transaction discipline at schema/CRUD time.
**Open decisions to resolve before/during this phase:** signing primitive (Key Tensions #2/#5 — determines whether offline verification lands in Phase 1 or is deferred); Lightning Address (Key Tensions #1 — recommended defer to v2, ship raw LNURL/QR).

### Phase 2: Management SPA + Public One-Pager
**Rationale:** Per-wallet mints require a UI to create/configure; the public one-pager is the note-holder's face of the mint. Both depend on the per-wallet mint model (Phase 1) existing. Frontend is the largest new-for-LNbits surface and is independent of the funds-loss-critical backend.
**Delivers:** Vue 3 + Quasar management SPA (create/configure mint: fees, limits, sunset, verify toggle, username, onion_url; view outstanding notes; mint activity) + public one-pager (mint QR, raw LNURL, limits, node info). Tor/`ONION_URL` base-URL substitution (spoof-proof derivation from mint row's `base_url`/`onion_url`).
**Uses:** Vue 3 + Quasar vendor bundles, `giftcards/static/` as structural template, `index`/`index_public` generic views, `pyqrcode` for QR.
**Implements:** `views_api.py` (mgmt API, `require_admin_key`/`require_invoice_key`), `views.py` (generic views), `static/` SPA.
**Addresses:** Management SPA + public one-pager + Tor features; cross-wallet isolation tests on management endpoints (Pitfall 10).

### Phase 3: Offline Verification + Lightning Address (v2 — contingent)
**Rationale:** Both are blocked by open decisions (Key Tensions #1, #2). Offline verification depends on the signing-primitive choice; Lightning Address depends on an lnurlp-side extension point (`withdrawLink` field + delegation hook) that requires an upstream PR. Neither is funds-loss-critical; both are differentiators.
**Delivers:** (If Option A/B chosen) `mintPubkey` + `sig`/`sig2` on rotate/split/merge responses; (if Lightning Address unblocked) LUD-16 address resolution via LNbits `lnurlp` with `withdrawLink` injection.
**Uses:** `httpx` (Option A) or `coincurve` (Option B) for signing; lnurlp extension point for Lightning Address.
**Research flags:** Both need deeper research during planning — signing primitive empirical verification against the real funding source; lnurlp delegation hook design.

### Phase Ordering Rationale

- **Phase 1 first** because the confirm-before-burn state machine + DB transaction discipline + in-flight registry are one mechanism that cannot be shipped incrementally — a pending note left by a crashed melt must be resolvable or it's a permanent funds freeze. The PoC suite is the acceptance gate and must run against the real backend.
- **Phase 2 second** because the frontend depends on the per-wallet mint model + management API existing (Phase 1), but is independent of the funds-loss-critical backend — it can be built in parallel once the API contract is stable, but lands after Phase 1 validates the core.
- **Phase 3 last** because both features are blocked by open decisions and are differentiators, not table stakes. Deferring them avoids forcing an lnurlp PR or a signing-primitive commitment onto the v1 critical path.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 1 (signing primitive):** empirical verification of `PaymentStatus.paid` tristate behavior against the actual `~/lnbits` funding source (VoidWallet won't exercise it; need a fake backend returning `paid=None`); `coincurve` transitive-dep risk assessment; per-backend `preimage` availability on `get_invoice_status` vs `get_payment_status`.
- **Phase 3 (Lightning Address):** lnurlp delegation hook design — requires understanding `lnurlp`'s `get_address_data` / `api_lnurl_response` internals and whether a `withdrawLink` field can be injected without forking lnurlp.

Phases with standard patterns (skip research-phase):
- **Phase 1 (scaffold, CRUD, fee math, sunset, comment protection, verify gating):** well-documented LNbits extension patterns (`giftcards` reference) + mechanical port from `lnurl-mint` source.
- **Phase 2 (frontend):** standard Vue 3 + Quasar SPA pattern, `giftcards/static/` is a direct template.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All versions verified against `~/lnbits/pyproject.toml` + live venv; API signatures read from source. `coincurve` transitive status is the one gray area. |
| Features | HIGH | Source `lnurl-mint` README + `router.py` read in full; reference `giftcards` extension read in full; LUD-25 draft referenced. |
| Architecture | HIGH | Source app + reference extension + LNbits core all read in full; port mapping verified against actual abstractions. Signing primitive + Lightning Address are open decisions, not gaps. |
| Pitfalls | HIGH | Drawn from `lnurl-mint` README security model, source code, nine PoC test files, LNbits `Database`/`payments`/`wallets` source. |

**Overall confidence:** HIGH

### Gaps to Address

- **Signing primitive (Key Tensions #2):** requirements-stage decision between Option A (node-direct, spec-fidelity, lnd/cln-only), Option B (per-mint keypair, portable, spec-deviation), Option C (drop for v1). Empirical research spike needed: confirm `PaymentStatus.paid` tristate + `signmessage` REST reachability against the real `~/lnbits` funding source.
- **Lightning Address (Key Tensions #1):** requirements-stage decision — accept v1 ships without LUD-16 (raw LNURL/QR, both researchers recommend) OR accept the lnurlp PR dependency as critical path. Contradicts PROJECT.md decision + FEATURES.md MVP list.
- **`coincurve` transitive-dep risk (Key Tensions #5):** bounded for Option A (test-only use), material for Option B (production use). Confirm whether the no-new-deps rule forbids transitive deps; if so, Option C is the fallback.
- **Per-backend `preimage` availability:** LNbits backends vary in whether `get_invoice_status` populates `preimage` (some only fill it on `get_payment_status`). Verify endpoint must tolerate `preimage=None` (source already does); validate per-backend during Phase 1 implementation.
- **Postgres `SELECT ... FOR UPDATE`:** the asyncio.Lock serializes within one process, but a multi-process LNbits deployment defeats it. The compare-and-set (`UPDATE ... WHERE minted=0` + `rowcount==1`) survives; confirm whether `FOR UPDATE` is needed for `swap`/`mark_pending` under Postgres.

## Sources

### Primary (HIGH confidence)
- `~/lnbits/pyproject.toml` — all LNbits dependency versions (lines 9–54); pydantic v1 pin (line 17); no-new-deps rule context.
- `~/lnbits/.venv` — runtime verification: `pydantic.VERSION == 1.10.26`, `coincurve` importable + `PublicKey.from_signature_and_message` present, `qrcode` absent, `pyqrcode` present, `embit.ec` lacks pubkey recovery.
- `~/lnbits/lnbits/db.py` — `Database`/`Connection` API (lines 134–409); `Compat` cross-DB helpers (58–131); per-call transaction behavior (the atomicity pitfall).
- `~/lnbits/lnbits/core/services/payments.py` — `create_invoice` (247), `pay_invoice` (58), `update_wallet_balance` (454), `check_payment_status`.
- `~/lnbits/lnbits/wallets/base.py` — `Wallet` ABC (108–154); `Feature` enum (18); **no `signmessage`** (verified by grep).
- `~/lnbits/lnbits/nodes/base.py` — `Node` ABC (153), `get_id` (168); **no signing**.
- `~/lnbits/lnbits/tasks.py` — `create_permanent_unique_task` (39), `run_interval` (152), `register_invoice_listener` (79).
- `~/lnbits/docs/devs/extensions.md` — no-new-deps rule (line 43), extension structure (33–39).
- `~/lnurl-mint/lnurl_mint/router.py` — endpoint behavior, fee math, in-flight melt tracking, reconcile, verify gating, comment protection (read 1–782).
- `~/lnurl-mint/lnurl_mint/db.py` — `NoteStore` with `threading.Lock` (41), `_add_column_if_missing` (110), `swap` atomicity (243).
- `~/lnurl-mint/lnurl_mint/signing.py` — `mint_pubkey` (30), `sign_note` (51), `verify_note` (80, uses `coincurve`).
- `~/lnurl-mint/lnurl_mint/node.py` — lnd/cln REST client incl. `sign_message` (175), `_sign_message_lnd` (328), `_sign_message_cln` (464).
- `~/lnurl-mint/tests/test_poc_*.py` — nine PoC test files encoding the funds-loss guarantees.
- `~/giftcards/` — reference extension: `__init__.py`, `crud.py`, `services.py`, `migrations.py`, `views.py`, `views_api.py`, `tasks.py`, `config.json`, `static/`.

### Secondary (MEDIUM confidence)
- `~/lnbits/poetry.lock` — `coincurve` 20.0.0 pulled transitively by `pynostr` (line 1026+).
- `~/lnbits/lnbits/extensions/lnurlp/` — owns `/.well-known/lnurlp` via redirect middleware; `LnurlPayResponse` lacks `withdrawLink` (the Lightning Address blocker).
- `~/lnbits/static/vendor.json` — Vue/Quasar/Vuex/vue-router vendor bundles (frontend framework confirmation).

### Tertiary (LOW confidence)
- LUD-25 draft (`github.com/lnurl/luds/blob/lnurlcash/25.md`) — referenced via PROJECT.md; offline verification spec-recommends node identity key (informs Option A vs B tradeoff).

---
*Research completed: 2026-08-28*
*Ready for roadmap: yes*
