# Phase 1 Research: Extension Scaffold + Data Model + Per-Wallet Mint CRUD

**Researched:** 2026-08-28
**Confidence:** HIGH — all patterns verified against actual source in `~/lnbits` and `~/lnurl-mint`; coincurve importability confirmed live in the venv.

---

## RQ1: LNbits Extension Loader Contract

### manifest.json + config.json

**`manifest.json`** (`giftcards/manifest.json` — exact format):
```json
{
  "repos": [
    {
      "id": "lnurlmint",
      "organisation": "<org>",
      "repository": "lnurlmint"
    }
  ]
}
```

**`config.json`** (`giftcards/config.json` — exact format):
```json
{
  "name": "lnurlmint",
  "short_description": "Mint Lightning-funded bearer notes (LUD-25 lnurlcash)",
  "tile": "/lnurlmint/static/image/lnurlmint.png",
  "contributors": [{"name": "...", "uri": "...", "role": "Lead dev"}],
  "version": "0.1.0",
  "min_lnbits_version": "1.5.4",
  "license": "MIT"
}
```
- `min_lnbits_version` MUST be `"1.5.4"` (matches `giftcards`, the LNbits version in `~/lnbits`).
- `tile` path points at an icon under `static/image/`.

### `__init__.py` exports (the loader contract)

Verified from `giftcards/__init__.py` (lines 1–45). The loader (`helpers.py` line 28) does `importlib.import_module(ext.module_name).db` — so `db` MUST be importable at module level. The full contract:

```python
import asyncio
from fastapi import APIRouter
from loguru import logger
from .crud import db
from .views import lnurlmint_generic_router
from .views_api import lnurlmint_api_router

lnurlmint_ext: APIRouter = APIRouter(prefix="/lnurlmint", tags=["lnurlmint"])
lnurlmint_ext.include_router(lnurlmint_generic_router)
lnurlmint_ext.include_router(lnurlmint_api_router)

lnurlmint_static_files = [
    {"path": "/lnurlmint/static", "name": "lnurlmint_static"}
]

scheduled_tasks: list[asyncio.Task] = []

def lnurlmint_stop():
    for task in scheduled_tasks:
        try:
            task.cancel()
        except Exception as ex:
            logger.warning(ex)

def lnurlmint_start():
    # Phase 2 will add: create_permanent_unique_task("ext_lnurlmint", wait_for_reconcile)
    pass  # stub for Phase 1

__all__ = ["db", "lnurlmint_ext", "lnurlmint_start", "lnurlmint_static_files", "lnurlmint_stop"]
```

**Key observations:**
- `lnurlmint_start`/`lnurlmint_stop` are plain functions (not async). `giftcards_start` imports `create_permanent_unique_task` lazily inside the function body (line 38).
- `scheduled_tasks` is a module-level `list[asyncio.Task]` — `start` appends, `stop` cancels.
- For Phase 1, `lnurlmint_start` can be a no-op stub (background tasks are Phase 2 / EXT-03). But the function MUST exist and be exported.
- `db` is imported from `.crud` (which does `db = Database("ext_lnurlmint")` at module level).

### Static files registration

`lnurlmint_static_files` is a list of dicts with `path` and `name` keys. The path `/lnurlmint/static` is served by LNbits core. The `static/` directory must contain:
- `static/routes.json` — SPA route map (array of `{path, name, template, component}`)
- `static/js/*.vue` + `static/js/*.js` — Vue SFCs + compiled JS
- `static/image/` — icons (referenced by `config.json` `tile`)

**`routes.json` format** (from `giftcards/static/routes.json`):
```json
[
  {
    "path": "/lnurlmint/",
    "name": "PageLnurlmint",
    "template": "/lnurlmint/static/js/index.vue",
    "component": "/lnurlmint/static/js/index.js"
  }
]
```

### Generic views (`views.py`)

From `giftcards/views.py` (lines 1–35): the `generic_router` registers `index` (auth-gated) and `index_public` (public) from `lnbits.core.views.generic`:

```python
from fastapi import APIRouter, Depends
from lnbits.core.views.generic import index, index_public
from lnbits.decorators import check_user_exists

lnurlmint_generic_router = APIRouter()

lnurlmint_generic_router.add_api_route(
    "/", methods=["GET"], endpoint=index,
    dependencies=[Depends(check_user_exists)],
)
```

- `index` renders `base.html` with `{"user": user.json()}` — the Vue SPA boots from `routes.json`.
- `index_public` renders `base.html` with `{"public": True}` — no auth.
- For Phase 1's placeholder, a single `index` route at `/` is sufficient (the public one-pager is Phase 6).

---

## RQ2: Database Migration Format

### Migration function signature and discovery

From `lnbits/core/helpers.py` (lines 22–58):

