---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_phase: 2 — Mint + Melt Vertical MVP
status: Ready to plan
last_updated: "2026-08-28T19:02:11.082Z"
progress:
  total_phases: 7
  completed_phases: 1
  total_plans: 3
  completed_plans: 3
  percent: 14
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
| 2 | Mint + Melt Vertical MVP | pending | 0/5 |
| 3 | Rotate + Split + Merge + Sunset | pending | 0/3 |
| 4 | Comment Protection + Verify | pending | 0/3 |
| 5 | Offline Verification | pending | 0/2 |
| 6 | Tor + Frontend | pending | 0/3 |
| 7 | Full Test Suite Port | pending | 0/2 |

## Current Focus

Phase 1 complete: the full per-wallet mint CRUD vertical slice is delivered — extension scaffold, complete data model (all four tables), POST/GET/GET/{id}/PUT/{id}/DELETE/{id} management API with cross-wallet isolation E2E-verified, outstanding-notes delete guard (409), and Vue placeholder with create/delete interactivity. Phase 2 (Mint + Melt Vertical MVP) is next — the highest-risk phase (confirm-before-burn state machine, in-flight melt tracking, background reconciliation, 5 critical PoC tests).

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

## Notes

- REQUIREMENTS.md stated 52 requirements; actual count is 63. Traceability updated with correct count.
- Phase 2 is the highest-risk phase (28 requirements): melt + confirm-before-burn + in-flight tracking + background reconcile + 5 critical PoC tests ship together.
- Phase 3 and Phase 4 can run in parallel (both depend on Phase 2, neither depends on the other).
- Plan 01-01 complete: walking skeleton verified E2E (extension loads, migration runs, POST/GET work, cross-wallet isolation holds).
- Plan 01-02 complete: full data model in place — m002 migration creates notes/mints_records/melts tables; Note/MintRecord/MeltRecord pydantic v1 models; store-hashes-not-secrets invariant enforced (no preimage column); compare-and-set + reconcile columns ready for Phase 2.
- Plan 01-03 complete: full per-wallet mint CRUD (get/update/delete) with cross-wallet isolation E2E-verified on all endpoints, outstanding-notes delete guard (409) with atomic check-and-delete, UpdateMint partial-update model, Vue create-mint form + delete button. Phase 1 complete.

---
*Last updated: 2026-08-28 (plan 01-03 complete, Phase 1 complete)*
