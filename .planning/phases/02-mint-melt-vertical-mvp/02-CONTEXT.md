# Phase 02: Mint + Melt Vertical MVP - Context

**Gathered:** 2026-08-28
**Status:** Ready for planning

<domain>
## Phase Boundary

A user can mint a Lightning-funded bearer note by paying an invoice and melt it back to sats via the withdraw callback — with the full confirm-before-burn state machine, in-flight melt tracking, background reconciliation, store-hashes-not-secrets discipline, and all five critical security PoCs passing against LNbits fixtures.

This phase delivers the core mechanism of LUD-25 lnurlcash: the mint payRequest flow (LUD-06), the informational withdrawRequest (LUD-03), the mutating melt callback with async background payment, the tristate settlement contract (paid=True/False/None), in-flight melt tracking via refcount registry, background reconciliation of stranded notes, and the 5 critical PoC tests that encode the funds-loss security guarantees.

Phase 1 delivered the extension scaffold, complete data model (4 tables), and per-wallet mint CRUD. Phase 2 builds on that foundation with note CRUD, the LNURL endpoints, the service layer (fee math, settlement, reconcile), the background task, and the test suite.

</domain>

<decisions>
## Implementation Decisions

### Settlement Detection & Lazy Materialization
- On-demand polling via `check_payment_status(payment_hash)` when `/w` or `/verify` is hit — direct port of source's lazy materialization pattern. No LNbits payment listener/webhook coupling.
- Note materialized lazily on first `/w` or `/verify` poll after settlement: `UPDATE mints SET minted=1 WHERE payment_hash=:ph AND minted=0` + `INSERT INTO notes` in one atomic `async with db.connect() as conn:` transaction.
- Compare-and-set pattern: `UPDATE ... WHERE minted=0` + check `rowcount==1` (port source pattern). Winner inserts note, losers no-op. Note id PRIMARY KEY is backstop.
- If settlement check returns `pending` (unconfirmable): leave `minted=0`, return `{"status":"OK", "pending":true}` to holder — note not yet spendable. No false negative after N retries.