```python
async def migrate_extension_database(ext, current_version=None):
    ext_migrations = importlib.import_module(f"{ext.module_name}.migrations")
    ext_db = importlib.import_module(ext.module_name).db
    async with ext_db.connect() as ext_conn:
        await run_migration(ext_conn, ext_migrations, ext.id, current_version)

async def run_migration(db, migrations_module, db_name, current_version=None):
    matcher = re.compile(r"^m(\d\d\d)_")
    for key, migrate in list(migrations_module.__dict__.items()):
        match = matcher.match(key)
        if match:
            version = int(match.group(1))
            if not current_version or version > current_version.version:
                await migrate(db)
                # ... update migration version tracking
```

**Key facts:**
- Migrations are `async def mNNN_name(db)` functions in `migrations.py`.
- Discovered by regex `^m(\d\d\d)_` on module attributes — run in `__dict__` order (which is definition order in Python 3.7+).
- `db` passed to each migration is a `Connection` (not `Database`) — so use `await db.execute(...)` directly (the Connection's execute, which commits per call).
- Already-applied versions are skipped (tracked in core `migrations` table for Postgres, or in the extension's own DB for SQLite).

### Migration DDL pattern (from `giftcards/migrations.py`)

```python
async def m001_initial(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS giftcards.cards (
            id            TEXT PRIMARY KEY,
            wallet        TEXT NOT NULL,
            amount        INTEGER NOT NULL,
            token_hash    TEXT NOT NULL UNIQUE,
            status        TEXT NOT NULL DEFAULT 'active',
            created_at    TIMESTAMP NOT NULL DEFAULT """
        + db.timestamp_now
        + """,
            expires_at    TIMESTAMP
        );
        """
    )
    # Indexes: use db.references_schema prefix ("" on SQLite, "lnurlmint." on Postgres)
    table = f"{db.references_schema}cards"
    await db.execute(
        f"CREATE INDEX IF NOT EXISTS idx_giftcards_cards_wallet ON {table}(wallet);"
    )
```

**Cross-DB DDL rules (verified from `lnbits/db.py` Compat class):**
- **Table names**: always schema-prefixed — `lnurlmint.mints`, `lnurlmint.notes`, etc. (works on both SQLite via `ATTACH` and Postgres via schema).
- **`db.timestamp_now`**: property → `"(strftime('%s', 'now'))"` on SQLite, `"now()"` on Postgres. Use string concatenation to embed in DDL.
- **`db.references_schema`**: `""` on SQLite, `"lnurlmint."` on Postgres. Use for index DDL where you can't use the schema-prefixed table name directly (SQLite can't create indexes with a schema prefix on an attached DB).
- **`db.timestamp_placeholder(key)`**: `":key"` on SQLite, `"to_timestamp(:key)"` on Postgres. Use for INSERT/UPDATE timestamp values.
- **`db.blob`**: `"BLOB"` on SQLite, `"BYTEA"` on Postgres.
- **`db.serial_primary_key`**: `"INTEGER PRIMARY KEY AUTOINCREMENT"` on SQLite, `"SERIAL PRIMARY KEY"` on Postgres.
- **Booleans**: stored as `INTEGER` (0/1) — LNbits convention (giftcards uses `INTEGER` for status flags, lnurl-mint uses `INTEGER` for `spent`/`pending`/`minted`/`settled`).
- **No `BOOL` type** — use `INTEGER NOT NULL DEFAULT 0`.

### SQLite vs Postgres DDL differences for our tables

All four tables use `TEXT PRIMARY KEY` (UUIDs/hashes) — no auto-increment needed. The only cross-DB concerns are:
1. `TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}` for `created_at` columns.
2. Index DDL uses `f"{db.references_schema}tablename"` prefix.
3. No `BOOLEAN` type — use `INTEGER` with 0/1 defaults.

---

## RQ3: `async with db.connect() as conn:` Transaction Pattern

### The critical atomicity rule

From `lnbits/db.py` (lines 292–390):

**`Database.execute`** (line 388):
```python
async def execute(self, query, values=None):
    async with self.connect() as conn:      # acquires lock, opens connection
        return await conn.execute(query, values)  # commits per call
```

**`Database.connect`** (line 323):
```python
@asynccontextmanager
async def connect(self):
    await self.lock.acquire()               # asyncio.Lock — serializes ALL access
    try:
        async with self.engine.connect() as conn:
            wconn = Connection(conn, self.type, self.name, self.schema)
            # ... schema setup (CREATE SCHEMA / ATTACH)
            yield wconn
    finally:
        self.lock.release()
```

**The problem**: `db.execute()`, `db.fetchone()`, `db.fetchall()`, `db.insert()`, `db.update()` each call `async with self.connect()` — **each call is its own lock acquisition + connection + transaction**. Three sequential `db.execute()` calls are three separate transactions with the lock released between them.

**The solution**: For multi-statement atomicity, use one `async with db.connect() as conn:` block:
```python
async with db.connect() as conn:
    # All statements here share one connection + one lock
    result = await conn.execute("UPDATE ... WHERE minted=0", {...})
    if result.rowcount == 1:
        await conn.execute("INSERT INTO ...", {...})
    # conn.execute commits per call (line 288: await self.conn.commit())
```

**`conn.execute`** (line 285):
```python
async def execute(self, query, values=None):
    params = self.rewrite_values(values) if values else {}
    result = await self.conn.execute(text(self.rewrite_query(query)), params)
    await self.conn.commit()                # commits after each execute
    return result
```

**Compare-and-set pattern** (from `giftcards/crud.py` line 107–121):
```python
async def mark_redeeming(token_hash: str) -> Optional[GiftCard]:
    result = await db.execute(
        "UPDATE giftcards.cards SET status = 'redeeming' WHERE token_hash = :hash AND status = 'active'",
        {"hash": token_hash},
    )
    if result.rowcount == 0:
        return None
    return await get_card_by_token_hash(token_hash)
```

For Phase 1, the mint CRUD operations are single-statement (create, get, update, delete) — `db.insert`, `db.fetchone`, `db.execute` are fine. The `async with db.connect() as conn:` discipline is needed for:
- **Delete with outstanding notes check**: query notes count + delete mint in one transaction (or query first, then delete — but the atomic version is safer).
- **CRUD stubs that Phase 2 will extend**: establish the pattern now.

### When to use `db.connect()` vs `db.execute`

| Operation | Use | Why |
|-----------|-----|-----|
| Single INSERT/SELECT/UPDATE/DELETE | `db.insert`/`db.fetchone`/`db.fetchall`/`db.execute` | One statement = one transaction, lock auto-managed |
| Multi-statement atomic (swap, settle_mint, mark_pending) | `async with db.connect() as conn:` + multiple `conn.execute()` | Lock held throughout, all statements in one connection |
| Compare-and-set (UPDATE ... WHERE condition + check rowcount + INSERT) | `async with db.connect() as conn:` | The UPDATE and INSERT must be atomic |

### Named placeholders

Always use `:name` named placeholders (never `?` positional). `Connection.rewrite_query` (line 141) auto-converts `?`→`%s` for Postgres, but `:name` works on both without conversion. This is the `giftcards` pattern throughout.

---

## RQ4: Pydantic v1 Model Patterns

### LNbits pins pydantic 1.10.26

From `giftcards/models.py` — the exact import and patterns:

```python
from pydantic import BaseModel, Field, validator, root_validator
```

**`BaseModel`** — all models extend this.
**`Field(..., gt=0)`** — for validation constraints (e.g., `amount: int = Field(..., gt=0)`).
**`validator("field_name")`** — field-level validation (NOT `field_validator`).
**`root_validator`** — model-level validation (NOT `model_validator`); used for cross-field validation.
**`class Config`** — model configuration (not shown in giftcards but standard v1).

### `no_database=True` field extra

From `lnbits/db.py` `model_to_dict` (line 673):
```python
if model.__fields__[key].field_info.extra.get("no_database", False):
    continue
```
Fields with `no_database=True` in their `Field(...)` extra are **excluded from DB insert/update** — useful for computed/API-only fields that don't map to columns. Example: `Field(..., no_database=True)`.

### Model patterns for our models

**DB row model** (maps to a table — all fields must match columns):
```python
class Mint(BaseModel):
    id: str
    wallet: str
    username: str
    base_fee_msat: int = 0
    fee_percent_ppm: int = 0
    min_sendable_msat: int = 1000
    max_sendable_msat: int = 1_000_000_000
    min_mint_msat: int = 10_000
    verify_enabled: bool = True
    sunset_mint: bool = False
    mint_privkey: str  # secp256k1 hex (64 chars)
    base_url: str = ""
    onion_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime
```

**API create model** (request body — no `id`/`wallet`/timestamps, those are server-generated):
```python
class CreateMint(BaseModel):
    username: str
    base_fee_msat: int = Field(0, ge=0)
    fee_percent_ppm: int = Field(0, ge=0, le=100_000)
    min_sendable_msat: int = Field(1000, ge=1)
    max_sendable_msat: int = Field(1_000_000_000, ge=1)
    min_mint_msat: int = Field(10_000, ge=0)
    verify_enabled: bool = True
    sunset_mint: bool = False
    base_url: str = ""
    onion_url: Optional[str] = None

    @root_validator
    def _sendable_bounds_ordered(cls, values):
        if values.get("min_sendable_msat", 0) > values.get("max_sendable_msat", 0):
            raise ValueError("min_sendable_msat must be <= max_sendable_msat")
        return values
```

**`dict_to_model`** (line 708): auto-maps DB rows to pydantic models — extra columns from JOINs are skipped (line 718: `if key not in model.__fields__: continue`). Nested `BaseModel`/`dict`/`list` fields are JSON-decoded automatically.

**`model_to_dict`** (line 663): converts pydantic model to dict for INSERT/UPDATE — `datetime` fields are converted to timestamps (SQLite: `int(ts)`, Postgres: `value.replace(tzinfo=None)`); nested models/dicts/lists are JSON-encoded; `no_database=True` fields are skipped.

---

## RQ5: Auth Decorator Patterns

### `require_admin_key` / `require_invoice_key`

From `lnbits/decorators.py` (lines 180–213):

```python
async def require_admin_key(
    request: Request,
    api_key_header: str = Security(api_key_header),
    api_key_query: str = Security(api_key_query),
) -> WalletTypeInfo:
    check = KeyChecker(api_key=api_key_header or api_key_query, expected_key_type=KeyType.admin)
    return await check(request)

async def require_invoice_key(
    request: Request,
    api_key_header: str = Security(api_key_header),
    api_key_query: str = Security(api_key_query),
) -> WalletTypeInfo:
    check = KeyChecker(api_key=api_key_header or api_key_query, expected_key_type=KeyType.invoice)
    return await check(request)
```

**Usage in route handlers** (from `giftcards/views_api.py` lines 155–160):
```python
@giftcards_api_router.post("")
async def api_create_card(
    data: CreateGiftCard,
    request: Request,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> dict:
    wallet_id = wallet.wallet.id    # the authenticated wallet's id
    user_id = wallet.wallet.user    # the owning user's id
```

**`WalletTypeInfo`** (from `lnbits/core/models/wallets.py` line 230):
```python
@dataclass
class WalletTypeInfo:
    key_type: KeyType    # admin=0, invoice=1, invalid=2
    wallet: Wallet       # the full Wallet object
```

- `wallet.wallet.id` — the wallet ID (use for `WHERE wallet = :wallet` scoping).
- `wallet.wallet.user` — the user ID.
- `require_admin_key` → 403 if the key is an invoice key or invalid.
- `require_invoice_key` → 403 if the key is invalid (allows both admin and invoice keys).

### `check_user_exists` (for generic views)

From `giftcards/views.py` line 12: `dependencies=[Depends(check_user_exists)]` — gates the `index` route to authenticated users (session-based, not API key).

### API router structure

From `giftcards/views_api.py` lines 75–77:
```python
giftcards_api_router = APIRouter(prefix="/api/v1/cards")
giftcards_lnurl_router = APIRouter(prefix="/api/v1/lnurl")
giftcards_claim_router = APIRouter(prefix="/api/v1/claim")
```

For lnurlmint Phase 1, the management API router:
```python
lnurlmint_api_router = APIRouter(prefix="/api/v1/mints")
```

This mounts under `lnurlmint_ext` (prefix `/lnurlmint`), so full path is `/lnurlmint/api/v1/mints`.

**Route ordering**: static routes (`/`, `/bulk`) MUST be declared before `/{id}` routes, or FastAPI matches the path param. Giftcards does this (line 319: `/validate-csv` before `/{card_id}`).

---

## RQ6: giftcards Reference Implementation Summary

### `__init__.py` (45 lines)
- Imports `db` from `.crud`, routers from `.views`/`.views_api`/`.tasks`.
- `giftcards_ext = APIRouter(prefix="/giftcards", tags=["GiftCards"])`.
- Includes 4 sub-routers: generic, api, lnurl, claim.
- `giftcards_static_files = [{"path": "/giftcards/static", "name": "giftcards_static"}]`.
- `scheduled_tasks: list[asyncio.Task] = []`.
- `giftcards_start()`: lazily imports `create_permanent_unique_task`, creates task, appends to `scheduled_tasks`.
- `giftcards_stop()`: cancels all `scheduled_tasks`.
- `__all__ = ["db", "giftcards_ext", "giftcards_start", "giftcards_static_files", "giftcards_stop"]`.

### `crud.py` (382 lines)
- `db = Database("ext_giftcards")` at module level (line 11).
- `create_card(card)`: `await db.insert("giftcards.cards", card)` — uses pydantic model directly.
- `get_card(card_id)`: `await db.fetchone("SELECT * FROM giftcards.cards WHERE id = :id", {"id": card_id}, GiftCard)`.
- `get_cards_by_wallet(wallet_id)`: `WHERE wallet = :wallet` — the wallet-scoping pattern.
- `mark_redeeming(token_hash)`: compare-and-set `UPDATE ... WHERE ... AND status = 'active'` + `rowcount == 0` check.
- `mark_card_expired(card_id)`: compare-and-set + returns `bool` (`rowcount == 1`).
- `delete_card(card_id)`: `await db.execute("DELETE FROM giftcards.cards WHERE id = :id", {"id": card_id})`.
- `update_card_fields(card_id, fields)`: dynamic SET clause from allowed field names.
- Uses `db.timestamp_placeholder('now')` for timestamp values in UPDATE.
- **No `async with db.connect()` usage** — giftcards doesn't need multi-statement atomicity (single-statement operations only). Our extension WILL need it for Phase 2.

### `migrations.py` (127 lines)
- 5 migrations: `m001_initial` through `m005_template_images`.
- `m001_initial`: creates `giftcards.cards` table + 2 indexes.
- `m003_branded_delivery`: idempotent `ALTER TABLE ADD COLUMN` wrapped in try/except (for re-runnability).
- All DDL uses `db.timestamp_now` for defaults, `db.references_schema` for index prefixes.
- `BLOB NOT NULL` for binary data (line 124).

### `views_api.py` (984 lines)
- 3 routers: `giftcards_api_router` (`/api/v1/cards`), `giftcards_lnurl_router` (`/api/v1/lnurl`), `giftcards_claim_router` (`/api/v1/claim`).
- POST `""` (create): `require_admin_key`, body is `CreateGiftCard` model, `wallet.wallet.id` for scoping.
- GET `""` (list): `require_invoice_key`, returns `list[GiftCardSummary]`.
- GET `/public/{token_hash}`: no auth (public redemption page).
- LNURL callback: returns `JSONResponse` with `LnurlSuccessResponse().dict()` or `LnurlErrorResponse(reason=...).dict()`.
- Uses `from lnurl import LnurlErrorResponse, LnurlSuccessResponse, LnurlWithdrawResponse, encode as lnurl_encode`.

### `models.py` (489 lines)
- All pydantic v1: `BaseModel`, `Field`, `validator`, `root_validator`.
- `GiftCard` (DB row model): all fields match columns, `Optional` for nullable.
- `GiftCardSummary` (projection): subset of fields for list view.
- `CreateGiftCard` (API request): no `id`/`wallet`/`created_at` (server-generated), `Field(..., gt=0)` for validation.
- `BulkCreateRequest`: `root_validator` for cross-field validation (either count+amount OR rows).
- `@validator("expires_at", pre=True)` for parsing date-only strings.

### `views.py` (35 lines)
- `giftcards_generic_router = APIRouter()`.
- `add_api_route("/", methods=["GET"], endpoint=index, dependencies=[Depends(check_user_exists)])`.
- `add_api_route("/redeem/{raw_token}", methods=["GET"], endpoint=index_public)`.

### `tasks.py` (24 lines)
- `wait_for_expiry()`: `await run_interval(60, _expire_gift_cards)()`.
- `_expire_gift_cards()`: queries expired cards, marks them, reclaims sats.
- Imported lazily inside `_expire_gift_cards` to avoid circular imports.

---

## RQ7: lnurl-mint's NoteStore — State Machine Reference

From `lnurl-mint/lnurl_mint/db.py` (395 lines):

### Source tables (sqlite, single-process)

**`notes`**:
- `id TEXT PRIMARY KEY` — sha256(k1), never the secret
- `amount_msat INTEGER NOT NULL`
- `spent INTEGER NOT NULL DEFAULT 0` — burned (kept, not deleted)
- `pending INTEGER NOT NULL DEFAULT 0` — reserved by in-flight melt
- `pending_payment_hash TEXT` — that melt's invoice hash, for reconcile

**`mints`** (pending mint invoices — NOT the config row):
- `payment_hash TEXT PRIMARY KEY`
- `pr TEXT NOT NULL` — for LUD-21 verify
- `amount_msat INTEGER NOT NULL`
- `minted INTEGER NOT NULL DEFAULT 0` — settled/materialized flag
- `comment_hash TEXT` — LUD-25 comment protection

**`melts`**:
- `payment_hash TEXT PRIMARY KEY`
- `pr TEXT NOT NULL`
- `settled INTEGER NOT NULL DEFAULT 0`

### CRUD operations (the state machine)

| Method | What it does | Atomicity |
|--------|-------------|-----------|
| `create_mint(payment_hash, pr, amount_msat, comment_hash)` | INSERT into mints; collision-check comment_hash against notes+mints | `with self._lock, self.conn:` |
| `pending_mint(payment_hash)` | SELECT amount_msat WHERE minted=0 | read-only |
| `settle_mint(payment_hash)` | UPDATE mints SET minted=1 WHERE minted=0 (compare-and-set) + INSERT note | `with self._lock, self.conn:` (atomic) |
| `note_amount(note_id)` | SELECT amount_msat WHERE spent=0 | read-only |
| `note_spent(note_id)` | SELECT spent | read-only |
| `note_pending(note_id)` | SELECT pending WHERE spent=0 | read-only |
| `swap(burn_ids, mint_note_ids, mint_amounts)` | Check all burn_ids (not spent, not pending) → burn all → mint new (collision-check against mints) | `with self._lock:` + `with self.conn:` (atomic) |
| `record_melt(payment_hash, pr)` | INSERT OR IGNORE into melts | `with self._lock, self.conn:` |
| `mark_melt_settled(payment_hash)` | UPDATE melts SET settled=1 | `with self._lock, self.conn:` |
| `mark_pending(note_ids, payment_hash)` | Check all (not spent, not pending) → UPDATE all SET pending=1, pending_payment_hash | `with self._lock, self.conn:` (atomic, all-or-nothing) |
| `finalize_melt(note_ids)` | UPDATE all SET spent=1, pending=0, pending_payment_hash=NULL | `with self._lock, self.conn:` |
| `restore(note_ids)` | UPDATE all SET pending=0, pending_payment_hash=NULL | `with self._lock, self.conn:` |
| `pending_melts()` | SELECT id, pending_payment_hash WHERE pending=1 AND spent=0 | read-only |

**Phase 1 scope**: Only mint config row CRUD (create/get/update/delete mint). The note/mint_record/melt CRUD operations are Phase 2. But the TABLES must exist from Phase 1 (migrations create all four).

---

## RQ8: coincurve Importability

**VERIFIED** — coincurve is importable in the LNbits venv:

```
from coincurve import PrivateKey, PublicKey
pk = PrivateKey()
pk.secret.hex()                          # → 64-char hex private key
pk.public_key.format(compressed=True).hex()  # → 66-char hex compressed public key
PublicKey.from_signature_and_message     # → exists (for verify_note, test-only)
```

- Version: 20.0.0 (via `importlib.metadata.version('coincurve')`)
- Location: `/home/exedev/lnbits/.venv/lib/python3.12/site-packages/coincurve/`
- Transitive dep (via `pynostr`), NOT declared in `pyproject.toml` — gray area per no-new-deps rule.
- LNbits' own code imports it: `wallets/nwc.py`, `wallets/sparkl2.py`, `utils/nostr.py`.

**For Phase 1**: `coincurve.PrivateKey()` is used to generate the mint's secp256k1 keypair at mint creation. The private key (64-char hex) is stored in `mints.mint_privkey`; the public key is derived from it (not stored — derived on demand in Phase 5). This is safe: `coincurve` is already used by LNbits itself, and keypair generation is a one-time operation at mint creation.

```python
from coincurve import PrivateKey
def _generate_mint_keypair() -> str:
    """Generate a secp256k1 private key, return as hex (64 chars)."""
    return PrivateKey().secret.hex()
```

---

## RQ9: Exact Table Schema

From `ARCHITECTURE.md` (lines 278–324) + `REQUIREMENTS.md` (DATA-01 through DATA-03) + `lnurl-mint/db.py` source:

### `lnurlmint.mints` — per-wallet mint config row (DATA-01)

| column | type | notes |
|--------|------|-------|
| `id` | TEXT PRIMARY KEY | mint id (UUID) — appears in LNURL paths |
| `wallet` | TEXT NOT NULL | owning LNbits wallet_id — **the scoping key** |
| `username` | TEXT NOT NULL | LUD-16 username (default "mint") |
| `base_url` | TEXT NOT NULL DEFAULT '' | public base URL |
| `onion_url` | TEXT | nullable — Tor hidden service |
| `base_fee_msat` | INTEGER NOT NULL DEFAULT 0 | flat fee part |
| `fee_percent_ppm` | INTEGER NOT NULL DEFAULT 0 | ppm fee part |
| `min_sendable_msat` | INTEGER NOT NULL DEFAULT 1000 | LUD-06 lower bound |
| `max_sendable_msat` | INTEGER NOT NULL DEFAULT 1000000000 | LUD-06 upper bound |
| `min_mint_msat` | INTEGER NOT NULL DEFAULT 10000 | net-of-fee dust floor |
| `verify_enabled` | INTEGER NOT NULL DEFAULT 1 | LUD-21 off-switch (0/1) |
| `sunset_mint` | INTEGER NOT NULL DEFAULT 0 | sunset mode (0/1) |
| `mint_privkey` | TEXT NOT NULL | secp256k1 hex (64 chars) — generated at creation |
| `created_at` | TIMESTAMP NOT NULL DEFAULT {db.timestamp_now} | |
| `updated_at` | TIMESTAMP NOT NULL DEFAULT {db.timestamp_now} | |

**Indexes**: `idx_lnurlmint_mints_wallet ON (wallet)` — for wallet-scoped management queries.

### `lnurlmint.notes` — bearer notes (DATA-02)

| column | type | notes |
|--------|------|-------|
| `id` | TEXT PRIMARY KEY | sha256(k1) hex — never the secret |
| `mint_id` | TEXT NOT NULL | FK → mints.id (note→mint→wallet scoping chain) |
| `amount_msat` | INTEGER NOT NULL | |
| `state` | TEXT NOT NULL DEFAULT 'outstanding' | outstanding/pending/spent (replaces spent+pending ints) |
| `minted` | INTEGER NOT NULL DEFAULT 0 | (from source mints table — but this is on notes? No — see below) |
| `comment_hash` | TEXT | nullable — LUD-25 comment protection |
| `created_at` | TIMESTAMP NOT NULL DEFAULT {db.timestamp_now} | |

**Wait — state vs spent/pending**: The REQUIREMENTS.md DATA-02 says `state (outstanding/pending/spent)` and `minted flag`. The source uses `spent` + `pending` as separate INTEGER flags. The requirements specify a `state` column. **Decision needed at plan time**: follow the requirement's `state` TEXT column, or use the source's `spent`+`pending` INTEGER columns. The `state` approach is cleaner but requires translating the source's `WHERE spent=0 AND pending=0` to `WHERE state='outstanding'`. The `spent`+`pending` approach preserves the source's exact query patterns. **Recommendation**: use `spent` + `pending` INTEGER columns (matching source exactly) to preserve the compare-and-set patterns (`UPDATE ... WHERE spent=0 AND pending=0`); the `state` column from the requirement can be a computed property on the pydantic model. BUT the requirement explicitly says `state` column — **this is at Claude's discretion per the CONTEXT.md decisions** ("exact SQL DDL syntax... are at Claude's discretion").

**Indexes**: `idx_lnurlmint_notes_mint_id ON (mint_id)`, `idx_lnurlmint_notes_pending ON (pending)` (for reconcile query).

### `lnurlmint.mints_records` — pending mint invoices (DATA-03)

| column | type | notes |
|--------|------|-------|
| `payment_hash` | TEXT PRIMARY KEY | |
| `mint_id` | TEXT NOT NULL | FK → mints.id |
| `pr` | TEXT NOT NULL | for LUD-21 verify |
| `amount_msat` | INTEGER NOT NULL | net amount |
| `comment_hash` | TEXT | nullable — LUD-25 comment protection |
| `created_at` | TIMESTAMP NOT NULL DEFAULT {db.timestamp_now} | |

**Note**: Source's `mints` table is renamed to `mints_records` to avoid clash with the config row (`mints`). The `minted` flag from source is NOT on this table — in the extension, settlement materializes a note in `notes` (existence = minted). Alternatively, keep `minted` for the compare-and-set pattern. **Recommendation**: keep `minted INTEGER NOT NULL DEFAULT 0` to preserve the compare-and-set `UPDATE mints_records SET minted=1 WHERE minted=0` pattern from source.

### `lnurlmint.melts` — melt records (DATA-03)

| column | type | notes |
|--------|------|-------|
| `payment_hash` | TEXT PRIMARY KEY | |
| `mint_id` | TEXT NOT NULL | FK → mints.id |
| `note_ids` | TEXT | JSON array of burned note ids (for reference) |
| `amount_msat` | INTEGER NOT NULL | |
| `pr` | TEXT NOT NULL | the melted-into invoice |
| `settled` | INTEGER NOT NULL DEFAULT 0 | local confirmed-settled flag |
| `created_at` | TIMESTAMP NOT NULL DEFAULT {db.timestamp_now} | |

### FKs and cross-DB notes

- SQLite doesn't enforce FKs by default (and `ATTACH`'d schema FKs are tricky). LNbits/giftcards doesn't use explicit `FOREIGN KEY` constraints — the `mint_id`/`wallet` columns are just TEXT with indexes. **Follow this convention**: no explicit FK constraints, just indexed columns + application-level JOIN scoping.
- Postgres: could use FKs but LNbits convention is not to. Keep it consistent.

