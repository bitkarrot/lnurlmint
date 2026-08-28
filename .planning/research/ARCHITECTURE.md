# Architecture Research

**Domain:** LNbits extension porting — LUD-25 lnurlcash (Lightning bearer assets) mint, ported from standalone `lnurl-mint` FastAPI app to a per-wallet LNbits extension (`lnurlmint`)
**Researched:** 2026-08-28
**Confidence:** HIGH (source app and reference extension both read in full; LNbits core services/wallets/db abstractions verified against `~/lnbits`)

---

## Standard Architecture

### System Overview — Source `lnurl-mint` (standalone FastAPI)

```
┌──────────────────────────────────────────────────────────────────┐
│                       HTTP / LNURL wire layer                     │
│  ┌────────────────┐  ┌──────────────┐  ┌───────────────────────┐ │
│  │ router.py      │  │ frontend.py  │  │ error_handler.py      │ │
│  │ LUD-06/03/21/  │  │ one-pager    │  │ (route_class wrapper) │ │
│  │ 16/25 endpoints│  │ GET /        │  │ → LNURL error shape   │ │
│  └───────┬────────┘  └──────────────┘  └───────────────────────┘ │
│          │                                                        │
├──────────┴────────────────────────────────────────────────────────┤
│                       Domain / state machine                       │
│  ┌────────────────┐  ┌──────────────┐  ┌───────────────────────┐ │
│  │ db.py          │  │ signing.py   │  │ mint_log.py / errors  │ │
│  │ NoteStore      │  │ mint_pubkey  │  │ .py (operator-only    │ │
│  │ + module lock  │  │ sign_note    │  │ side-channel logs)    │ │
│  │ + pending/final│  │ verify_note  │  │                       │ │
│  │ /restore SM    │  │ (coincurve)  │  │                       │ │
│  └───────┬────────┘  └──────┬───────┘  └───────────────────────┘ │
│          │                  │                                      │
├──────────┴──────────────────┴────────────────────────────────────┤
│                       Funding / node integration                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ node.py — lnd/cln REST client (httpx)                      │  │
│  │ create_invoice / pay_invoice / is_invoice_settled /        │  │
│  │ is_payment_complete / invoice_preimage / payment_preimage /│  │
│  │ sign_message / fetch_node_info (cached)                    │  │
│  └────────────────────────────────────────────────────────────┘  │
│          │                                                        │
├──────────┴────────────────────────────────────────────────────────┤
│                       Process / lifecycle                          │
│  ┌────────────────┐  ┌──────────────────────────────────────────┐│
│  │ server.py      │  │ config.py — Settings (pydantic-settings) ││
│  │ lifespan +     │  │ + public_base_url (Tor-aware, Host-      ││
│  │ monitor task   │  │ spoof-proof) + funding_source()          ││
│  │ (reconcile)    │  │                                          ││
│  └────────────────┘  └──────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

### System Overview — Target `lnurlmint` LNbits extension

```
┌──────────────────────────────────────────────────────────────────┐
│            LNbits app (loads extension routers/static/tasks)       │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ lnurlmint/__init__.py                                       │ │
│  │  lnurlmint_ext (APIRouter prefix=/lnurlmint)                │ │
│  │  lnurlmint_static_files, lnurlmint_start/stop               │ │
│  └──────┬──────────────────────────────────────────────────────┘ │
│         │                                                          │
├─────────┴──────────────────────────────────────────────────────────┤
│  HTTP layer                                                        │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐ │
│  │ views_api.py │ │ views_lnurl  │ │ views.py     │ │ static/  │ │
│  │ mgmt SPA API │ │ LUD-06/03/21 │ │ index/       │ │ (Vue/    │ │
│  │ (admin/invoice│ │ /25 endpoints│ │ index_public)│ │  SPA)   │ │
│  │  key dec.)   │ │  + verify    │ │ generic views│ │          │ │
│  └──────┬───────┘ └──────┬───────┘ └──────────────┘ └──────────┘ │
│         │                │                                         │
├─────────┴────────────────┴─────────────────────────────────────────┤
│  Domain layer                                                       │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐ │
│  │ crud.py      │ │ services.py  │ │ signing.py   │ │ models.py│ │
│  │ Database(    │ │ create/      │ │ mint_pubkey/ │ │ pydantic │ │
│  │ "ext_lnurl-  │ │ pay via      │ │ sign_note    │ │ + Mint   │ │
│  │  mint")      │ │ LNbits wallet│ │ (HARD — see  │ │  row     │ │
│  │ wallet-scoped│ │ + reconcile  │ │  signing)    │ │  config) │ │
│  └──────┬───────┘ └──────┬───────┘ └──────────────┘ └──────────┘ │
│         │                │                                         │
├─────────┴────────────────┴─────────────────────────────────────────┤
│  LNbits core (shared, not extension-owned)                          │
│  ┌──────────────────┐ ┌───────────────┐ ┌────────────────────────┐│
│  │ core/services/   │ │ wallets/      │ │ core/crud/payments.py  ││
│  │ payments.py      │ │ base.Wallet   │ │ get_payment(hash) →    ││
│  │ create_invoice/  │ │ (funding src) │ │ Payment{preimage,status}│
│  │ pay_invoice      │ │ get_*_status  │ │                        ││
│  └──────────────────┘ └───────┬───────┘ └────────────────────────┘│
│         │                      │                                    │
│  ┌──────┴──────────────────────┴────────────────────────────────┐ │
│  │ extensions/lnurlp — owns /.well-known/lnurlp/{user}           │ │
│  │ (redirect_paths) + PayLink table + Lightning Address resolve  │ │
│  └───────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities — Source `lnurl-mint`

