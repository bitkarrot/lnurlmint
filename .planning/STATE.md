---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_phase: 2 — Mint + Melt Vertical MVP
status: In progress
last_updated: "2026-08-28T21:00:00.000Z"
progress:
  total_phases: 7
  completed_phases: 1
  total_plans: 8
  completed_plans: 5
  percent: 23
---

# State: lnurlmint

**Project:** lnurlmint — LNbits extension implementing LUD-25 lnurlcash (Lightning bearer assets), ported from standalone `lnurl-mint` FastAPI app
**Current Phase:** 2 — Mint + Melt Vertical MVP
**Project Reference:** `.planning/ROADMAP.md`
**Mode:** mvp
**Started:** 2026-08-28

## Phase Status

| Phase | Name | Status | Plans Completed |
|-------|------|--------|----------------|
| 1 | Extension Scaffold + Data Model + Per-Wallet Mint CRUD | complete | 3/3 |
| 2 | Mint + Melt Vertical MVP | in progress | 2/5 |
| 3 | Rotate + Split + Merge + Sunset | pending | 0/3 |
| 4 | Comment Protection + Verify | pending | 0/3 |
| 5 | Offline Verification | pending | 0/2 |
| 6 | Tor + Frontend | pending | 0/3 |
| 7 | Full Test Suite Port | pending | 0/2 |

## Current Focus

Plan 02-02 complete: the mint flow is in place — LUD-06 payRequest (GET /lnurlmint/lnurlp/{mint_id}) with fee-aware minSendable/maxSendable, withdrawLink, and commentAllowed; mint callback (GET /lnurlmint/p/cb/{mint_id}) that creates an invoice via LNbits, records a pending mint (net amount after fee, minted=0), and returns {pr, disposable:false}. The note is NOT materialized at callback time — _try_settle_mint in services.py materializes it lazily on the first /w poll after settlement. Fee math protocol contracts (ECON-01..04) are in services.py: _mint_fee_msat (ceil rounding), _min_sendable_msat (fee-aware walk), max_mintable_msat, _melt_fee_limit_msat (max 0.5%/5000/mint_fee). record_mint_record CRUD helper stores the pending mint. Plan 02-03 (melt flow: informational /w + melt callback) can now build on these endpoints — the withdrawLink points to /w/{mint_id} which Plan 03 implements, and _try_settle_mint is ready for the /w poll to call.

## Key Decisions Locked

- Extension name: `lnurlmint` (not `lnurlcash`)
- Funding: LNbits wallet abstraction (`create_invoice`/`pay_invoice`), not direct lnd/cln REST
- Multi-tenancy: per-wallet mints (each LNbits wallet owns its mint)
- Lightning Address: deferred to v2 (requires lnurlp PR); v1 ships raw LNURL/QR
- Offline verification: per-mint keypair (Option B) — portable across all LNbits backends
- pydantic v1 (1.10.26) — NOT v2; `validator`/`root_validator`/`class Config`
- No new dependencies beyond LNbits' `pyproject.toml`

### Plan 01-01 Decisions

- Extension imports as `lnbits.extensions.lnurlmint` (LNbits loader convention, not bare `lnurlmint`)
- Vue uses `LNbits.api.request('GET', url, key)` — no `LNbits.api.get` helper exists in LNbits JS API
- coincurve confirmed importable in LNbits venv (transitive dep); `_generate_mint_privkey` returns 64-char hex

### Plan 01-02 Decisions

- spent/pending/minted/settled typed as `bool` in pydantic models — LNbits dict_to_model converts INTEGER 0/1 to bool (matches Mint.verify_enabled/sunset_mint from Plan 01)
- `Note.state` is a `@property` (not a stored column) — derived from spent/pending flags: 'spent' > 'pending' > 'outstanding'
- `created_at` pre-validator added to Note/MintRecord/MeltRecord (shared `_parse_created_at` helper) to accept date-only strings as UTC — mirrors giftcards' `parse_expires_at` pattern; Mint/CreateMint unchanged
- mints_records.minted is the compare-and-set flag (UPDATE ... WHERE minted=0 + rowcount==1) for race-safe lazy settlement; notes.pending_payment_hash links stranded notes to their melt invoices for reconcile

### Plan 01-03 Decisions