---

## Phase 1 Implementation Notes

### Files to create

| File | Purpose | Lines (est.) |
|------|---------|-------------|
| `lnurlmint/__init__.py` | Extension loader contract | ~40 |
| `lnurlmint/manifest.json` | Marketplace metadata | ~9 |
| `lnurlmint/config.json` | Display metadata | ~15 |
| `lnurlmint/migrations.py` | `m001_initial` (4 tables + indexes) | ~80 |
| `lnurlmint/models.py` | pydantic v1 models (Mint, CreateMint, Note, MintRecord, MeltRecord) | ~120 |
| `lnurlmint/crud.py` | `Database("ext_lnurlmint")` + mint CRUD (wallet-scoped) | ~120 |
| `lnurlmint/views.py` | Generic router (index placeholder) | ~15 |
| `lnurlmint/views_api.py` | Management API router (CRUD endpoints) | ~120 |
| `lnurlmint/static/routes.json` | SPA route map | ~7 |
| `lnurlmint/static/js/index.vue` | Placeholder Vue SFC | ~20 |
| `lnurlmint/static/js/index.js` | Placeholder JS companion | ~10 |
| `lnurlmint/static/image/lnurlmint.png` | Extension icon | binary |

### Management API endpoints (Phase 1)

| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| POST | `/lnurlmint/api/v1/mints` | `require_admin_key` | `CreateMint` | `Mint` |
| GET | `/lnurlmint/api/v1/mints` | `require_invoice_key` | — | `list[Mint]` |
| GET | `/lnurlmint/api/v1/mints/{mint_id}` | `require_invoice_key` | — | `Mint` |
| PUT | `/lnurlmint/api/v1/mints/{mint_id}` | `require_admin_key` | `CreateMint` (partial) | `Mint` |
| DELETE | `/lnurlmint/api/v1/mints/{mint_id}` | `require_admin_key` | — | `{"success": true}` |