| Component | Responsibility | Boundary / Talks to |
|-----------|----------------|---------------------|
| `router.py` | All LNURL endpoints: `/.well-known/lnurlp/{user}` (payRequest), `/p/cb` (mint callback), `/w` (withdrawRequest info), `/w/cb` (melt/rotate/split/merge callback), `/verify/{hash}` (LUD-21), `/.well-known/lnurlw/{user}` (mint-address). Fee math, in-flight melt tracking (`_in_flight_melts`), `_melt_pay` background task, `reconcile_pending_melts`. | db.NoteStore, node.*, signing.*, config.settings, mint_log, errors |
| `db.py` (`NoteStore`) | Outstanding bearer notes + pending mints + melt records. Store-hashes-not-secrets. Confirm-before-burn state machine: `mark_pending` → `finalize_melt`/`restore`. All-or-nothing `swap` (burn+mint atomic). `threading.Lock` + sqlite `with conn:` transactions. | sqlite file, config (path), errors (log_internal_error) |
| `node.py` | lnd/cln REST client over httpx. `create_invoice` (supplies own preimage), `pay_invoice` (+`PaymentFailed`), `is_payment_complete`/`is_invoice_settled` (3-way: True/False/raise-on-pending), `invoice_preimage`/`payment_preimage`, `sign_message`, `fetch_node_info` (cached). | lnd/cln REST endpoints, config (LightningBackendConfig) |
| `signing.py` | LUD-25 offline verification: `mint_pubkey` (node identity), `sign_note` (recoverable sig over `h`/`h2`+amount via node signmessage), `verify_note` (coincurve recovery, test-only). | node.sign_message / fetch_node_info, coincurve |
| `config.py` | `Settings` (pydantic-settings, env/.env). `public_base_url` (Tor-aware, Host-spoof-proof — derives from settings not request). `funding_source()` → `LightningBackendConfig`. | env, node.LightningBackendConfig |
| `server.py` | FastAPI app + `lifespan`: disables uvicorn access log (k1-in-URL theft), boot funding-source check, spawns `_monitor_funding_source` (health transitions + reconcile on every healthy tick). | router, frontend, node, config, reconcile_pending_melts |
| `models.py` | pydantic LNURL wire models incl. LUD-25 extensions (`withdrawLink`, `mintPubkey`, `sig`/`sig2`, `verify`, `disposable: false`). | — |
| `errors.py` / `mint_log.py` | Operator-only side channels: `log_internal_error` (error.log, ref-id, never caller-facing), `log_mint`/`log_melt` (mint.log accounting). | config (db path for log location) |
| `error_handler.py` | `LnurlErrorResponseHandler` route_class — converts raised HTTPException into LUD-01 `{"status":"ERROR","reason":...}` wire shape. | router |
| `frontend.py` | One-pager HTML (mint QR, lightning address, limits, node info). | router-fee-helpers, node.cached_fetch_node_info |

### Component Responsibilities — Target `lnurlmint` extension

| Component | Responsibility | Boundary / Talks to |
|-----------|----------------|---------------------|
| `__init__.py` | `lnurlmint_ext` APIRouter (prefix `/lnurlmint`), `lnurlmint_static_files`, `lnurlmint_start`/`lnurlmint_stop` (register `create_permanent_unique_task` for reconcile + health monitor). | LNbits loader, tasks, crud.db |
| `crud.py` | `Database("ext_lnurlmint")`. All SQL, **wallet-scoped**. NoteStore-equivalent ops reimplemented as async functions: `create_mint`, `settle_mint`, `swap`, `mark_pending`, `finalize_melt`, `restore`, `pending_melts`, mint/melt record lookups. Mint row CRUD. | LNbits `Database` (async, SQLite/Postgres) |
| `services.py` | Funding via LNbits wallet abstraction: `create_invoice(wallet_id=...)` (mint), `pay_invoice(wallet_id=...)` (melt), settlement/preimage lookups via `get_payment(hash)` / `check_payment_status`, `_melt_pay` background, `reconcile_pending_melts`. | `lnbits.core.services.payments`, `lnbits.core.crud.payments`, `lnbits.wallets` (funding source), crud |
| `signing.py` | LUD-25 offline verification — **HARD PROBLEM**: LNbits `Wallet` abstraction has no `signmessage`. See Hard Problems. | signing primitive (TBD), crud (mint row for pubkey) |
| `views_api.py` | Management SPA API: create/configure mint (fees, limits, sunset, verify, username), list outstanding notes, mint activity. `require_admin_key`/`require_invoice_key` decorators, `WalletTypeInfo`. | services, crud, models |
| `views_lnurl.py` | Public LNURL wire endpoints: `/lnurlmint/{mint_id}/p/cb`, `/w`, `/w/cb`, `/verify/{hash}`. LUD-01 error shape via route_class or per-handler. | services, crud, signing, models |
| `views.py` | Generic views: `index` (mgmt SPA, `check_user_exists`), `index_public` (public one-pager). | `lnbits.core.views.generic` |
| `migrations.py` | `m001_initial` (mints + notes + melts tables, wallet-scoped), `m002_*` style. | `db` |
| `models.py` | pydantic: `Mint` (per-wallet config row), `Note`/`MintRecord`/`MeltRecord`, LNURL wire models (ported), mgmt API models. | — |
| `tasks.py` | `wait_for_reconcile` / `wait_for_health` — `run_interval` wrappers for `create_permanent_unique_task`. | `lnbits.tasks`, services |
| `config.json` / `manifest.json` | Extension metadata (`min_lnbits_version`, name, tile). | LNbits loader |
| `static/` | SPA bundle (mgmt + public one-pager). | — |

---

## Recommended Project Structure

```
lnurlmint/
├── __init__.py            # lnurlmint_ext router, start/stop, static_files
├── config.json            # extension metadata (min_lnbits_version, name)
├── manifest.json          # repo manifest
├── migrations.py          # m001_initial (mints/notes/melts), m002_*
├── models.py              # Mint, Note, MintRecord, MeltRecord, LNURL wire models
├── crud.py                # Database("ext_lnurlmint"), wallet-scoped SQL
├── services.py            # funding via LNbits wallet, _melt_pay, reconcile, fees
├── signing.py             # mint_pubkey / sign_note (signing primitive TBD)
├── tasks.py               # run_interval wrappers for reconcile + health
├── views.py               # generic index / index_public (SPA shells)
├── views_api.py           # mgmt SPA API (admin/invoice key)
├── views_lnurl.py         # public LUD-06/03/21/25 endpoints + verify
├── errors.py              # log_internal_error (operator-only; mint_log folded in)
└── static/                # SPA bundle (mgmt + public one-pager)
```

