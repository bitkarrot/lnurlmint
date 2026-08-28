---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_phase: 01
status: in_progress
last_updated: "2026-08-28T19:00:00.000Z"
progress:
  total_phases: 7
  completed_phases: 0
  total_plans: 3
  completed_plans: 1
  percent: 33
---

# State: lnurlmint

**Project:** lnurlmint — LNbits extension implementing LUD-25 lnurlcash (Lightning bearer assets), ported from standalone `lnurl-mint` FastAPI app
**Current Phase:** 01
**Project Reference:** `.planning/ROADMAP.md`
**Mode:** mvp
**Started:** 2026-08-28

## Phase Status

| Phase | Name | Status | Plans Completed |
|-------|------|--------|----------------|
| 1 | Extension Scaffold + Data Model + Per-Wallet Mint CRUD | in_progress | 1/3 |
| 2 | Mint + Melt Vertical MVP | pending | 0/5 |
| 3 | Rotate + Split + Merge + Sunset | pending | 0/3 |
| 4 | Comment Protection + Verify | pending | 0/3 |
| 5 | Offline Verification | pending | 0/2 |
| 6 | Tor + Frontend | pending | 0/3 |
| 7 | Full Test Suite Port | pending | 0/2 |

## Current Focus

Phase 1: Extension scaffold + data model + per-wallet mint CRUD. Plan 01-01 complete — the walking skeleton (extension loads, m001_initial migration creates mints table, POST/GET management API with wallet scoping, placeholder Vue page). Plans 02 (notes/mints_records/melts tables + note CRUD) and 03 (full mint CRUD: get/update/delete + isolation tests) remain.

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

## Notes

- REQUIREMENTS.md stated 52 requirements; actual count is 63. Traceability updated with correct count.
- Phase 2 is the highest-risk phase (28 requirements): melt + confirm-before-burn + in-flight tracking + background reconcile + 5 critical PoC tests ship together.
- Phase 3 and Phase 4 can run in parallel (both depend on Phase 2, neither depends on the other).
- Plan 01-01 complete: walking skeleton verified E2E (extension loads, migration runs, POST/GET work, cross-wallet isolation holds).

---
*Last updated: 2026-08-28 (plan 01-01 complete)*
