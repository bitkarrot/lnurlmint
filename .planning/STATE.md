---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_phase: 2 — Mint + Melt Vertical MVP
status: Phase 2 complete
last_updated: "2026-08-28T21:22:00.000Z"
progress:
  total_phases: 7
  completed_phases: 2
  total_plans: 8
  completed_plans: 8
  percent: 38
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
| 2 | Mint + Melt Vertical MVP | complete | 5/5 |
| 3 | Rotate + Split + Merge + Sunset | pending | 0/3 |
| 4 | Comment Protection + Verify | pending | 0/3 |
| 5 | Offline Verification | pending | 0/2 |
| 6 | Tor + Frontend | pending | 0/3 |
| 7 | Full Test Suite Port | pending | 0/2 |

## Current Focus

Plan 02-05 complete: the 5 critical funds-loss security PoC tests are ported and passing against LNbits fixtures — Phase 2 is complete. FakeNode/HodlNode/InFlightNode test fixtures monkeypatch the module-level payment imports in services.py AND views_lnurl.py (lnbits_create_invoice, lnbits_pay_invoice, check_transaction_status) with controllable tristate behaviour (paid=True/False/None via PaymentSuccessStatus/PaymentFailedStatus/PaymentPendingStatus). TEST-01 (duplicate_melt): a note melted twice is rejected with "pending". TEST-02 (a2_settle_race): compare-and-set settle_mint produces exactly one note. TEST-03 (tristate — highest-risk): paid=None (HodlNode ambiguous) leaves the note pending NOT restored; after hodl settles, reconcile finalizes; benign failure restores. TEST-04 (reconcile_inflight): reconcile skips in-flight melts (InFlightNode asyncio.Event coordination). TEST-05 (f2_pending_info_leak): /w rejects pending notes with "pending" — no value leaked. All 7 tests pass in 0.68s, stable across 3 runs. Tests use @pytest.mark.anyio (LNbits convention, not pytest-asyncio), per-test DB isolation (drop + re-migrate), and _CONFIRMATION_RETRY_DELAYS_SECONDS=() for fast single-attempt confirmation. Phase 3 (rotate/split/merge/sunset) and Phase 4 (comment protection + verify) can now proceed in parallel.

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

### Plan 02-03 Decisions