### Structure Rationale

- **One module per source counterpart** where the boundary survives the port (`signing`, `models`, `errors`); **merged** where LNbits conventions collapse them (`server.py` lifespan → `__init__.py` start/stop + `tasks.py`; `node.py` → `services.py` + LNbits core; `config.py` → `models.Mint` row + LNbits settings; `frontend.py` → `static/` + `views.py`).
- **`views_lnurl.py` separate from `views_api.py`**: public unauthenticated LNURL wire endpoints vs. key-authenticated management API — different auth model, different router prefix, no shared decorators. Mirrors giftcards' `giftcards_lnurl_router` vs `giftcards_api_router` split.
- **`crud.py` is pure SQL + wallet-scoping**; **`services.py` is the funding/orchestration layer** (the boundary that replaces `node.py`). This keeps the DB lock discipline in one place and the Lightning calls in another, matching giftcards' `crud`/`services` split.

---

## Architectural Patterns

### Pattern 1: Per-wallet multi-tenancy via `wallet_id` scoping

**What:** Every table that holds mint state carries a `wallet` FK to the owning LNbits wallet; every query includes `WHERE wallet = :wallet`. A mint *is* a per-wallet config row.
**When to use:** Always — this is the LNbits multi-tenant model. There is no global mint.
**Trade-offs:** +No cross-wallet leakage possible at the query layer. −Every LNURL endpoint must resolve `mint_id` → `wallet_id` first (one extra lookup) before any note operation.

**Example:**
```python
# crud.py — every note query is wallet-scoped via the mint row
async def get_note(mint_id: str, note_id: str) -> Note | None:
    return await db.fetchone(
        "SELECT n.* FROM lnurlmint.notes n "
        "JOIN lnurlmint.mints m ON n.mint_id = m.id "
        "WHERE n.id = :nid AND m.wallet = :wid",
        {"nid": note_id, "wid": <mint.wallet>}, Note)
```

### Pattern 2: Confirm-before-burn state machine over async DB transactions

**What:** A melt never burns a note until its outgoing payment is *positively confirmed* settled. Notes are reserved (`pending=1`) → reply `{"status":"OK"}` immediately (LUD-03 step 6) → background task pays → `finalize_melt` (burn) on confirmed success, `restore` on confirmed failure, **left pending** if unconfirmable.
**When to use:** Every melt. This is the funds-loss guard.
**Trade-offs:** +A crashed/restarted process can't double-spend or vanish a note. −Requires a reconcile task to resolve notes left pending across a restart; requires a live settlement-lookup path that distinguishes pending from failed (see Hard Problems).

**Example:**
```python
# services.py — the lock is the Database's asyncio.Lock, held for the whole tx
async with db.connect() as conn:           # holds db.lock (asyncio.Lock)
    await conn.execute("UPDATE ... SET pending=1 ...", {...})  # mark_pending
# ... reply OK, then background:
#   on confirmed success -> finalize_melt (spent=1, pending=0)
#   on confirmed failure -> restore       (pending=0)
#   on unconfirmable     -> leave pending (reconcile picks up later)
```

### Pattern 3: Store-hashes-not-secrets

**What:** A note's storage id is `sha256(k1)` (hex). For a minted note that's the funding invoice's payment hash; for a comment-protected mint it's the WALLET-chosen `comment_hash`; for a rotate/split/merge it's the WALLET-generated `h`/`h2`. The spendable secret is never persisted or logged.
**When to use:** Everywhere a note is identified in DB/logs.
**Trade-offs:** +A leaked DB reveals counts/values but not spendable notes. −Mint settlement/preimage must be fetched live from the funding source for LUD-21 verify (never cached) — preserved exactly from source.

### Pattern 4: LNbits lifecycle hooks (`start`/`stop` + `create_permanent_unique_task`)

**What:** `server.py`'s `lifespan` (boot check + monitor task) becomes `lnurlmint_start()` registering a `create_permanent_unique_task("ext_lnurlmint", wait_for_reconcile)`; `lnurlmint_stop()` cancels it. No FastAPI `lifespan` — LNbits owns the app.
**When to use:** All background work.
**Trade-offs:** +Idiomatic; survives LNbits' own startup ordering. −No boot-time "funding source reachable?" gate that blocks serving (LNbits starts the extension after the app is up); the boot reconcile must run inside the task's first tick, guarded.

---

## Data Flow (LNbits context)

### Mint (pay invoice → note created)

```
Wallet pays invoice
   ↓ (Lightning settlement, async)
[1st lazy touch] GET /lnurlmint/{mint}/w?k1=<preimage>   (or /verify/{hash})
   ↓ views_lnurl._resolve_note(k1)
   ↓ _note_amount_by_id(note_id=sha256(k1))
   ↓ crud.note_amount → None (not yet materialized)
   ↓ _mint_settled(payment_hash) → check_payment_status / get_payment(hash).success
   ↓ crud.settle_mint(payment_hash)  → INSERT note under payment_hash (or comment_hash)
   ↓ log_mint(...)
   ← returns note value → withdrawRequest response
```
- **Change vs source:** `is_invoice_settled` → LNbits `get_payment(payment_hash).success` (DB) or `check_payment_status` (live `funding_source.get_invoice_status`). The note is materialized lazily on first `/w` or `/verify` poll after settlement, exactly as in source. `create_invoice` (the `/p/cb` callback) uses LNbits `core.services.payments.create_invoice(wallet_id=mint.wallet, ...)` and reads `payment.preimage` from the returned `Payment` — the preimage is NOT supplied by the extension (LNbits' `create_invoice` has no `r_preimage` param); it relies on the backend returning it. `crud.create_mint` stores `payment_hash` + `pr` + net amount + optional `comment_hash`, scoped to the mint row.

### Melt (callback → background pay → burn/restore)

```
GET /lnurlmint/{mint}/w/cb?k1=...&pr=<invoice>
   ↓ views_lnurl.get_withdraw_callback
   ↓ resolve notes, validate pr amount == total, reject self-mint / reused-hash
   ↓ crud.mark_pending(note_ids, payment_hash)   [async with db.connect() — atomic]
   ↓ _track_melt_start(payment_hash)             [in-process refcount map]
   ↓ crud.record_melt(payment_hash, pr)
   ← reply {"status":"OK"} (+ verify URL if enabled)   [LUD-03 step 6 — immediate]
   ↓ BackgroundTasks / task: services._melt_pay
        ↓ pay_invoice(wallet_id=mint.wallet, payment_request=pr, max_sat=...)
        ↓ on success: crud.finalize_melt + mark_melt_settled + log_melt
        ↓ on raise: _confirm_payment (retry is_payment_complete via check_payment_status)
             ↓ confirmed not paid → crud.restore
             ↓ confirmed paid     → finalize_melt + mark_melt_settled
             ↓ unconfirmable      → leave pending + log_internal_error
        ↓ finally: _track_melt_end
```
- **Change vs source:** `pay_invoice` is LNbits' (returns `Payment` with `.status`/`.preimage`/`.fee`). `is_payment_complete` → `check_payment_status(payment)` → `funding_source.get_payment_status(checking_id)` returning `PaymentStatus(paid: bool|None, fee_msat, preimage)`. The 3-way distinction (True/False/raise-on-pending) maps to `paid=True`/`paid=False`/`paid=None` — **but** the "failed ⇒ no HTLC outstanding" guarantee is NOT as strong across all LNbits backends (see Hard Problems). The confirm-before-burn discipline is preserved; the in-flight refcount map (`_in_flight_melts`) is preserved verbatim (it's process-local, not DB).