### Delete with outstanding notes (409 Conflict)

Per CONTEXT.md decision: reject delete with 409 if the mint has outstanding notes. This requires querying `notes` table (which exists from Phase 1 migration) — a simple `SELECT COUNT(*) FROM lnurlmint.notes WHERE mint_id = :mid AND spent = 0` check before delete.

### Mint ID generation

Per CONTEXT.md: auto-generated UUID (user never controls the id). Use `uuid4().hex` or `f"mint_{uuid4().hex[:16]}"` — giftcards uses `uuid4().hex` for card_id (but giftcards' id is set in `services.py`, not in the API handler). For lnurlmint, generate in `crud.create_mint` or `views_api.py`.

### Keypair generation at mint creation

```python
from coincurve import PrivateKey

def _generate_mint_privkey() -> str:
    return PrivateKey().secret.hex()  # 64-char hex
```
Called in `crud.create_mint` (or the API handler) before inserting the mint row.

### `updated_at` handling

The `mints` table has `updated_at` — set to `db.timestamp_now` on creation, and updated on PUT. Use `db.timestamp_placeholder('now')` in UPDATE queries: `updated_at = {db.timestamp_placeholder('now')}` with `{"now": time.time()}`.

### EXT-04: No new dependencies

All imports must be from LNbits' `pyproject.toml` or transitive deps already in the venv:
- `fastapi`, `pydantic` (v1), `loguru` — core.
- `coincurve` — transitive (verified importable).
- `lnbits.db.Database`, `lnbits.decorators.*`, `lnbits.core.views.generic.*` — LNbits core.

No `pydantic-settings`, no `qrcode`, no `uvicorn`, no `httpx` (not needed for Phase 1).

---

## Open Questions for Planning

1. **`notes.state` TEXT vs `spent`+`pending` INTEGER columns**: REQUIREMENTS.md DATA-02 specifies `state (outstanding/pending/spent)` + `minted flag`. Source uses `spent`+`pending` INTEGER. At Claude's discretion per CONTEXT.md. **Recommendation**: use `spent`+`pending` INTEGER (preserves source's compare-and-set query patterns exactly); expose `state` as a computed property on the pydantic `Note` model. The `minted` flag goes on `mints_records` (not `notes`).