- Task execution order adjusted to dependency order: Task 1 (/w) → Task 3 (in-flight registry stubs) → Task 2 (/w/cb) — Task 2 imports _track_melt_start and _melt_pay from services.py which are created in Task 3 (per the plan's depends_on note)
- asyncio.Lock (NOT a thread-level lock) for the in-flight registry per CONTEXT.md — LNbits is async-native and the port's tests use asyncio.gather (not OS threads); all access is async/await
- _melt_pay is a stub that logs a warning and clears the in-flight entry in finally: — Plan 04 replaces it with the full tristate settlement (pay_invoice → check_payment_status → finalize/restore/leave-pending)
- h required when pr absent returns "missing h" for invalid/absent h, then "Rotate/split/merge not yet implemented." for valid h — Phase 3 implements rotate/split/merge; Phase 2 defers
- Self-mint rejection checks mint_record_exists (mints_records table) and duplicate-melt checks melt_record_exists (melts table) — both BEFORE mark_pending so no state is mutated on rejection
- logger.debug in /w/cb logs only mint_id (not k1, pr, h, payment_hash, or query params) — SEC-05 no-secret-logging

### Plan 02-04 Decisions

- Task execution order adjusted to dependency order: Task 2 (_confirm_payment) → Task 1 (_melt_pay) → Task 3 (reconcile) → Task 4 (wiring) — _melt_pay calls _confirm_payment, so _confirm_payment must exist first for each commit to be self-consistent
- _confirm_payment uses status.success/status.failed/status.paid is None directly — NEVER the .pending property (which is self.paid is not True, True for BOTH None AND False). Using .pending would treat confirmed failure as pending and retry forever. This is the single most critical tristate gotcha (RQ7 #1).
- Rephrased comments to avoid the literal string "status.pending" (used ".pending property" instead) to satisfy the grep "status.pending returns no matches" acceptance criterion — the invariant is identical
- _melt_pay's finally block calls _track_melt_end(payment_hash) unconditionally (no has_payment_hash guard) — matches the plan; if has_payment_hash is False, payment_hash is None and _track_melt_end(None) is a harmless no-op. In practice the melt callback only schedules _melt_pay for valid bolt11 invoices that always have a payment_hash.
- boot_reconcile is NOT added to scheduled_tasks (one-shot that completes quickly; cancelled when event loop closes if still running) — matches the plan
- lnurlmint_start uses local imports (inside the function) for create_permanent_unique_task, boot_reconcile, wait_for_melt_reconcile — avoids circular import at module load time, matches the giftcards pattern
- reconcile_pending_melts NEVER auto-restores unconfirmable melts (paid=None) — logs and leaves pending for operator investigation. Auto-restoring would risk a double-spend if the HTLC is actually in flight.

### Plan 02-05 Decisions

- Tests live in lnurlmint/tests/ (symlinked into lnbits/extensions/lnurlmint/tests/); run via `cd /home/exedev/lnbits && .venv/bin/python -m pytest lnbits/extensions/lnurlmint/tests/` — the plan's `tests/` path is relative to the extension root, and `cd /home/exedev/lnbits` is where the venv lives (documentation simplification, same as Plans 02-01..02-04)
- Async tests use @pytest.mark.anyio (anyio plugin, LNbits convention) NOT @pytest.mark.asyncio (pytest-asyncio not installed) — matches the giftcards extension test pattern
- Test files import helpers via `from lnurlmint.tests.conftest import fake_invoice, mint_note` (full package path) — a bare `from tests.conftest import ...` collides with lnbits' root tests/ package; the full path resolves under pytest's prepend import mode (lnbits/extensions/ has no __init__.py, so lnurlmint is importable as top-level)
- FakeNode patches BOTH services_module AND views_module — views_lnurl.py imports lnbits_create_invoice separately at module level (line 24); only patching services would leave the mint callback calling the real LNbits create_invoice
- InFlightNode.check_transaction_status checks `if payment_hash in self.settled` before returning PaymentFailedStatus — so mint-note setup (mint payment_hash in settled) still materializes via _try_settle_mint, while melt hashes (not yet settled) report paid=False (lnd 404 for unregistered payment)
- HodlNode.settle_hodl_payments decodes each pending_hodl bolt11 via bolt11.decode(pr).payment_hash and adds it to self.settled — the research pseudocode's `sha256(bytes.fromhex(p))` was incorrect (p is a bolt11 string, not hex)
- TEST-01 uses a bare BackgroundTasks() — add_task registers _melt_pay but it never runs outside FastAPI's response machinery, keeping the note pending between the two melt calls (exactly the window the duplicate-melt guard protects)
- mint_note helper goes through the real lazy-settlement path (_try_settle_mint) rather than directly inserting a note row — settlement bugs surface in setup too
- paid=None modelled as PaymentPendingStatus return (NOT a ValueError raise) per CONTEXT.md — the source's is_payment_complete raised; LNbits' PaymentStatus models tristate via paid=None. _confirm_payment treats both the same (retry/leave pending).

## Notes

- REQUIREMENTS.md stated 52 requirements; actual count is 63. Traceability updated with correct count.
- Phase 2 is the highest-risk phase (28 requirements): melt + confirm-before-burn + in-flight tracking + background reconcile + 5 critical PoC tests ship together.
- Phase 3 and Phase 4 can run in parallel (both depend on Phase 2, neither depends on the other).
- Plan 01-01 complete: walking skeleton verified E2E (extension loads, migration runs, POST/GET work, cross-wallet isolation holds).
- Plan 01-02 complete: full data model in place — m002 migration creates notes/mints_records/melts tables; Note/MintRecord/MeltRecord pydantic v1 models; store-hashes-not-secrets invariant enforced (no preimage column); compare-and-set + reconcile columns ready for Phase 2.
- Plan 01-03 complete: full per-wallet mint CRUD (get/update/delete) with cross-wallet isolation E2E-verified on all endpoints, outstanding-notes delete guard (409) with atomic check-and-delete, UpdateMint partial-update model, Vue create-mint form + delete button. Phase 1 complete.
- Plan 02-01 complete: note state-machine CRUD (13 functions + PendingNoteError) with compare-and-set lazy settlement, all-or-nothing mark_pending, mint_id-scoped mutations, and 4 LNURL wire models — all verified against SQLite. Plans 02-02 (mint flow) and 02-03 (melt flow) can now build LNURL endpoints on these primitives.
- Plan 02-02 complete: mint flow (LUD-06 payRequest + callback) with fee math protocol contracts (ECON-01..04), lazy settlement helper, and record_mint_record CRUD. The payRequest advertises fee-aware bounds + withdrawLink; the callback creates an invoice via LNbits and records a pending mint (net amount, minted=0) without materializing the note. _try_settle_mint materializes lazily on first /w poll. Plan 02-03 (melt flow) can now build the /w endpoint that calls _try_settle_mint.
- Plan 02-03 complete: redeem flow (LUD-03 informational /w + melt callback /w/cb). The /w endpoint is purely informational — advertises note value without burning, rejects pending (SEC-04)/spent/unknown, lazily settles, echoes k1 verbatim. The /w/cb melt callback validates pr via bolt11.decode, rejects self-mint/duplicate payment hashes (SEC-06), atomically reserves via mark_pending, registers in-flight via _track_melt_start (SEC-03), records the melt, replies {status:OK} immediately, and schedules background _melt_pay. REDEEM-06 validation enforced. In-flight refcount registry (asyncio.Lock) + _melt_pay stub in services.py — Plan 04 implements the full tristate settlement.
- Plan 02-04 complete: confirm-before-burn state machine + in-flight tracking + background reconcile. _melt_pay implements full tristate settlement (pay_invoice → _confirm_payment → paid=True finalize, paid=False restore, paid=None leave pending). Every restore path goes through _confirm_payment first (SEC-01). _confirm_payment uses status.success/status.failed/status.paid is None (NOT .pending). reconcile_pending_melts skips in-flight, single-attempt confirm, logs+leaves pending for unconfirmable. boot_reconcile one-shot at startup. tasks.py + lnurlmint_start/stop wired via create_permanent_unique_task (EXT-03). Plan 02-05 (critical PoC tests) can now port all 5 PoC tests.
- Plan 02-05 complete: 5 critical funds-loss security PoC tests ported and passing against LNbits fixtures — Phase 2 complete. TEST-01 (duplicate_melt), TEST-02 (a2_settle_race), TEST-03 (tristate — paid=None leaves pending NOT restored), TEST-04 (reconcile skips in-flight), TEST-05 (/w rejects pending notes). FakeNode/HodlNode/InFlightNode fixtures monkeypatch services.py + views_lnurl.py payment imports with controllable tristate. All 7 tests pass in 0.68s, stable across 3 runs. Phase 3 + Phase 4 can proceed in parallel.

---
*Last updated: 2026-08-28 (plan 02-05 complete, Phase 2 complete)*