### Rotate (callback → burn → new note)

```
GET /lnurlmint/{mint}/w/cb?k1=<old>&h=<new_hash>
   ↓ resolve old note, refund=(n-1)*base_fee for merge, 0 for rotate
   ↓ crud.swap(burn_ids=[old], mint_note_ids=[h], mint_amounts=[merged])  [atomic tx]
   ← {"status":"OK", "sig": sign_note(h, merged, ...)}
```
- **Change vs source:** `swap` runs inside a single `async with db.connect() as conn:` holding the asyncio.Lock, multiple `conn.execute()`s, one commit — replaces `with self._lock, self.conn:`. `sign_note` is the HARD signing problem (see below). No funding call (rotate/merge move no Lightning funds).

### Verify (GET → live node lookup)

```
GET /lnurlmint/{mint}/verify/{payment_hash}
   ↓ if !mint.verify_enabled → 404
   ↓ crud.mint_pr(hash) → if mint & !comment-protected → 404 (preimage IS the secret)
   ↓ _mint_settled(hash) → check_payment_status / get_payment(hash).success
   ↓ if settled: _mint_preimage → get_payment(hash).preimage (live, never cached)
   ← {"settled": bool, "preimage": hex|None, "pr": ...}
   (or melt direction: crud.melt_pr → _melt_settled/_melt_preimage)
```
- **Change vs source:** `invoice_preimage`/`payment_preimage` → `get_payment(payment_hash).preimage` (LNbits stores preimage on the `Payment` row once settled) or `check_payment_status(...).preimage` (live `PaymentStatus.preimage`). The store-hashes-not-secrets / never-cache-preimage policy is preserved. **Risk:** LNbits backends vary in whether `get_invoice_status` populates `preimage`; verify may need `get_payment` (DB) first, falling back to live status. Must verify per-backend during implementation.

---

## Per-Wallet Multi-Tenancy Design

### Tables (all schema-prefixed `lnurlmint.`)

**`lnurlmint.mints`** — the per-wallet mint config row (one per wallet):

| column | type | notes |
|--------|------|-------|
| `id` | TEXT PK | mint id (e.g. `mint_<hash>`) — appears in LNURL paths |
| `wallet` | TEXT NOT NULL | owning LNbits wallet_id — **the scoping key** |
| `username` | TEXT UNIQUE | LUD-16 username for Lightning Address (scoped to host) |
| `base_fee_msat` | INTEGER | per-mint mint fee flat part |
| `fee_percent_ppm` | INTEGER | per-mint mint fee ppm part |
| `min_sendable_msat` / `max_sendable_msat` | INTEGER | LUD-06 bounds |
| `min_mint_msat` | INTEGER | net-of-fee dust floor |
| `max_k1s` | INTEGER | callback k1 cap |
| `sunset_mint` | BOOL | reject mint+split, allow rotate/merge/melt |
| `verify_enabled` | BOOL | LUD-21 off-switch (endpoint 404s when off) |
| `onion_url` | TEXT NULL | Tor hidden service for base-URL substitution |
| `created_at` / `updated_at` | TIMESTAMP | |

**`lnurlmint.notes`** — outstanding + burned bearer notes:

| column | type | notes |
|--------|------|-------|
| `id` | TEXT PK | sha256(k1) hex — never the secret |
| `mint_id` | TEXT NOT NULL | FK → mints.id (**note→mint→wallet scoping chain**) |
| `amount_msat` | INTEGER | |
| `spent` | INTEGER DEFAULT 0 | burned (kept, not deleted — replay fails as spent) |
| `pending` | INTEGER DEFAULT 0 | reserved by in-flight melt |
| `pending_payment_hash` | TEXT NULL | that melt's invoice hash, for reconcile |

**`lnurlmint.mints_records`** (source `mints` table, renamed to avoid clash with the config row) — pending mint invoices:

| column | type | notes |
|--------|------|-------|
| `payment_hash` | TEXT PK | |
| `mint_id` | TEXT NOT NULL | FK → mints.id |
| `pr` | TEXT | for LUD-21 verify |
| `amount_msat` | INTEGER | net amount |
| `minted` | INTEGER DEFAULT 0 | settled/materialized flag |
| `comment_hash` | TEXT NULL | LUD-25 comment protection |

**`lnurlmint.melts`** — melt records for LUD-25 melt verify:

| column | type | notes |
|--------|------|-------|
| `payment_hash` | TEXT PK | |
| `mint_id` | TEXT NOT NULL | FK → mints.id |
| `pr` | TEXT | |
| `settled` | INTEGER DEFAULT 0 | local confirmed-settled flag |

### Scoping discipline