2. **`mints_records.minted` flag**: Source has `minted INTEGER DEFAULT 0` on its `mints` table (our `mints_records`). Keep it to preserve the compare-and-set `UPDATE ... WHERE minted=0` pattern. The requirement DATA-03 doesn't explicitly mention `minted` but it's implied by the settle_mint state machine.

3. **`melts.note_ids` column**: REQUIREMENTS.md DATA-03 says `note_ids` on melts. Source doesn't have this (it tracks pending notes via `notes.pending_payment_hash`). Include it as TEXT (JSON array) for audit/reference, or omit and rely on the `notes.pending_payment_hash` → `melts.payment_hash` join. **Recommendation**: include it per the requirement.

4. **`mints.max_k1s` column**: ARCHITECTURE.md schema includes `max_k1s` but REQUIREMENTS.md DATA-01 doesn't list it. **Recommendation**: omit from Phase 1 (can be added in a later migration if needed); the source has it as a global setting, not per-mint.

5. **Frontend placeholder complexity**: CONTEXT.md says "minimal Vue placeholder page." The `index.js` companion to `index.vue` — giftcards' compiled JS files are 50-73KB. For a true placeholder, a minimal hand-written JS file (not compiled from a build step) should suffice. **Recommendation**: write a minimal `index.js` that defines a Vue component object matching `index.vue`'s template, without a build step.