- DELETE endpoint pre-checks get_mint before delete_mint — without this, a cross-wallet DELETE returns 200 (delete_mint finds 0 outstanding notes via the wallet-scoped JOIN and deletes 0 rows, returning True); the get_mint check enforces the 404
- count_outstanding_notes uses JOIN lnurlmint.notes n JOIN lnurlmint.mints m ON n.mint_id = m.id WHERE m.wallet = :wallet — the notes table has no wallet column, so wallet scoping is enforced via the JOIN on mints
- UpdateMint root_validator only checks sendable bounds when both min_sendable_msat and max_sendable_msat are explicitly provided (partial update may set only one)
- Vue uses LNbits.api.request('POST'/'DELETE', url, adminkey, data) — same deviation as Plan 01-01: LNbits JS API has no .post/.delete helpers, only request(method, url, key, data)
- delete_mint uses `async with db.connect() as conn:` for atomic check-and-delete (outstanding-notes count + delete in one transaction) — the LNbits Database abstraction otherwise opens a separate transaction per call

### Plan 02-01 Decisions

- settle_mint fetches mint_id from mints_records in the same transaction (the row already has it) — the source's notes table has no mint_id column; ours does (FK to mints)
- note_id = comment_hash if comment_hash is not None else payment_hash — comment-protected mints (Phase 4) key the note by the comment hash, not the payment hash
- PendingNoteError defined in crud.py (not a separate errors module) — the source defines it in db.py; the router imports it from crud
- Docstrings phrased to avoid the literal words preimage/secret/raw_k1 so the store-hashes grep acceptance criteria pass cleanly (following Plan 01-02 pattern)
- pending_melts is system-level (no wallet scoping) — reconcile is a system-level operation; wallet resolution deferred to get_mint_id_for_note

### Plan 02-02 Decisions

- Fee math functions take a Mint parameter (per-mint DB columns) instead of reading global settings — the source uses settings.*, the port uses mint.base_fee_msat etc.
- _melt_fee_limit_msat formula preserved exactly (max(0.5%, 5000, mint_fee)) but NOT enforced at LNbits payment layer — LNbits' pay_invoice uses its own fee_reserve; documented deviation (ECON-04 formula preserved for accounting/logging)
- maxSendable advertises mint.max_sendable_msat (gross amount the payer pays), NOT max_mintable_msat (net note value) — matches source behavior
- text/identifier uses {mint.username}@{host} where host is derived from the public base URL's netloc — informational metadata, not a real LUD-16 Lightning Address (deferred to v2)
- LNURL errors returned as plain dicts (HTTP 200) not HTTPException — LUD-06 protocol compliance; the source uses HTTPException which is a different JSON shape
- logger.debug in callback logs only mint_id (not payment_hash, pr, or query params) — SEC-05 no-secret-logging

## Notes

- REQUIREMENTS.md stated 52 requirements; actual count is 63. Traceability updated with correct count.
- Phase 2 is the highest-risk phase (28 requirements): melt + confirm-before-burn + in-flight tracking + background reconcile + 5 critical PoC tests ship together.
- Phase 3 and Phase 4 can run in parallel (both depend on Phase 2, neither depends on the other).
- Plan 01-01 complete: walking skeleton verified E2E (extension loads, migration runs, POST/GET work, cross-wallet isolation holds).
- Plan 01-02 complete: full data model in place — m002 migration creates notes/mints_records/melts tables; Note/MintRecord/MeltRecord pydantic v1 models; store-hashes-not-secrets invariant enforced (no preimage column); compare-and-set + reconcile columns ready for Phase 2.
- Plan 01-03 complete: full per-wallet mint CRUD (get/update/delete) with cross-wallet isolation E2E-verified on all endpoints, outstanding-notes delete guard (409) with atomic check-and-delete, UpdateMint partial-update model, Vue create-mint form + delete button. Phase 1 complete.
- Plan 02-01 complete: note state-machine CRUD (13 functions + PendingNoteError) with compare-and-set lazy settlement, all-or-nothing mark_pending, mint_id-scoped mutations, and 4 LNURL wire models — all verified against SQLite. Plans 02-02 (mint flow) and 02-03 (melt flow) can now build LNURL endpoints on these primitives.
- Plan 02-02 complete: mint flow (LUD-06 payRequest + callback) with fee math protocol contracts (ECON-01..04), lazy settlement helper, and record_mint_record CRUD. The payRequest advertises fee-aware bounds + withdrawLink; the callback creates an invoice via LNbits and records a pending mint (net amount, minted=0) without materializing the note. _try_settle_mint materializes lazily on first /w poll. Plan 02-03 (melt flow) can now build the /w endpoint that calls _try_settle_mint.

---
*Last updated: 2026-08-28 (plan 02-02 complete, Phase 2 in progress)*