- **Every note/mint_record/melt row carries `mint_id`**, and `mints.wallet` is the wallet FK. Note lookups join `mints` to enforce `wallet`. The public LNURL endpoints resolve `mint_id` from the path, load the mint row once, and pass `mint_id` (and implicitly `wallet`) into every crud call.
- **Management API** (`views_api.py`) uses `require_admin_key`/`require_invoice_key` → `WalletTypeInfo.wallet.id`; queries are `WHERE wallet = :wallet` (giftcards pattern, `crud.get_cards_by_wallet`).
- **No cross-wallet query exists** — there is no global `note_amount(note_id)`; it's always `note_amount(mint_id, note_id)` scoped through the mint row.

---

## Port Mapping (source → target)

| Source `lnurl-mint` | Target `lnurlmint` | What changes | What's preserved |
|---------------------|--------------------|--------------|------------------|
| `node.py` (lnd/cln REST, httpx) | `services.py` + LNbits `core.services.payments` / `core.crud.payments` / `wallets.base.Wallet` | Per-wallet `create_invoice(wallet_id=...)`/`pay_invoice(wallet_id=...)` replace direct REST. No `LightningBackendConfig`; LNbits picks the funding source. **`sign_message` has no LNbits equivalent** (see Hard Problems). | `create_invoice` returns a preimage (via `Payment.preimage`); `pay_invoice` returns fee+preimage+status; settlement/preimage lookups via `get_payment`/`check_payment_status`. |
| `db.py` `NoteStore` (sqlite + `threading.Lock` + `with conn:`) | `crud.py` over `Database("ext_lnurlmint")` | `threading.Lock` → `Database.lock` (`asyncio.Lock`, held for `async with db.connect()`). `with self._lock, self.conn:` → `async with db.connect() as conn:` + multiple `conn.execute()` + commit. SQLite-only → SQLite **and** Postgres (parameterized `?`/`%s` auto-rewrite, `timestamp_placeholder`). | Store-hashes-not-secrets; pending/finalize/restore SM; all-or-nothing `swap`; burned-rows-kept; `pending_melts()` for reconcile. |
| `server.py` lifespan + `_monitor_funding_source` | `__init__.py` `lnurlmint_start`/`lnurlmint_stop` + `tasks.py` (`create_permanent_unique_task`, `run_interval`) | No FastAPI lifespan; LNbits owns the app. Boot funding-source check → first tick of the task (guarded). Monitor → `run_interval(health_interval, _probe)`. | Reconcile on every healthy tick; health-transition-only logging; reconcile skips in-flight melts. |
| `config.py` `Settings` + `public_base_url` | `models.Mint` (per-wallet DB row) + LNbits extension/global settings | Per-mint fees/limits/sunset/verify/username/onion_url live on the `mints` row, not env. `public_base_url` Tor-awareness → per-mint `onion_url` + LNbits request base_url (see Hard Problems — Tor). | Host-header-spoof-proof derivation (use configured base, not request Host); onion substitution when request Host matches onion host. |
| `router.py` endpoints | `views_lnurl.py` (public) + `views_api.py` (mgmt) | Paths gain `/lnurlmint/{mint_id}/` prefix (per-wallet). `.well-known/lnurlp/{user}` → delegated to `lnurlp` extension (see Hard Problems — Lightning Address). Fee math, in-flight tracking, `_melt_pay`, `reconcile_pending_melts` move to `services.py`. | LUD-06/03/21/25 wire shapes; `HEX32_PATTERN`; comment-protection gating; sunset gating; self-mint/reused-hash rejection. |
| `signing.py` (`mint_pubkey`/`sign_note` via node `sign_message`) | `signing.py` (signing primitive TBD) | **HARD** — LNbits `Wallet` has no `signmessage`. See Hard Problems. | `verify_note` (coincurve recovery, test-only) unchanged; `_message` digest scheme unchanged. |
| `models.py` | `models.py` | + `Mint` config row model; LNURL wire models ported verbatim (incl. `withdrawLink`, `mintPubkey`, `sig`/`sig2`, `disposable: false`). | Wire shapes. |
| `errors.py` / `mint_log.py` | `errors.py` (folded) | Files-next-to-db → LNbits log dir / loguru. `log_internal_error` ref-id pattern preserved. | Never-caller-facing exception text; ref-id link. |
| `error_handler.py` (`LnurlErrorResponseHandler` route_class) | route_class on `views_lnurl` router (or per-handler `JSONResponse`) | Same wrapper, ported. | LUD-01 error shape. |
| `frontend.py` (one-pager) | `static/` SPA + `views.py` `index_public` | HTML templating → SPA bundle. | Mint QR, address, limits, node info. |
| Root `.well-known/lnurlp/{user}` / `lnurlw/{user}` | **Delegated to `lnurlp` extension** | Extension does NOT register `.well-known` routes (would conflict with `lnurlp_redirect_paths`). See Hard Problems. | LUD-16 address resolves to the mint's payRequest. |

---

## The Hard Port Problems

### 1. Lock discipline translation (MEDIUM — solvable)

**Source:** `threading.Lock` + sqlite `with self.conn:` (context-managed transaction). All access serialized in-process; `with self._lock, self.conn:` is one atomic transaction.

**Target:** `Database("ext_lnurlmint")` exposes `async with db.connect() as conn:` which acquires `db.lock` (an `asyncio.Lock`) for its entire duration and yields a SQLAlchemy `Connection`. Multiple `conn.execute()` calls within one `connect()` block form the atomic unit; `conn.update`/`conn.insert` auto-commit, but raw `conn.execute()` for multi-statement atomicity (the `swap`: burn N + mint M in one tx) must be committed explicitly at the end (`await conn.conn.commit()`) and the asyncio.Lock held throughout guarantees no interleaving.

**What's preserved:** The lock is still single-writer-per-Database; the confirm-before-burn state machine is a sequence of these atomic blocks. The `_in_flight_melts` refcount map stays process-local (not DB) — it guards reconcile from racing a live melt, unchanged.

