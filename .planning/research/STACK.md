# Stack Research

**Domain:** LNbits extension implementing LUD-25 lnurlcash (Lightning bearer assets) — port of standalone `lnurl-mint` FastAPI app into the LNbits extension model.
**Researched:** 2026-08-28
**Confidence:** HIGH (all versions verified against actual files in `~/lnbits` and `~/lnurl-mint`; all API signatures read from source)

---

## The No-New-Dependencies Constraint

`docs/devs/extensions.md` (line 43) is unambiguous:

> DO NOT ADD NEW DEPENDENCIES. Try to use the dependencies that are available in `pyproject.toml`. Getting the LNbits project to accept a new dependency is time consuming and uncertain, and may result in your extension NOT being made available to others.

Every dependency `lnurl-mint` uses must therefore be classified as **AVAILABLE** (already in LNbits `pyproject.toml`), **NOT AVAILABLE → replacement**, or **NOT NEEDED** (the LNbits extension model removes the need). This is done per-dependency in the table below and cross-referenced throughout.

LNbits version targeted: **1.5.4** (`pyproject.toml` line 3). Matches `giftcards/config.json` `min_lnbits_version: "1.5.4"`. Python: `>=3.10,<3.13` (line 4).

---

## lnurl-mint Dependency Audit

Each dependency from `~/lnurl-mint/pyproject.toml` (lines 9–23), verified against `~/lnbits/pyproject.toml` (lines 9–54) and the live `.venv`:

| lnurl-mint dep | lnurl-mint version | LNbits pyproject.toml | Status | Verdict |
|---|---|---|---|---|
| `fastapi` | `>=0.115.11,<0.116.0` | `fastapi~=0.116.1` (line 12) | **AVAILABLE** (different minor) | Use LNbits' FastAPI. Extension mounts an `APIRouter`, never its own app. |
| `uvicorn[standard]` | `>=0.34.0` | `uvicorn~=0.40.0` (line 22) | **NOT NEEDED** | LNbits hosts the ASGI app; the extension never runs its own server. Drop entirely. |
| `bolt11` | (unpinned) | `bolt11~=2.1.1` (line 37) | **AVAILABLE** | Same library, same major. `lnurl-mint` uses `bolt11.decode` for preimage verification; LNbits core also imports it (`payments.py` lines 5–7). |
| `httpx` | (unpinned) | `httpx~=0.27.2` (line 14) | **AVAILABLE** | Needed only if the extension reaches the node directly for `signmessage` (see §Signing). Otherwise unused — LNbits core APIs replace `node.py`'s REST calls. |
| `pydantic-settings` | `>=2.0,<3.0` | **NOT PRESENT** | **NOT AVAILABLE → replacement** | LNbits pins **pydantic v1**: `pydantic~=1.10.26` (line 17, verified `pydantic.VERSION == 1.10.26` in venv). `pydantic-settings` is a v2 library and cannot be used. Replace with LNbits' own settings pattern: `from lnbits.settings import settings` (a pydantic-v1 `BaseSettings` already instantiated). Per-mint config becomes DB rows, not env vars. |
| `qrcode` | `>=8.2` | **NOT PRESENT** (`qrcode` lib absent from venv) | **NOT AVAILABLE → replacement** | LNbits ships `pyqrcode~=1.2.1` (line 18, importable in venv). Different API: `pyqrcode.create(data)` → `.code` matrix, not `qrcode.make()`. The `giftcards` extension uses `pyqrcode` exactly this way (`services.py` line 311). Use `pyqrcode`. |
| `bech32` | `>=1.2.0` | `bech32~=1.2.0` (line 10) | **AVAILABLE** | Identical version range. Used by `lnurl-mint` only for zbase32/lnurl encoding edge cases; likely unneeded once LNbits' `lnurl` lib (line 16, `lnurl~=0.10.0`) handles LNURL encoding. |
| `coincurve` | `>=20.0.0` | **NOT in pyproject.toml** | **GRAY AREA — see §Signing** | Importable in the venv (`coincurve` 20.0.0, transitive via `pynostr` per `poetry.lock`), and LNbits' own code imports it directly (`wallets/nwc.py`, `wallets/sparkl2.py`, `utils/nostr.py`). But it is **not declared** in `pyproject.toml`, so relying on it violates the spirit of the no-new-deps rule and is fragile (removing `pynostr` would break us). `embit~=0.8.0` (line 42, declared) lacks pubkey-recovery (`PublicKey.from_signature_and_message`) — embit's `Signature` has no recovery id. **No declared dep can replace coincurve for recoverable-signature verification.** |

### Summary of replacements

| lnurl-mint dep | Replacement in LNbits |
|---|---|
| `uvicorn[standard]` | None — LNbits hosts the app. |
| `pydantic-settings` (v2) | `lnbits.settings.settings` (pydantic v1 `BaseSettings`) + per-mint DB rows. |
| `qrcode` | `pyqrcode` (already in LNbits, used by `giftcards`). |
| `coincurve` | **Unresolved** — see Signing section. Either (a) accept the transitive-dep risk, (b) reach the node for signing and verify via the node too, or (c) drop offline verification for v1. |