### In-Flight Melt Tracking
- Module-level `dict[str, int]` refcount registry (port source) — fast, in-process, cleared on restart. NOT a DB column (DB column can't distinguish "crashed mid-pay" from "still paying").
- Register melt as in-flight immediately after `mark_pending` succeeds, before HTTP response sent — prevents reconcile race.
- Clear from registry in `finally:` block of `_melt_pay` background task — cleared even on crash/exception.
- Reconcile skips in-flight melts entirely (doesn't call `check_payment_status`) — prevents false "not found" → restore while HTLC is still being sent.

### Reconcile Scheduling & Boot Behavior
- 60-second reconcile interval (reasonable default, source uses ~60s `funding_source_health_check_interval_seconds`).
- Boot-time one-shot reconcile in `lnurlmint_start()` before registering periodic task — resolves stranded notes from crashed process immediately on startup.
- Skip health-check integration; use `create_permanent_unique_task` + `run_interval` (giftcards pattern) — LNbits task wrapper already crash-restarts. Do NOT port source's `_monitor_funding_source` health-check-then-reconcile pattern.
- Unconfirmable melts (`paid=None`): log as internal error, leave pending, retry next tick — operator can investigate. Do NOT auto-restore after N unconfirmable ticks (risks double-spend if HTLC is actually in flight).

### PoC Test Architecture
- Port `FakeNode` pattern: monkeypatch `pay_invoice`/`check_payment_status` with controllable tristate behavior — direct port of source fixtures. Do NOT use LNbits' `FakeWallet` directly (can't easily model `paid=None` tristate).
- Model `paid=None` (unconfirmable) as `check_payment_status` raising `ValueError` for ambiguous states (port source's `is_payment_complete` pattern) — `_confirm_payment` catches and returns `None`.
- Test isolation: in-memory SQLite per test, `async with db.connect()` for setup/teardown — fast, isolated.
- All 5 PoC tests in one plan (Plan 5): TEST-01 (double_melt/duplicate_melt), TEST-02 (a2_settle_race), TEST-03 (melt_restore_double_payout — tristate), TEST-04 (reconcile_inflight_race), TEST-05 (f2_pending_info_leak). They're interdependent and ship together per ROADMAP.

### Claude's Discretion
- Exact retry backoff delays for `_confirm_payment` (source uses `(1,2,4,8,16)` ~31s total) — port or adjust as needed for LNbits test speed.
- Internal error logging format (source uses `log_internal_error` — map to LNbits `logging.error` or equivalent).
- Whether to expose reconcile status via management API (not required by ROADMAP, but useful for operators).
- Exact `run_interval` wrapper vs custom `while settings.lnbits_running` loop — both work, giftcards uses `create_permanent_unique_task` directly.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Phase 1 data model** — `mints`, `notes`, `mints_records`, `melts` tables already created (m001 + m002 migrations). `notes` has `spent`/`pending`/`pending_payment_hash` columns ready for the state machine. `mints_records.minted` is the compare-and-set flag.
- **Phase 1 CRUD** — `crud.py` has `create_mint`, `get_mint`, `update_mint`, `delete_mint`, `count_outstanding_notes` — all wallet-scoped. Note CRUD (`settle_mint`, `mark_pending`, `finalize_melt`, `restore`, `pending_melts`) will be added here.
- **Phase 1 models** — `Mint`, `CreateMint`, `UpdateMint`, `MintResponse`, `Note`, `MintRecord`, `MeltRecord` pydantic v1 models. `Note.state` property already derives 'spent'/'pending'/'outstanding' from flags.
- **LNbits services** — `create_invoice(wallet_id, amount, memo, ...)`, `pay_invoice(wallet_id, payment_request, ...)`, `check_payment_status(payment_hash)` from `lnbits.core.services.payments`.
- **LNbits tasks** — `create_permanent_unique_task(name, coro)`, `run_interval(interval, func)` from `lnbits.tasks`.
- **Giftcards pattern** — `giftcards_start()` / `giftcards_stop()` with `create_permanent_unique_task` + `scheduled_tasks` list.

### Established Patterns
- **DB transaction atomicity** — `async with db.connect() as conn:` for multi-statement ops (Phase 1 `delete_mint` pattern).
- **Wallet scoping** — every query includes `WHERE wallet = :wallet` or JOIN on `mints.wallet` (Phase 1 pattern).
- **pydantic v1** — `BaseModel`, `validator`, `root_validator`, `class Config` (NOT v2).
- **Auth decorators** — `require_admin_key` for mutating, `require_invoice_key` for read.
- **Store-hashes-not-secrets** — no preimage/secret/k1 column in any table (Phase 1 invariant).

### Integration Points
- **Extension lifecycle** — `lnurlmint_start()` / `lnurlmint_stop()` in `__init__.py` (currently no-op, will register background task).
- **API router** — `lnurlmint_ext` APIRouter (currently has management API, will add LNURL endpoints).
- **Generic views** — `views.py` has `index`/`index_public` (management UI placeholder, public one-pager in Phase 6).
- **Frontend** — `static/js/index.vue` management placeholder (Phase 6 adds full UI).

</code_context>

<specifics>
## Specific Ideas

- The tristate settlement contract is the single highest-risk port detail. `pay_invoice` raising `PaymentFailed` does NOT mean no HTLC was sent — the HTLC may still be held open. `check_payment_status` must be used to distinguish terminal failure (`paid=False`) from indeterminate (`paid=None`). This is encoded in TEST-03.
- The in-flight registry is the fix for the reconcile race: before the `pay_invoice` RPC lands, the node may truthfully report "not found" (lnd 404 / cln empty `listpays`), and an innocent reconcile would `restore` the note while the payment is still going out. This is encoded in TEST-04.
- Fee math must be mirrored exactly: `_mint_fee_msat` rounds UP to whole sats, `_melt_fee_limit_msat` uses `max(0.5%, 5000msat, mint_fee)`. These are protocol contracts, not implementation details.
- The source's `_confirm_payment` uses retry delays `(1,2,4,8,16)` (~31s total). For tests, `delays=()` is used for fast execution. The port should preserve this configurable delay pattern.

</specifics>

<deferred>
## Deferred Ideas

- Health-check integration (funding source reachability monitoring) — deferred; LNbits' `create_permanent_unique_task` handles crash-restart, and funding source health is LNbits core's responsibility.
- Reconcile status management API endpoint — useful for operators but not required by ROADMAP; can add in a later phase.
- Multiple notes melted into the same invoice (refcount > 1) — the source supports this via refcount, but the `melt_pr` duplicate guard currently rejects it. The refcount pattern is preserved for safety but the feature is not exercised in Phase 2.

</deferred>