**What's different / risk:** Postgres has real row-level concurrency, but because LNbits' `Database.connect()` holds a single asyncio.Lock for the whole connection, writes are still serialized within one extension's DB. Cross-extension concurrency (lnurlp + lnurlmint) is not lock-protected — but the two don't write the same tables. The `swap`'s "check `mints_records` for collision then insert note" must be inside one `connect()` block to remain race-free (it is). **Action:** implement `swap`/`mark_pending`/`settle_mint` as single-`connect()` multi-`execute()` transactions with a final commit; port the PoC race tests to confirm.

### 2. Signing primitive (HIGH — open design question)

**Source:** `signing.sign_note` calls `node.sign_message` (lnd/cln `signmessage` RPC) → recoverable sig over `LNURLcash:{amount}:{h}` with the funding node's identity key. `mint_pubkey` = node identity pubkey. This is LUD-25 offline verification: a holder verifies a note against the mint's node identity without trusting the mint online.

**Target problem:** LNbits' `Wallet` abstraction (`lnbits/wallets/base.py`) defines `create_invoice`, `pay_invoice`, `get_invoice_status`, `get_payment_status`, `create_hold_invoice`, `settle_hold_invoice`, `cancel_hold_invoice`, `status`, `paid_invoices_stream` — **NO `sign_message`**. Verified: the only `signmessage` references in `~/lnbits` are generated lnd gRPC stubs, not exposed through the abstraction. Different backends (Alby, Blink, Breez, Boltz, clnrest, corelightning, lndrest, FakeWallet…) don't uniformly expose it.

**Options (decision needed):**
1. **Per-mint keypair in DB** — generate a secp256k1 key per mint row, store privkey, sign with it. Deviates from "sign with the funding node's identity" (a holder can't cross-verify against the Lightning node pubkey), but is fully portable across LNbits backends and needs no new dep if `coincurve` is already in LNbits (verify). `mintPubkey` = the mint's own key, not the node's.
2. **Bypass the abstraction for lnd/cln backends only** — reintroduce a thin signmessage client when the active funding source is lndrest/clnrest (they expose it). Rejected-for-v1 per PROJECT.md (idiomatic, no direct node REST), but could be a best-effort fallback.
3. **Drop offline signing for v1** — `sig`/`sig2`/`mintPubkey` omitted (the source already omits them when no funding source / signing fails; wallets still function). Reduces LUD-25 parity.

**Recommendation:** Option 1 (per-mint keypair) for v1 portability, with `mintPubkey` documented as the mint's key (not the node's). Confirm `coincurve` is in LNbits' deps; if not, this becomes a no-new-deps blocker → fall back to Option 3. **Action:** verify `coincurve` availability; decide before signing phase.

### 3. Preimage / settlement lookups (MEDIUM — semantics differ)

**Source:** `is_invoice_settled`, `is_payment_complete` return True/False and **raise** on non-terminal (hodl/pending) — the 3-way distinction is load-bearing for confirm-before-burn. `invoice_preimage`/`payment_preimage` fetch live, never cached.

**Target:** LNbits offers two paths:
- `core.crud.payments.get_payment(payment_hash)` → reads the LNbits `Payment` row (DB bookkeeping): `.status` (`pending`/`success`/`failed`), `.preimage`, `.success`. Fast, local.
- `core.services.payments.check_payment_status(payment)` → `funding_source.get_payment_status(checking_id)` / `get_invoice_status(checking_id)` → `PaymentStatus(paid: bool|None, fee_msat, preimage)`. Live. `paid=None` = pending, `paid=False` = failed, `paid=True` = success.

**Mapping:**
- `is_invoice_settled(hash)` → `get_payment(hash).success` (DB, fast) or `check_payment_status` → `get_invoice_status`.
- `is_payment_complete(hash)` → `check_payment_status` → `get_payment_status` → `PaymentStatus.paid`: `True`→confirmed paid, `False`→confirmed not paid, `None`→pending (raise/leave-pending, matching source's raise).
- `invoice_preimage`/`payment_preimage` → `get_payment(hash).preimage` (DB, populated on settlement) or `check_payment_status(...).preimage` (live).

**Risk:** The source's `PaymentFailed`-is-not-proof-no-HTLC concern (hodl invoice) applies identically — `paid=False` from a backend may still mean an HTLC is live. The confirm-before-burn discipline (confirm via `is_payment_complete` retries before `restore`) is preserved. **But** LNbits backends vary in `PaymentStatus.preimage` population on `get_invoice_status` (some only fill it on `get_payment_status`). **Action:** verify preimage availability per backend during the verify phase; the verify endpoint must tolerate `preimage=None` (source already does).

### 4. Lightning Address integration / `.well-known` (HIGH — integration design)

**Source:** owns `/.well-known/lnurlp/{username}` and `/.well-known/lnurlw/{username}`, returns a LUD-06 `LnurlPayResponse` **extended with `withdrawLink`** (the LUD-25 field that makes paying the invoice mint a bearer note).

**Target problem:** LNbits' `lnurlp` extension already owns `/.well-known/lnurlp/{username}` via `lnurlp_redirect_paths` (`from_path: /.well-known/lnurlp` → `redirect_to_path: /api/v1/well-known`), resolving usernames via `get_address_data(username)` → `lnurlp.pay_links` row → `api_lnurl_response`. PROJECT.md mandates delegation to avoid route conflict. **But** the `lnurl` library's `LnurlPayResponse` (used by `lnurlp`) has **no `withdrawLink` field** — LUD-25's extension is not part of standard LUD-06. So a plain lnurlp PayLink cannot advertise the mint's withdraw side.

**Options (decision needed):**
1. **Mint creates a `lnurlp.pay_links` row + a parallel lnurlmint-owned payRequest.** The lnurlp row handles `.well-known` resolution and returns the standard payRequest; the mint's `/p/cb` callback is the lnurlp callback. `withdrawLink` is injected by… not possible without modifying lnurlp's response model (cross-extension coupling, fragile).
2. **Mint serves its own payRequest at `/lnurlmint/{mint_id}/p` and registers a username→mint mapping that lnurlp's well-known handler consults.** Requires either (a) a hook in lnurlp's `get_address_data` to delegate to lnurlmint, or (b) lnurlmint owning a *separate* well-known path. (a) is cross-extension coupling; (b) conflicts with lnurlp's redirect.
3. **Mint does NOT use Lightning Address for v1; advertises the payRequest LNURL directly (QR).** Drops LUD-16 convenience; no `.well-known` conflict; simplest. The public one-pager shows the raw LNURL/QR, not an `user@host` address.

**Recommendation:** Option 3 for v1 (raw LNURL/QR, no Lightning Address), with Option 1/2 as a later phase that requires an lnurlp-side extension point (a `withdrawLink` field + a delegation hook). This matches PROJECT.md's "delegate to lnurlp" intent without forcing a cross-extension API change in v1. **Action:** confirm with stakeholder; if Lightning Address is a v1 must-have, this becomes the schedule-critical path (needs an lnurlp PR).

### 5. Tor-aware base URL (LOW–MEDIUM)

**Source:** `config.public_base_url(request_base_url)` returns `onion_url` if the request's Host matches the onion hostname, else `base_url`. `base_url` is a required setting, never request-derived (Host-header-spoof-proof).

**Target:** Per-mint `onion_url` lives on the `mints` row. The extension's LNURL handlers receive a `Request`; they must derive the public base URL the same way — compare `request.url.hostname` (or `X-Forwarded-Host`) against the mint's `onion_url` hostname, else use a configured base. **Problem:** LNbits doesn't give the extension a per-mint configured clearnet `base_url` for free; the mint row needs a `base_url` column too (or fall back to LNbits' global `settings.lnbits_base_url` / the request's forwarded host). giftcards' `_public_base_url(request)` reads `X-Forwarded-Host`/`X-Forwarded-Proto` — that's the *request-derived* path the source explicitly rejects as spoofable.

**Resolution:** Store `base_url` (clearnet) and `onion_url` on the mint row; `public_base_url(request)` = onion if request Host matches onion host, else the mint's configured `base_url`, else LNbits global base. Never trust the raw request Host for the clearnet case (preserve the spoof-proof guarantee); `X-Forwarded-Host` is acceptable only behind a trusted proxy (LNbits' convention) — document the trust assumption. **Action:** add `base_url` + `onion_url` to the `mints` row; port `public_base_url`/`public_base_url_and_host` as methods on the `Mint` model.