---

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|---|---|---|---|
| LNbits extension model | 1.5.4 | Hosts the extension as an `APIRouter` mounted under `/lnurlmint` | This *is* the project. The extension is a Python package discovered by LNbits' extension loader, not a standalone app. |
| FastAPI `APIRouter` | ~=0.116.1 (LNbits) | HTTP endpoints (LNURL pay/withdraw/callback/verify) | LNbits already runs FastAPI; extensions contribute routers via `__init__.py` (`giftcards/__init__.py` lines 11–15). No standalone `FastAPI()` app. |
| pydantic v1 | ~=1.10.26 (LNbits) | Request/response models, DB row models | **Must be v1** — LNbits pins v1 and `pyproject.toml` has `[tool.pydantic-mypy]` v1 config. Use `BaseModel`, `validator` (not `field_validator`), `root_validator`. `lnurl-mint`'s `config.py` uses v2 `field_validator`/`model_validator` — these must be rewritten. |
| LNbits `Database` | (core, `lnbits.db.Database`) | Note store, mint rows, melt records | Replaces `lnurl-mint`'s raw `sqlite3` + `threading.Lock`. Async, SQLite+Postgres dual support, migration runner. See §Database. |
| LNbits core payments API | (core, `lnbits.core.services.payments`) | `create_invoice`, `pay_invoice`, `update_wallet_balance` | Replaces `node.py`'s direct lnd/cln REST. Per-wallet, backend-agnostic. See §Core APIs. |
| LNbits tasks | (core, `lnbits.tasks`) | Background melt reconciliation | `create_permanent_unique_task` replaces `server.py`'s lifespan + monitor. See §Tasks. |
| Vue 3 + Quasar | (LNbits vendor bundles) | Management SPA + public one-pager | The LNbits frontend is Vue 3 + Quasar + Vuex + vue-router, served as precompiled vendor bundles (`lnbits/static/vendor.json`: `vue.global.prod.js`, `quasar.umd.prod.js`, `vuex.global.js`, `vue-router.global.js`). Extensions ship `.vue` SFCs in `static/js/` registered via `static/routes.json`. See §Frontend. |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---|---|---|---|
| `bolt11` | ~=2.1.1 | Decode BOLT-11 invoices (preimage/hash verification) | When verifying a returned preimage matches an invoice's payment hash (`lnurl-mint/node.py` line 759 does this; LNbits `payments.py` lines 5–7 import the same). |
| `pyqrcode` | ~=1.2.1 | QR code generation for the public one-pager | Replaces `qrcode`. `giftcards/services.py` line 311 shows the pattern: `pyqrcode.create(data)` → iterate `.code` matrix. |
| `bech32` | ~=1.2.0 | LNURL bech32 encoding (if needed at all) | Likely unneeded — LNbits' `lnurl~=0.10.0` lib handles `lnurlw://`/`lnurlp://` encoding. Use `lnurl` instead of hand-rolling bech32. |
| `httpx` | ~=0.27.2 | Direct node REST calls **only** for `signmessage` | Only if the extension reaches the funding node directly for LUD-25 offline signing (see §Signing). All other node interaction goes through LNbits core APIs. |
| `lnbits.settings.settings` | (core) | Global LNbits settings (base URL, funding source config) | Replaces `lnurl-mint/config.py`'s `Settings(BaseSettings)`. Per-mint config (fees, limits, sunset, verify toggle) becomes DB rows on the mint table. |
| `loguru` | ~=0.7.3 (LNbits) | Logging | LNbits' logger. Replaces `lnurl-mint`'s stdlib `logging`. `giftcards` and all LNbits code use `from loguru import logger`. |

### Development Tools

