# Phase 1: Extension Scaffold + Data Model + Per-Wallet Mint CRUD - Context

**Gathered:** 2026-08-28
**Status:** Ready for planning

<domain>
## Phase Boundary

The lnurlmint extension loads in LNbits, database migrations run creating all four tables (mints, notes, mints_records, melts) on both SQLite and Postgres, and a wallet owner can create and configure a per-wallet mint via the management API (`/lnurlmint/api/v1/mints`). No LNURL endpoints yet — this phase delivers the scaffold + data model + management CRUD vertical slice. The DB transaction atomicity discipline (`async with db.connect() as conn:` for multi-statement ops) is established here in the CRUD layer, before any burn/mint code in Phase 2.

</domain>

<decisions>
## Implementation Decisions

### Mint Config Defaults
- Default `base_fee_msat` = 0 (fee-free by default, operator opts into fees)
- Default `fee_percent_ppm` = 0 (fee-free by default)
- Default `min_sendable_msat` = 1000 (1 sat), `max_sendable_msat` = 1_000_000_000 (1M sats) — matches lnurl-mint's `.env.example`
- Default `min_mint_msat` = 10_000 (10 sats) — matches lnurl-mint default

### Management API Shape
- Endpoint prefix: `/lnurlmint/api/v1/mints` (follows giftcards' `/giftcards/api/v1` pattern)
- Auth: `require_admin_key` for create/update/delete, `require_invoice_key` for read (matches giftcards)
- Mint `id`: auto-generated UUID (user never controls the id, like giftcards' card_id)
- Delete mint with outstanding notes: reject with 409 Conflict (preserves outstanding notes)

### Keypair Generation Timing
- Generate mint's secp256k1 keypair at mint creation (Phase 1) — `mint_privkey` column exists in `mints` table from the start (DATA-01), generating the key now avoids a migration later; Phase 5 just adds signing logic
- Store private key as hex (32 bytes, 64 chars) — simplest for secp256k1, matches `coincurve` API

### Frontend Placeholder
- Ship a minimal Vue placeholder page in Phase 1 — a single Vue SFC that renders "lnurlmint" + mint list (reads from the management API), so the extension has a visible page in LNbits' UI from day one
- Static files structure follows giftcards' `static/` layout: `static/js/` for SFCs, `static/routes.json` for route registration, `static/image/` for icons

### Claude's Discretion
All other implementation choices (exact SQL DDL syntax, pydantic model field names, internal helper function names, migration numbering) are at Claude's discretion — follow lnurl-mint's source structure and giftcards' LNbits conventions as documented in the research.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- **giftcards extension** (`~/giftcards/`): `__init__.py` (extension lifecycle pattern), `crud.py` (`Database("ext_giftcards")` pattern, wallet-scoped queries), `migrations.py` (migration format), `views_api.py` (auth decorators, API router structure), `services.py` (`pay_invoice`/`update_wallet_balance` usage), `config.json` + `manifest.json` (metadata format)
- **lnurl-mint source** (`~/lnurl-mint/lnurl_mint/`): `db.py` (NoteStore state machine — the reference for what the CRUD layer must support), `models.py` (LNURL wire response shapes), `config.py` (settings structure → becomes per-mint DB row)
- **LNbits core** (`~/lnbits/lnbits/`): `db.py` (`Database` abstraction, `async with db.connect() as conn:` for atomicity), `decorators.py` (`require_admin_key`, `require_invoice_key`, `WalletTypeInfo`), `core/views/generic.py` (`index`, `index_public`), `tasks.py` (`create_permanent_unique_task`)

### Established Patterns
- Extension lifecycle: `__init__.py` exports `lnurlmint_ext` (APIRouter), `lnurlmint_start`/`lnurlmint_stop`, `db`, `lnurlmint_static_files`
- DB access: `Database("ext_lnurlmint")`, async queries with named placeholders, migrations auto-discovered by regex `m(\d\d\d)_`
- Wallet scoping: every query JOINs on `mints.wallet` — no cross-wallet access (giftcards pattern)
- Transaction atomicity: `async with db.connect() as conn:` for multi-statement ops (critical — `db.execute`/`db.fetchone` each open a separate transaction)
- pydantic v1: `validator`/`root_validator`/`class Config` (NOT v2 syntax — LNbits pins pydantic 1.10.26)

### Integration Points
- LNbits loader discovers the extension via `manifest.json` + `config.json`
- Management API mounts under `lnurlmint_ext` APIRouter (prefix `/lnurlmint`)
- Static files served at `/lnurlmint/static` via `lnurlmint_static_files`
- Frontend route registered via `static/routes.json` + `index`/`index_public` generic views

</code_context>

<specifics>
## Specific Ideas

- The `mints` table should include `mint_privkey` (secp256k1 hex) from the start — Phase 5 adds signing logic but the column and key generation happen here
- The four tables (`mints`, `notes`, `mints_records`, `melts`) and their relationships are documented in detail in `.planning/research/ARCHITECTURE.md` — follow that schema
- `coincurve` is used for keypair generation (transitive dep, already imported by LNbits' nostr/nwc code) — verify it's importable before relying on it

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>