---

## Scaling Considerations

| Scale | Architecture adjustment |
|-------|------------------------|
| 1 wallet, 10s of notes | Single LNbits instance, SQLite — the default. All patterns as designed. |
| 100s of wallets, 1000s of notes | Postgres backend (LNbits `Database` supports it). Index `notes(mint_id, spent, pending)`, `mints_records(payment_hash)`, `melts(payment_hash)`. Reconcile task interval scales with pending count (already a no-op when nothing pending). |
| 1000s of wallets | The per-`Database` asyncio.Lock serializes all lnurlmint writes — becomes the first bottleneck under heavy concurrent melt load. Mitigation: the lock is only held per-transaction (short); reads (`fetchone`/`fetchall`) each take the lock too but are fast. No sharding needed at LNbits' scale. |

### Scaling Priorities

1. **First bottleneck:** the `Database.lock` (asyncio.Lock) serializing lnurlmint writes under concurrent melts. Fix: keep transactions short (mark_pending is one UPDATE; swap is N+M UPDATE/INSERT in one connection). Postgres row-locks don't help while the asyncio.Lock is held — acceptable for LNbits' typical load.
2. **Second bottleneck:** live settlement lookups (`check_payment_status`) hitting the funding node on every `/verify` and every reconcile tick. Fix: `melt_settled`/`mint_settled` local flags short-circuit (preserved from source); `cached_fetch_node_info` 1h TTL for the one-pager (preserved).

---

## Anti-Patterns

### Anti-Pattern 1: Trusting the request Host for callback URLs

**What people do:** use `request.url_for(...)` / `request.base_url` directly for the LNURL `callback`/`withdrawLink`.
**Why it's wrong:** Host header is attacker-controllable (or cache-poisonable behind a proxy that doesn't vary on Host) → an attacker rewrites the mint's callback URLs to their own host, breaking redemption or phishing preimages.
**Do this instead:** derive base URL from the mint row's configured `base_url`/`onion_url` (source's `public_base_url`), with onion substitution only when the request Host matches the onion host. `X-Forwarded-Host` only behind a trusted proxy.

### Anti-Pattern 2: Burning a note on `pay_invoice` failure without confirming

**What people do:** on `pay_invoice` raising, `restore` the note immediately.
**Why it's wrong:** a dropped connection / timeout after the HTLC went out looks like a failure; restoring lets the holder re-melt → double spend. A `PaymentFailed` (clean failure) doesn't even guarantee no HTLC (hodl invoice).
**Do this instead:** confirm via `is_payment_complete` (`check_payment_status` → `get_payment_status`) with backoff before *either* finalize or restore; leave pending if unconfirmable. Preserve `_confirm_payment` + the in-flight refcount map verbatim.

### Anti-Pattern 3: Persisting or logging the spendable secret (k1/preimage)

**What people do:** store `k1` or the preimage in a column "for debugging".
**Why it's wrong:** a leaked DB/log becomes a theft vector for every outstanding note.
**Do this instead:** store only `sha256(k1)` (the note id / payment hash). Fetch preimages live for verify, never cache. The mint's own `mint_log` logs note *ids* (hashes), never secrets.

### Anti-Pattern 4: Adding a `.well-known/lnurlp` route in the extension

**What people do:** register `/.well-known/lnurlp/{user}` in `lnurlmint_ext`.
**Why it's wrong:** conflicts with LNbits' `lnurlp` extension's `lnurlp_redirect_paths`; route resolution order undefined, breaks Lightning Address for the whole node.
**Do this instead:** delegate to `lnurlp` (Option 3 for v1: raw LNURL/QR, no address; later: integrate via an lnurlp extension point).

### Anti-Pattern 5: Unwallet-scoped note queries

**What people do:** `SELECT * FROM notes WHERE id = ?` (no mint_id/wallet filter).
**Why it's wrong:** cross-wallet leakage — one wallet's note becomes resolvable/inspectable from another wallet's mint context.
**Do this instead:** every note query joins `mints` and filters `wallet = :wallet` (or scopes by `mint_id` from the path, which itself resolves to one wallet).

