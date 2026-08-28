# Walking Skeleton — lnurlmint LNbits Extension

**Phase:** 1
**Generated:** 2026-08-28

## Capability Proven End-to-End

An LNbits wallet owner can create a per-wallet mint via the management API (`POST /lnurlmint/api/v1/mints`) and view it on the extension's placeholder Vue page — proving the full stack: extension loads in LNbits, migration runs creating the `mints` table, a mint row is written and read back, and the UI displays it.

## Architectural Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Framework | LNbits extension as a FastAPI `APIRouter` (`prefix="/lnurlmint"`) | Reuses LNbits runtime, wallet auth, DB abstraction, and Lightning primitives; extension is the only supported deployment model per project constraints |
| Data layer | `lnbits.db.Database("ext_lnurlmint")` with async migrations (`m001_initial`, `m002_*`) | SQLite for local dev, PostgreSQL for production, all via LNbits core DB abstraction; follows the giftcards extension pattern |
| Auth | `require_admin_key` for create/update/delete, `require_invoice_key` for read | LNbits provides wallet-scoped API keys; `wallet.wallet.id` derived from the decorator, never from request body — enforces per-wallet scoping at the auth layer |
| Multi-tenancy | Per-wallet mints — every query carries `WHERE wallet = :wallet` | Each LNbits wallet owns its mint; no cross-wallet access possible at the query layer (giftcards pattern) |
| pydantic version | pydantic v1 (1.10.26) — `BaseModel`, `validator`, `root_validator`, `class Config` | LNbits pins v1; v2 syntax (`field_validator`/`model_validator`) is incompatible and must not be used |
| Keypair generation | `coincurve.PrivateKey().secret.hex()` at mint creation, stored in `mints.mint_privkey` | Transitive dep already imported by LNbits' own nostr/nwc code; keypair generated now avoids a migration in Phase 5 |
| Frontend | Vue 3 + Quasar SFCs via LNbits vendor bundles, `static/routes.json` route registration | LNbits-conventional SPA pattern; giftcards `static/` layout as structural template |
| Background tasks | `lnurlmint_start`/`lnurlmint_stop` stubs (Phase 2 wires `create_permanent_unique_task`) | Skeleton needs the lifecycle hooks to exist and be exported; actual task wiring deferred to Phase 2 |
| Directory layout | Extension files at repo root (`__init__.py`, `models.py`, `crud.py`, `views_api.py`, `views.py`, `migrations.py`, `static/`), symlinked into `~/lnbits/lnbits/extensions/lnurlmint` | Matches giftcards extension structure and LNbits extension loading conventions |
| Deployment target | Local LNbits dev instance at `/home/exedev/lnbits` via extension symlink | Target LNbits v1.5.4 installation is present in the workspace; tests run against it |
| Dependencies | No new Python packages beyond LNbits' `pyproject.toml` | LNbits extension policy forbids new deps; `coincurve` is transitive (already used by LNbits core) |

## Stack Touched in Phase 1

- [ ] Extension scaffold — `__init__.py`, `manifest.json`, `config.json`, `views.py`, `crud.py`, `views_api.py`, `migrations.py`, `models.py`, `static/`
- [ ] Routing — management API under `/lnurlmint/api/v1/mints`, SPA route `/lnurlmint/`
- [ ] Database — real read/write via `lnurlmint.mints` table (m001_initial); SQLite/PostgreSQL compatible migration
- [ ] UI — placeholder Vue page that fetches and displays the mint list from the management API
- [ ] Deployment — extension symlinked into `~/lnbits/lnbits/extensions/lnurlmint`, loads in LNbits

## Out of Scope (Deferred to Later Slices)

- LNURL endpoints (payRequest, withdraw, callback, verify) — Phase 2
- Note/mint_record/melt CRUD operations (settle_mint, swap, mark_pending, etc.) — Phase 2
- Confirm-before-burn state machine — Phase 2
- Background reconcile task — Phase 2
- Mint fee math — Phase 2
- Rotate/split/merge — Phase 3
- Comment protection + verify endpoint — Phase 4
- Offline verification (signing) — Phase 5
- Tor base-URL substitution — Phase 6
- Full management SPA + public one-pager — Phase 6
- Lightning Address (LUD-16) — v2
- Full test suite port — Phase 7

## Subsequent Slice Plan

Each later phase adds one vertical slice on top of this skeleton without altering its architectural decisions:

- Phase 2: Mint + Melt Vertical MVP — A user can mint a Lightning-funded bearer note and melt it back to sats with the full confirm-before-burn state machine.
- Phase 3: Rotate + Split + Merge + Sunset — A note holder can rotate, split, and merge bearer notes; sunset mode gates new issuance.
- Phase 4: Comment Protection + Verify — LUD-25 comment protection and LUD-21 verify endpoint with real off-switch.
- Phase 5: Offline Verification — Per-mint keypair signing with `sig`/`sig2` on rotate/split/merge.
- Phase 6: Tor + Frontend — Tor base-URL substitution, management SPA, public one-pager.
- Phase 7: Full Test Suite Port — Complete behavioral parity verification.