| Tool | Purpose | Notes |
|---|---|---|
| `pytest` | ~=9.0.2 (LNbits dev group) | Test suite | LNbits dev deps include `pytest`, `pytest-asyncio`-style fixtures, `pytest-httpserver`, `mock`. Port `lnurl-mint`'s ~25 tests (incl. PoCs) against LNbits test fixtures. |
| `ruff` | ~=0.14.10 (LNbits) | Linting | Match LNbits config: line-length 88, select `F,E,W,I,A,C,N,UP,RUF,B,S`. |
| `black` | ~=26.3.1 (LNbits) | Formatting | Line-length 88 (NOT `lnurl-mint`'s 120). |
| `mypy` | ~=1.17.1 (LNbits) | Type checking | `pydantic.mypy` plugin enabled; `ignore_missing_imports` for `coincurve`/`embit` etc. |

---

## LNbits Extension Structure

Verified from `giftcards/` (the reference extension) and `docs/devs/extensions.md`:

### Required files

| File | Purpose | Reference |
|---|---|---|
| `__init__.py` | Exports `lnurlmint_ext: APIRouter`, `lnurlmint_static_files: list`, `lnurlmint_start()`, `lnurlmint_stop()`, `db`. LNbits' loader imports these by convention. | `giftcards/__init__.py` lines 11–45 |
| `manifest.json` | Repository identity for the extension marketplace: `{"repos": [{"id": "lnurlmint", "organisation": "<org>", "repository": "lnurlmint"}]}` | `giftcards/manifest.json` |
| `config.json` | Display metadata: `name`, `short_description`, `tile` (path to icon under `static/`), `contributors`, `version`, `min_lnbits_version` (set `"1.5.4"`), `license` | `giftcards/config.json` |
| `migrations.py` | `async def m001_initial(db): ...` functions. Auto-discovered by regex `m(\d\d\d)_` and run in version order by `lnbits.core.helpers.run_migration` (line 43). | `giftcards/migrations.py` |
| `crud.py` | DB access. Instantiates `db = Database("ext_lnurlmint")` at module level. Parameterized queries with `:name` placeholders. | `giftcards/crud.py` line 11 |
| `models.py` | pydantic v1 `BaseModel`s for DB rows and API payloads. | `giftcards/models.py` |
| `services.py` | Business logic calling LNbits core payment APIs. | `giftcards/services.py` lines 15–16, 119–122, 192–197 |
| `views.py` | Generic (UI) routes — `index` (auth-gated SPA shell) and `index_public` (public SPA shell) from `lnbits.core.views.generic`. | `giftcards/views.py` |
| `views_api.py` | JSON API routes + LNURL endpoints. Protected by `require_admin_key`/`require_invoice_key` decorators. | `giftcards/views_api.py` |
| `tasks.py` | Background task coroutines, started from `*_start()` via `create_permanent_unique_task`. | `giftcards/tasks.py` |
| `static/` | Frontend assets: `js/*.vue` + compiled `js/*.js`, `routes.json`, images, fonts. | `giftcards/static/` |
| `static/routes.json` | SPA route map: `[{path, name, template, component}]` pointing at `.vue`/`.js` pairs. | `giftcards/static/routes.json` |

### `__init__.py` exports (the contract LNbits' loader expects)

```python
# __init__.py — names are convention; LNbits imports by module + attribute
from fastapi import APIRouter
lnurlmint_ext: APIRouter = APIRouter(prefix="/lnurlmint", tags=["lnurlmint"])
lnurlmint_ext.include_router(lnurlmint_generic_router)   # views.py
lnurlmint_ext.include_router(lnurlmint_api_router)       # views_api.py
lnurlmint_ext.include_router(lnurlmint_lnurl_router)     # public LNURL endpoints

lnurlmint_static_files = [{"path": "/lnurlmint/static", "name": "lnurlmint_static"}]

def lnurlmint_start(): ...   # create_permanent_unique_task("ext_lnurlmint", reconcile_melts)
def lnurlmint_stop(): ...    # cancel scheduled_tasks

__all__ = ["db", "lnurlmint_ext", "lnurlmint_start", "lnurlmint_static_files", "lnurlmint_stop"]
```

`giftcards/__init__.py` is the line-by-line template (lines 11–45).

---

## LNbits Core Payment APIs (replacing `node.py`)

`lnurl-mint/node.py` speaks lnd/cln REST directly. In the extension, all Lightning interaction goes through `lnbits.core.services.payments`. Signatures read from `~/lnbits/lnbits/core/services/payments.py`:

### `create_invoice` (line 247)

```python
async def create_invoice(
    *,
    wallet_id: str,
    amount: float,                       # sats (NOT msat)
    currency: str | None = "sat",
    memo: str,
    description_hash: bytes | None = None,
    unhashed_description: bytes | None = None,
    expiry: int | None = None,
    extra: dict | None = None,
    webhook: str | None = None,
    internal: bool | None = False,       # use FakeWallet (internal transfers)
    payment_hash: str | None = None,     # caller-supplied → hold-invoice path
    labels: list[str] | None = None,
    conn: Connection | None = None,
) -> Payment
```

- **Returns** `Payment` (pydantic model) with `.bolt11`, `.payment_hash`, `.preimage`, `.checking_id`, `.amount_msat`, `.expiry`, `.memo`, `.extra`, `.fee`, `.status`.
- `amount` is in **sats** (converted to msat internally: `amount_msat=amount_sat*1000`, line 338). `lnurl-mint` works in msat — convert.
- `payment_hash` (caller-supplied) triggers the hold-invoice path (`funding_source.create_hold_invoice`, line 302) — **this is how `lnurl-mint`'s caller-supplied-preimage mint maps over**: generate the preimage, derive `payment_hash = sha256(preimage)`, pass `payment_hash` here. Note: hold invoices require `Feature.holdinvoice` on the funding source; not all backends support it.
- Without `payment_hash`, the backend generates its own preimage (returned in `Payment.preimage` if the backend exposes it).
- `internal=True` uses `fake_wallet` — for internal LNbits wallet-to-wallet, no real Lightning. Not suitable for minting (the preimage must come from a real payment).

### `pay_invoice` (line 58)

```python
async def pay_invoice(
    *,
    wallet_id: str,
    payment_request: str,                # BOLT-11 string
    max_sat: int | None = None,          # amount cap (sats)
    extra: dict | None = None,
    description: str = "",
    tag: str = "",
    labels: list[str] | None = None,
    conn: Connection | None = None,
) -> Payment
```

- **Returns** `Payment` with `.status` (`PaymentState.SUCCESS`/`.PENDING`/`.FAILED`), `.preimage`, `.fee`, `.payment_hash`.
- `max_sat` is the **amount cap** (not a fee limit). `lnurl-mint`'s `pay_invoice` takes `fee_limit_msat` — LNbits does not expose a fee-limit parameter here; the funding source's own fee policy applies. This is a **behavioral gap** to note for the melt path.
- Raises `PaymentError` (from `lnbits.exceptions`) on failure. `giftcards/services.py` line 192 shows the call pattern and checks `payment.status != PaymentState.SUCCESS.value`.
- **Async settle semantics**: `lnurl-mint`'s melt responds OK immediately, pays in background, burns on settle. LNbits' `pay_invoice` is **synchronous** (awaits the payment result). The extension must call it from a background task (`create_permanent_unique_task`) to preserve the respond-then-pay discipline. `Payment.pending` covers the in-flight case.

### `update_wallet_balance` (line 454)

```python
async def update_wallet_balance(
    wallet: Wallet,          # the Wallet object (not id)
    amount: int,             # sats; negative = debit, positive = credit
    conn: Connection | None = None,
) -> None
```

- Negative `amount` debits (creates an internal "Admin debit" payment); positive credits (creates an internal invoice marked success).
- Raises `ValueError` if debit would go negative or credit exceeds max balance.
- `giftcards/services.py` lines 119–122 use this to lock sats at gift-card creation; line 219 to reclaim on expiry. **Same pattern** as `lnurl-mint`'s mint-fee withholding and melt funding.

### What `node.py` functions map to

| `node.py` function | LNbits replacement | Notes |
|---|---|---|
| `create_invoice(amount_msat, config, memo)` | `create_invoice(wallet_id=..., amount=msat//1000, memo=..., payment_hash=...)` | Caller-supplied preimage → pass `payment_hash=sha256(preimage).hex()`. Requires hold-invoice support. |
| `pay_invoice(invoice, config, fee_limit_msat)` | `pay_invoice(wallet_id=..., payment_request=invoice, max_sat=...)` | No fee-limit param. Run from a background task for async melt. |
| `is_invoice_settled` / `invoice_preimage` | `get_standalone_payment(payment_hash)` / `get_wallet_payment(checking_id)` from `lnbits.core.crud` + `Payment.status`/`.preimage` | LNbits tracks payment status in its own DB; no need to query the node live. |
| `is_payment_complete` / `payment_preimage` | `get_payment_status(checking_id)` on the funding source, or `Payment.status`/`.preimage` from LNbits DB | For melt reconciliation. |
| `fetch_node_info` / `cached_fetch_node_info` | `Node.get_info()` / `Node.get_public_info()` via `funding_source.__node_cls__` | Only if `has_feature(Feature.nodemanager)`. |
| `sign_message` | **No LNbits equivalent** — see §Signing. | |

---

## Database Abstraction (replacing `lnurl-mint/db.py`)

`lnurl-mint/db.py` uses raw `sqlite3` + a module-level `threading.Lock` (line 41) with a single-process constraint (README lines 256–261). The extension replaces this with `lnbits.db.Database`.

### `Database` API (`lnbits/db.py` line 292)

```python
from lnbits.db import Database
db = Database("ext_lnurlmint")          # giftcards/crud.py line 11
```

- **Name convention**: `"ext_<id>"`. The loader expects `db` at module level (`helpers.py` line 28: `ext_db = importlib.import_module(ext.module_name).db`).
- **SQLite**: file at `<data_folder>/ext_lnurlmint.sqlite3` (line 308). **Postgres**: schema `lnurlmint` (the `ext_` prefix is stripped, line 312–313); tables are schema-prefixed (`lnurlmint.notes`).
- **Async**: all methods are coroutines. Engine is SQLAlchemy `AsyncEngine` (line 317) over `aiosqlite` (SQLite) or `asyncpg` (Postgres).
- **Connection**: `async with db.connect() as conn:` (line 323) — yields a `Connection`. An `asyncio.Lock` serializes connection acquisition (line 320) — this **replaces** `lnurl-mint`'s `threading.Lock`. For multi-statement atomicity, do all statements inside one `db.connect()` block; `conn.execute` commits per call (line 288). For a true multi-statement transaction, use a single `conn` and manage commit explicitly.
- **Placeholders**: write `?`/`:name` in queries; `Connection.rewrite_query` (line 141) auto-converts `?`→`%s` for Postgres. **Always use `:name` named placeholders** (the `giftcards` pattern) — safest cross-DB.
- **Methods**: `fetchall(query, values, model)`, `fetchone(query, values, model)`, `execute(query, values)`, `insert(table, model)`, `update(table, model, where)`, `fetch_page(...)`. `model` is a pydantic class; rows are auto-mapped (`dict_to_model`).
- **Cross-DB compat**: `db.timestamp_now` (property → `now()`/`strftime`), `db.timestamp_placeholder(key)` (line 130), `db.references_schema` (line 110 — `""` on SQLite, `lnurlmint.` on Postgres), `db.serial_primary_key`, `db.blob` (`BLOB`/`BYTEA`). **Use these for any DDL or timestamp logic** — `giftcards/migrations.py` lines 15–16, 25, 81, 124 show the pattern.

### Migrations (`lnbits/core/helpers.py` line 37)

```python
# migrations.py
async def m001_initial(db):
    await db.execute(f"""
        CREATE TABLE IF NOT EXISTS lnurlmint.notes (
            id TEXT PRIMARY KEY,
            amount_msat INTEGER NOT NULL,
            spent INTEGER NOT NULL DEFAULT 0,
            pending INTEGER NOT NULL DEFAULT 0,
            pending_payment_hash TEXT
        );
    """)
    # cross-DB index: use db.references_schema prefix
    table = f"{db.references_schema}notes"
    await db.execute(f"CREATE INDEX IF NOT EXISTS idx_lnurlmint_notes_pending ON {table}(pending);")
```

- Discovered by regex `m(\d\d\d)_` on module attributes (`helpers.py` line 43). Run in ascending version order, skipping already-applied versions (tracked in core `migrations` table).
- `db` passed to each migration is a `Connection` (line 33–34).
- **Schema prefix**: tables created as `lnurlmint.<table>` (the extension's schema). Reference tables in queries as `lnurlmint.notes` (works on both SQLite via `ATTACH` and Postgres via schema) — `giftcards/crud.py` line 21 does `SELECT * FROM giftcards.cards`.
- `lnurl-mint`'s `_add_column_if_missing` ad-hoc migrations (db.py lines 110–118) become proper numbered `m002_*`, `m003_*` functions.

### Mapping `NoteStore` → `Database`

| `NoteStore` (lnurl-mint) | Extension pattern |
|---|---|
| `threading.Lock` + `with self.conn:` | `async with db.connect() as conn:` (asyncio.Lock inside) |
| `CREATE TABLE IF NOT EXISTS` in `__init__` | `m001_initial(db)` migration |
| `_add_column_if_missing` | numbered `m002_*` migrations |
| `sqlite3` only | SQLite + Postgres via `Database` |
| single-process constraint | removed — LNbits serializes via `asyncio.Lock` per `Database` instance |
| `swap()` atomic burn+mint (line 243) | all statements inside one `db.connect()` block; the confirm-before-burn discipline becomes a DB transaction |

---

## Tasks (replacing `server.py` lifespan + monitor)

`lnurl-mint/server.py` runs a lifespan that starts a background health monitor and melt reconciler. The extension uses LNbits' task system (`lnbits/tasks.py`):

| Primitive | Signature | Use |
|---|---|---|
| `create_permanent_unique_task(name, coro_factory)` | line 39 — wraps `coro_factory` in `catch_everything_and_restart` (restarts after 5s on any exception, line 70) | Melt reconciliation loop. Started from `lnurlmint_start()`. |
| `create_permanent_task(coro_factory)` | line 35 | Non-unique long-running tasks. |
| `run_interval(seconds, func)` | line 152 — returns a wrapper that calls `func()` every `seconds` while `settings.lnbits_running` | The reconciler: `await run_interval(60, reconcile_pending_melts)()`. `giftcards/tasks.py` line 38 uses exactly this. |
| `register_invoice_listener(queue, name)` + `wait_for_paid_invoices(name, func)` | lines 79, 137 | **Settle mints reactively**: instead of polling, register a listener that fires `settle_mint(payment)` when a mint invoice is paid. This replaces `lnurl-mint`'s settle-on-verify-poll. |
| `cancel_all_tasks()` / `*_stop()` | line 45 | `lnurlmint_stop()` cancels the task list. |

`giftcards/__init__.py` lines 36–43 show the start/stop pattern; `giftcards/tasks.py` shows the `run_interval` wrapper.

---

## Signing — LUD-25 Offline Verification (the hard problem)

LUD-25 offline verification needs two things:
1. **`mintPubkey`** — the mint's signing identity (the funding node's identity pubkey).
2. **`sign_note`** — a recoverable ECDSA signature over `LNURLcash:<amount>:<note_id_hex>` using the "Lightning Signed Message" double-sha256 convention, via the node's `signmessage` RPC.
3. **`verify_note`** — pubkey recovery from the signature+message (done by the *wallet*, not the mint, but the mint's test suite checks it).

### What LNbits exposes

- **`mintPubkey` — AVAILABLE (conditionally).** `lnbits.wallets.base.Wallet.__node_cls__` (line 110) points to a `Node` subclass for backends that support node management (`Feature.nodemanager`). `Node.get_id()` (`nodes/base.py` line 168) returns the identity pubkey (lnd: `identity_pubkey`, `nodes/lndrest.py` line 83; cln: `id`, `nodes/cln.py` line 197). Access:
  ```python
  from lnbits.wallets import get_funding_source
  fs = get_funding_source()
  if fs.has_feature(Feature.nodemanager) and fs.__node_cls__:
      node = fs.__node_cls__(fs)
      mint_pubkey = await node.get_id()
  ```
  Only `LndRestWallet` and `CLNRestWallet` set `__node_cls__`/`Feature.nodemanager`. `FakeWallet`, `VoidWallet`, and some others do not — offline verification is then unavailable, same as `lnurl-mint` with no funding source.

- **`signmessage` — NOT EXPOSED.** Confirmed by grep: no `signmessage`/`sign_message`/`SignMessage` anywhere in `lnbits/nodes/` or `lnbits/wallets/*.py` (only in generated `lnd_grpc_files/` protobuf stubs, which are not wired into the `Wallet`/`Node` ABC). The `Wallet` ABC (`base.py` lines 108–191) has `create_invoice`, `pay_invoice`, `status`, `get_invoice_status`, `get_payment_status`, `paid_invoices_stream`, hold-invoice methods — **no signing**. The `Node` ABC (`nodes/base.py`) has `get_id`, peers, channels, info, payments, invoices — **no signing**.

- **`verify_note` (pubkey recovery) — needs `coincurve`.** `lnurl-mint/signing.py` line 92 uses `PublicKey.from_signature_and_message(signature, digest, hasher=None)`. Verified: `coincurve.PublicKey.from_signature_and_message` exists in the venv. **No declared LNbits dep can do this** — `embit`'s `ec.Signature` has no recovery id and `PublicKey` has no `from_signature_and_message`.

### Options (confidence: MEDIUM — this is the one open architectural decision)

| Option | mintPubkey | sign_note | verify_note | Dep risk | Spec fidelity |
|---|---|---|---|---|---|
| **A. Reach node directly for `signmessage`** via `httpx` using `settings.lnd_rest_endpoint`/`settings.lnd_rest_macaroon` or `settings.clnrest_*` runes (all present in `lnbits/settings.py` lines 548–551 and clnrest settings). Re-implement `node.py`'s `_sign_message_lnd`/`_sign_message_cln` (lines 328, 464) — they're small. Use `coincurve` for `verify_note` in tests. | `Node.get_id()` | direct REST | `coincurve` (test-only) | `coincurve` transitive; `httpx` declared | **Highest** — matches `lnurl-mint` exactly. |
| **B. Extension-owned keypair** for signing (generate a secp256k1 key, store privkey in DB, advertise its pubkey as `mintPubkey`). Sign with `embit`/`coincurve` locally. | derived locally | local `coincurve`/`embit` | `coincurve` | `coincurve` transitive | **Deviates from LUD-25** — spec recommends the node identity key so notes verify against the same key that signs BOLT-11s. |
| **C. Drop offline verification for v1** — omit `mintPubkey`/`sig`/`sig2`. | — | — | — | none | Spec-incomplete (offline verification is optional in LUD-25, so this is conformant but feature-reduced). |

**Recommendation: Option A.** It preserves full parity with `lnurl-mint`, uses only `httpx` (declared) for the signing RPC, and the `coincurve` use is confined to the test suite's `verify_note` (production signing is done by the node, verification by the holder's wallet). The `coincurve` transitive-dep risk is real but bounded: (1) LNbits' own `nwc.py`/`nostr.py` already hard-depend on it, so it won't be removed lightly; (2) if it is, only `verify_note` (test-only) breaks, not production. Flag this as a Key Decision for the parent agent.

If the no-new-deps rule is interpreted strictly (transitive deps forbidden too), Option C is the safe fallback for v1 and Option B the upgrade path.

---

## Frontend Framework

**Vue 3 + Quasar + Vuex + vue-router**, served as precompiled vendor bundles by LNbits core. This is NOT a choice — it is the LNbits extension frontend contract.

### Evidence

- `lnbits/static/vendor.json` loads: `vue.global.prod.js`, `quasar.umd.prod.js`, `vuex.global.js`, `vue-router.global.js`, `vue-qrcode-reader.umd.js`, `qrcode.vue.browser.js`, `vue-i18n.global.prod.js`, plus `axios`, `moment`, `underscore`, `chart.js`, `showdown`.
- `giftcards/static/js/index.vue` (line 1+) uses Quasar components (`q-card`, `q-btn`, `q-select`, `q-col-gutter`) and Vue SFC syntax (`<template id="page-giftcards">`, `v-model`).
- `giftcards/static/routes.json` maps SPA routes to `.vue`/`.js` pairs.
- `giftcards/views.py` registers `index` (auth-gated) and `index_public` (public) from `lnbits.core.views.generic` — these render `base.html`, which loads the Vue/Quasar runtime and boots the SPA from `routes.json`.

### Build tooling

- **No per-extension build step is required for the SPA shell.** LNbits core ships the Vue/Quasar runtime precompiled. Extensions ship `.vue` SFCs and a compiled `.js` companion (the `.vue` is the template, the `.js` is the component logic) — see `giftcards/static/js/index.vue` + `index.js`.
- The `.js` files are compiled (50KB–73KB in giftcards) — there is a build step, but it produces plain JS consumed by LNbits' runtime Vue compiler. The `lnbits-extensions` repo (`~/lnbits-extensions`) uses **SolidJS + Vite** (`vite-plugin-solid`, `package.json`), but that is the **extensions marketplace/gallery**, NOT the framework individual extension frontends use. Do not confuse the two.
- **For `lnurlmint`**: ship `static/js/index.vue` + `index.js` (management SPA) and `static/js/public.vue` + `public.js` (public one-pager), registered in `static/routes.json`. The public one-pager (mint QR, lightning address, limits, node info) replaces `lnurl-mint/frontend.py`'s server-rendered HTML.

### What NOT to use

| Avoid | Why | Use Instead |
|---|---|---|
| SolidJS / React | LNbits' runtime only loads Vue/Quasar vendor bundles. A Solid/React SPA won't mount. | Vue 3 SFCs + Quasar components. |
| Server-side Jinja templates for the SPA | `index`/`index_public` render `base.html` (core-owned); extension UI lives in the Vue SPA. | `.vue` SFCs + `routes.json`. |
| `lnbits-extensions` (SolidJS) as a template | It's the marketplace gallery, not the extension frontend framework. | `giftcards/static/` is the reference. |

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|---|---|---|
| LNbits `create_invoice`/`pay_invoice` | Direct lnd/cln REST (keep `node.py`) | Never for v1 — PROJECT.md explicitly rejects standalone lnd/cln mode. Only if a backend feature LNbits doesn't expose (e.g. fee_limit_msat) becomes a funds-loss risk. |
| `Database("ext_lnurlmint")` + migrations | Raw sqlite (keep `NoteStore`) | Never — loses Postgres support and the async/lock model LNbits requires. |
| `pyqrcode` | `qrcode` | Never — `qrcode` is not in LNbits and cannot be added. |
| `lnbits.settings.settings` + per-mint DB rows | `pydantic-settings` v2 `BaseSettings` | Never — pydantic v1 is pinned; v2 is incompatible. |
| Option A (node-direct `signmessage`) | Option B (extension keypair) | If the node backend doesn't expose `signmessage` over REST, or if the operator uses a funding source with no node identity (FakeWallet). |
| Vue 3 + Quasar | SolidJS | Never for the extension SPA — LNbits runtime is Vue-only. (SolidJS is only for the separate `lnurl-wallet` holder app, which is out of scope for v1.) |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|---|---|---|
| `uvicorn` / standalone `FastAPI()` app | LNbits hosts the ASGI app; extensions are routers. | `APIRouter(prefix="/lnurlmint")` in `__init__.py`. |
| `pydantic-settings` / pydantic v2 syntax (`field_validator`, `model_validator`) | LNbits pins pydantic 1.10.26; v2 is absent. | pydantic v1 `validator`/`root_validator`; `lnbits.settings.settings`. |
| `qrcode` library | Not installed (venv check: `ModuleNotFoundError: qrcode`). | `pyqrcode` (declared, used by `giftcards`). |
| `threading.Lock` + `sqlite3` | Single-process only; LNbits is async and may use Postgres. | `Database` + `asyncio.Lock` (built into `Database.connect`). |
| `logging` (stdlib) | LNbits standardizes on `loguru`. | `from loguru import logger`. |
| `.well-known/lnurlp/{user}` routes from the extension | Conflicts with LNbits' built-in `lnurlp` extension (PROJECT.md Out of Scope). | Delegate Lightning Address to LNbits `lnurlp`. |
| Adding any dep not in `pyproject.toml` | Forbidden by `docs/devs/extensions.md` line 43. | Use only declared deps; `coincurve` is the one gray-area exception (transitive, see §Signing). |

---

## Stack Patterns by Variant

**If the funding source is `LndRestWallet` or `CLNRestWallet` (has `Feature.nodemanager`):**
- `mintPubkey` available via `Node.get_id()`.
- `signmessage` reachable via direct REST using `settings.lnd_rest_*` / `settings.clnrest_*` (Option A).
- Node info (alias, color, capacity) available via `Node.get_public_info()`.

**If the funding source is `FakeWallet` / `VoidWallet` / other (no `nodemanager`):**
- Offline verification unavailable — omit `mintPubkey`/`sig`/`sig2` (same as `lnurl-mint` with no funding source).
- Mint/melt still work via `create_invoice`/`pay_invoice` (FakeWallet handles internal).
- Node info one-pager shows "no funding source configured".

**If Postgres is the configured backend (`LNBITS_DATABASE_URL=postgres://...`):**
- All queries auto-rewritten (`?`→`%s`, schema-prefixed tables).
- Use `db.timestamp_placeholder`, `db.references_schema`, `db.blob` in DDL — never raw SQLite-isms.

---

## Version Compatibility

| Package A (LNbits) | Compatible With | Notes |
|---|---|---|
| `pydantic~=1.10.26` | pydantic v1 models only | `lnurl-mint`'s v2 `config.py` must be rewritten to v1. `field_validator`→`validator`, `model_validator`→`root_validator`, `SettingsConfigDict`→`class Config`. |
| `fastapi~=0.116.1` | `APIRouter`, `Depends`, `Security` | `lnurl-mint` pins `<0.116.0`; LNbits is `0.116.1`. API is compatible at the router level. |
| `bolt11~=2.1.1` | `bolt11.decode`/`encode` | Same major as `lnurl-mint`'s unpinned `bolt11`. |
| `sqlalchemy~=1.4.54` | `Database` internals (async engine) | Extensions don't use SQLAlchemy directly — they use `Database`'s `fetchall`/`execute`. The `~=1.4` pin means no SQLAlchemy 2.0 API. |
| `bech32~=1.2.0` | `lnurl-mint`'s `bech32>=1.2.0` | Identical. |
| `coincurve` 20.0.0 (transitive) | `lnurl-mint`'s `coincurve>=20.0.0` | Version matches exactly. Risk is the transitive (undeclared) status, not the version. |

---

## Sources

- `~/lnbits/pyproject.toml` — all LNbits dependency versions verified (lines 9–54).
- `~/lnbits/.venv` — runtime verification: `pydantic.VERSION == 1.10.26`, `coincurve` importable + `PublicKey.from_signature_and_message` present, `qrcode` absent, `pyqrcode` present, `embit.ec` lacks pubkey recovery.
- `~/lnbits/poetry.lock` — `coincurve` 20.0.0 pulled transitively by `pynostr` (line 1026+).
- `~/lnbits/lnbits/db.py` — `Database`/`Connection` API (lines 134–409), `Compat` cross-DB helpers (lines 58–131).
- `~/lnbits/lnbits/core/helpers.py` — migration runner `run_migration` (lines 37–58), `migrate_extension_database` (lines 22–34).
- `~/lnbits/lnbits/core/services/payments.py` — `create_invoice` (247), `pay_invoice` (58), `update_wallet_balance` (454).
- `~/lnbits/lnbits/tasks.py` — `create_permanent_unique_task` (39), `run_interval` (152), `register_invoice_listener` (79), `wait_for_paid_invoices` (137).
- `~/lnbits/lnbits/decorators.py` — `require_admin_key` (180), `require_invoice_key` (204), `check_user_exists` (315), `WalletTypeInfo`.
- `~/lnbits/lnbits/wallets/base.py` — `Wallet` ABC (108), `Feature` enum (18), no `signmessage`.
- `~/lnbits/lnbits/nodes/base.py` — `Node` ABC (153), `get_id` (168), no `signmessage`.
- `~/lnbits/lnbits/nodes/lndrest.py` — `_get_id` returns `identity_pubkey` (83).
- `~/lnbits/lnbits/nodes/cln.py` — `_get_id` (197).
- `~/lnbits/lnbits/core/views/generic.py` — `index` (198), `index_public` (213).
- `~/lnbits/lnbits/static/vendor.json` — Vue/Quasar/Vuex/vue-router vendor bundles.
- `~/lnbits/docs/devs/extensions.md` — no-new-deps rule (line 43), structure (lines 33–39).
- `~/giftcards/` — reference extension: `__init__.py`, `crud.py`, `services.py`, `migrations.py`, `views.py`, `tasks.py`, `config.json`, `manifest.json`, `static/routes.json`, `static/js/*.vue`.
- `~/lnurl-mint/pyproject.toml` — dependency list (lines 9–23).
- `~/lnurl-mint/lnurl_mint/node.py` — lnd/cln REST client incl. `sign_message` (175), `_sign_message_lnd` (328), `_sign_message_cln` (464).
- `~/lnurl-mint/lnurl_mint/signing.py` — `mint_pubkey` (30), `sign_note` (51), `verify_note` (80, uses `coincurve.PublicKey.from_signature_and_message`).
- `~/lnurl-mint/lnurl_mint/db.py` — `NoteStore` with `threading.Lock` (41), `_add_column_if_missing` (110).
- `~/lnurl-mint/lnurl_mint/config.py` — pydantic v2 `Settings` (uses `field_validator`/`model_validator`/`SettingsConfigDict`).
- `~/lnbits-extensions/package.json` + `vite.config.js` — confirmed SolidJS is the *marketplace* framework, not extension frontends.

---
*Stack research for: LNbits extension implementing LUD-25 lnurlcash (lnurlmint)*
*Researched: 2026-08-28*