---

## Integration Points

### External Services

| Service | Integration pattern | Notes |
|---------|---------------------|-------|
| Lightning funding source (lnd/cln/Alby/…) | LNbits `core.services.payments.create_invoice`/`pay_invoice` + `wallets.base.Wallet` via `get_funding_source()` | Per-wallet. **No `signmessage`** in the abstraction (Hard Problem 2). Settlement via `get_payment`/`check_payment_status`. |
| LNbits `lnurlp` extension | Delegation for `.well-known/lnurlp/{user}` (owned by lnurlp via `lnurlp_redirect_paths`) | v1: no Lightning Address (Option 3). Later: needs an lnurlp-side `withdrawLink` field + delegation hook. |
| LNbits `Database` | `Database("ext_lnurlmint")`, schema `lnurlmint`, SQLite + Postgres | `async with db.connect()` holds `asyncio.Lock`; multi-statement atomicity within one connection. |
| LNbits task lifecycle | `create_permanent_unique_task("ext_lnurlmint", ...)` in `lnurlmint_start`; `run_interval` | Reconcile + health monitor. |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| `views_lnurl` ↔ `services` | direct async calls | Public endpoints orchestrate via services; never touch `crud` Lightning calls directly. |
| `services` ↔ `crud` | direct async calls | services owns funding + state-machine orchestration; crud owns SQL + wallet scoping. |
| `services` ↔ LNbits core payments | `create_invoice`/`pay_invoice`/`get_payment`/`check_payment_status` | The `node.py` replacement boundary. |
| `signing` ↔ signing primitive | TBD (per-mint keypair / node bypass / dropped) | Hard Problem 2 — decision gates the signing phase. |
| `lnurlmint` ↔ `lnurlp` | none in v1 (delegation deferred) | Hard Problem 4. |

---

## Suggested Build Order (dependency → phase)

1. **Scaffold + DB schema + models** (no behavior yet): `__init__.py` (router, static, start/stop stubs), `config.json`/`manifest.json`, `migrations.py` (`m001_initial`: `mints` + `notes` + `mints_records` + `melts`, wallet-scoped, indexes), `models.py` (`Mint`, `Note`, `MintRecord`, `MeltRecord` + ported LNURL wire models), `crud.py` skeleton. *Everything else depends on this.*
2. **Per-wallet mint CRUD + management API**: `views_api.py` create/configure/list mints (admin/invoice key decorators), `views.py` generic index. *Needs (1).*
3. **Funding integration (mint path)**: `services.py` `create_invoice`-via-LNbits + `crud.create_mint`/`settle_mint`; `views_lnurl.py` `/p/cb` + lazy `/w` resolution + `/verify` (mint direction). Port `_mint_settled`/`_mint_preimage` over `get_payment`/`check_payment_status`. *Needs (1,2); delivers the core mint flow.*
4. **Melt + state machine + reconcile**: `crud.mark_pending`/`finalize_melt`/`restore`/`swap`/`record_melt`/`pending_melts` as async single-`connect()` transactions; `services._melt_pay` + `_confirm_payment` + `_in_flight_melts`; `views_lnurl.py` `/w/cb` (melt/rotate/split/merge); `tasks.py` reconcile via `create_permanent_unique_task`. Port the confirm-before-burn PoCs. *Needs (3); the funds-loss-critical core.*
5. **Fees + sunset + comment protection**: `_mint_fee_msat`, `_min_sendable_msat` walk, `max_mintable_msat`, `_melt_fee_limit_msat`, sunset gating, LUD-25 comment-hash keying. *Needs (4); mostly ported math.*
6. **Signing / offline verification** (gated by Hard Problem 2 decision): `signing.py` `mint_pubkey`/`sign_note`; wire `sig`/`sig2`/`mintPubkey` into `/w` and `/w/cb`. *Needs (4); blocked on the signing-primitive decision.*
7. **Tor-aware base URL + public one-pager frontend**: `Mint.public_base_url`; `static/` SPA (mgmt + public); node info display (`cached_fetch_node_info` equivalent over LNbits funding source `status`). *Needs (3,5); frontend last.*
8. **Lightning Address integration** (deferred, Hard Problem 4): lnurlp delegation hook. *Post-v1; needs lnurlp-side change.*
9. **Full test-suite port**: security PoCs (double-spend, race, preimage leak, fee conservation, reconcile-inflight) adapted to LNbits fixtures + wallet mocks. *Incremental alongside (3–6); finalized after.*

**Dependency rule:** DB schema + models before endpoints; funding (mint) before melt; core lifecycle (mark_pending/finalize/restore + reconcile) before fees/signing/sunset; backend (all wire endpoints) before frontend; signing is a parallel track blocked only on the primitive decision; Lightning Address is explicitly last/optional.

---

## Sources

- Source app: `~/lnurl-mint/lnurl_mint/{router,db,node,signing,config,server,models,errors,mint_log}.py` (read in full)
- Reference extension: `~/giftcards/{__init__,crud,services,views_api,migrations,tasks,views}.py` + `config.json`/`manifest.json` (read in full)
- LNbits core: `lnbits/core/services/payments.py` (`create_invoice`/`pay_invoice`/`check_payment_status` signatures), `lnbits/core/models/payments.py` (`Payment`, `PaymentState`), `lnbits/core/crud/payments.py` (`get_payment` accepts `checking_id` OR `payment_hash`), `lnbits/db.py` (`Database`/`Connection` — `asyncio.Lock`, `connect()` ctx mgr, `timestamp_placeholder`), `lnbits/wallets/base.py` (`Wallet` ABC — **no `sign_message`**; `PaymentStatus(paid, fee_msat, preimage)`), `lnbits/extensions/lnurlp/` (`lnurlp_redirect_paths` owns `.well-known/lnurlp`; `get_address_data`; `LnurlPayResponse` has no `withdrawLink`)
- LUD-25 draft (`github.com/lnurl/luds/blob/lnurlcash/25.md`) per PROJECT.md context

---
*Architecture research for: LNbits LUD-25 lnurlcash mint extension port*
*Researched: 2026-08-28*
