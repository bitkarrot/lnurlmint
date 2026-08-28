# Phase 03: Rotate + Split + Merge + Sunset - Context

**Gathered:** 2026-08-28
**Status:** Ready for planning

<domain>
## Phase Boundary

A note holder can rotate, split, and merge bearer notes using WALLET-supplied `h`/`h2` secret hashes, with sunset mode gating new issuance — completing the full redeem lifecycle.

This phase delivers the `swap` primitive (atomic burn N notes + mint M notes in one `db.connect()` block), the rotate/merge/split callback branches in `/w/cb`, sunset mode gating (reject mint + split, allow rotate/merge/melt), and the fee conservation + collision griefing PoC tests.

Phase 2 delivered the melt flow with confirm-before-burn tristate settlement. Phase 3 extends the `/w/cb` callback to handle the non-melt branches (rotate/split/merge) using the same note state machine.

</domain>

<decisions>
## Implementation Decisions

### Offline Signing (sig/sig2) Timing
- Defer to Phase 5 (Offline Verification) — Phase 3 returns `{"status":"OK"}` without sig/sig2. The `WithdrawSuccessResponse` model already has optional `sig`/`sig2` fields (set to None).
- Stub `sign_note`: `async def sign_note(h, amount, mint) -> None` returns None. Phase 5 implements real signing with per-mint keypair (coincurve).
- Do NOT reference `sign_note` in production code paths until Phase 5. The stub exists only so the callback can call it without import errors.

### swap() Collision Check
- Check both `mints_records` (pending mint invoices) AND `notes` (existing note ids) for collisions — prevents the A1 pending-mint squat attack (TEST-08).
- Collision check runs inside the same `async with db.connect() as conn:` block as the burn+mint — atomic, all-or-nothing. No race window between check and insert.
- Collision on either table → reject with `{"status":"ERROR","reason":"Invalid or already spent k1."}` (port source error message).

### Claude's Discretion
- Whether to add a dedicated `swap` function to `crud.py` or extend existing note CRUD — either is fine as long as it's atomic.
- Exact split/merge fee arithmetic is fully specified by the source (change = total - amount - base_fee; refund = (n-1) * base_fee) — port directly.
- Sunset gating points are specified by the source (/p/cb and /w/cb split branch only) — port directly.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Phase 2 note CRUD** — `settle_mint`, `mark_pending`, `finalize_melt`, `restore`, `pending_melts`, `get_note`, `get_mint_by_id` in `crud.py`. The `swap` function will be added here.
- **Phase 2 /w/cb callback** — Currently handles melt only (pr present). The rotate/split/merge branches will be added when pr is absent but h is present.
- **Phase 2 fee math** — `_mint_fee_msat`, `_min_sendable_msat`, `_melt_fee_limit_msat` in `services.py`. Split/merge fee arithmetic uses `base_fee_msat` from the mint row.
- **Phase 2 test fixtures** — `FakeNode`, `HodlNode`, `InFlightNode` in `tests/conftest.py`. PoC tests for Phase 3 will reuse these fixtures.
- **Phase 2 LNURL wire models** — `WithdrawSuccessResponse` already has optional `sig`/`sig2` fields.

### Established Patterns
- **DB transaction atomicity** — `async with db.connect() as conn:` for multi-statement ops (Phase 1 + Phase 2 pattern).
- **Wallet scoping** — all note queries scoped by `mint_id` (which maps to wallet via mints table).
- **LNURL error format** — `{"status":"ERROR","reason":"..."}` with HTTP 200.
- **No-secret-logging** — no k1/pr/preimage/h/h2 in logs.
- **pydantic v1** — BaseModel, validator, class Config.

### Integration Points
- **`/w/cb` callback** — `views_lnurl.py` currently rejects non-melt requests with "Rotate/split/merge not yet implemented." — this will be replaced with real branches.
- **`/p/cb` callback** — `views_lnurl.py` mint callback — sunset check will be added.
- **`crud.py`** — `swap` function will be added alongside existing note CRUD.
- **`services.py`** — `sign_note` stub will be added (Phase 5 implements real signing).

</code_context>

<specifics>
## Specific Ideas

- The `swap` primitive is the core of rotate/split/merge: atomically burn N notes (UPDATE SET spent=1) and mint M notes (INSERT) in one `db.connect()` block. If ANY burn fails (invalid/spent/pending note) or ANY mint fails (collision), the entire transaction rolls back.
- Split fee arithmetic: `change = total - amount - base_fee_msat`. Reject if `change < 1` (zero-value note). The split costs exactly one `base_fee_msat`, taken from the change side.
- Merge fee arithmetic: `refund = (n-1) * base_fee_msat`. Output note value = `sum(inputs) + refund`. Merging n notes costs only one base fee (refund of n-1).
- Sunset gating: `/p/cb` (mint) and `/w/cb` split branch reject when `sunset_mint=True`. Rotate, merge, and melt remain open so holders can consolidate/redeem.
- Collision griefing prevention: `swap` checks that new note ids (h/h2) don't collide with existing `notes.id` OR `mints_records.payment_hash`. This prevents an attacker from squatting a victim's future note id.
- Fee conservation PoC: `paid_in == outstanding + melted_out + fees - refunds` must hold after any sequence of operations. No fee rounding or swap arithmetic allows attacker gain.

</specifics>

<deferred>
## Deferred Ideas

- Offline signing (sig/sig2) — deferred to Phase 5. Phase 3 returns {status:OK} without signatures.
- `sign_note` implementation — Phase 5 implements with per-mint keypair (coincurve).
- `verify` field in melt response — Phase 4 implements the /verify endpoint.

</deferred>