---

## Summary

All 9 research questions are answered with HIGH confidence from actual source code. The key findings:

1. **Loader contract**: `__init__.py` exports `lnurlmint_ext`, `lnurlmint_static_files`, `lnurlmint_start`/`lnurlmint_stop`, `db`. `manifest.json` + `config.json` with `min_lnbits_version: "1.5.4"`. Static files at `/lnurlmint/static` with `routes.json`.

2. **Migrations**: `async def m001_initial(db)` with `db.execute(CREATE TABLE...)`. `db.timestamp_now` for defaults, `db.references_schema` for index prefixes. Auto-discovered by regex `m(\d\d\d)_`.

3. **Transaction atomicity**: `db.execute`/`db.fetchone` each open a separate transaction. Multi-statement ops MUST use `async with db.connect() as conn:`. Compare-and-set via `result.rowcount == 1`.

4. **Pydantic v1**: `BaseModel`, `validator`, `root_validator`, `Field(..., gt=0)`. `no_database=True` to exclude fields from DB ops. `dict_to_model`/`model_to_dict` auto-convert.

5. **Auth**: `require_admin_key`/`require_invoice_key` → `WalletTypeInfo` with `.wallet.id` for scoping. `Depends()` in route handler signature.

6. **giftcards reference**: Complete file-by-file breakdown above. Key pattern: `db = Database("ext_giftcards")` at module level, `WHERE wallet = :wallet` scoping, `db.insert("giftcards.cards", model)` for inserts.

7. **NoteStore state machine**: 14 methods, all atomic under `with self._lock, self.conn:`. Phase 1 only needs mint config CRUD; note/mint_record/melt CRUD is Phase 2. Tables must exist from Phase 1.

8. **coincurve**: VERIFIED importable (v20.0.0, transitive via pynostr). `PrivateKey().secret.hex()` for keypair generation. `PublicKey.from_signature_and_message` exists for test-only verify.

9. **Table schema**: 4 tables (`mints`, `notes`, `mints_records`, `melts`), all schema-prefixed `lnurlmint.`, wallet-scoped via `mints.wallet` FK chain. No explicit FK constraints (LNbits convention). INTEGER for booleans (0/1).
