# Pitfalls Research

**Domain:** LUD-25 lnurlcash mint — bearer-asset Lightning notes, ported from standalone `lnurl-mint` (FastAPI + sqlite + module-level lock + direct lnd/cln REST) to an LNbits extension (async `Database` abstraction, per-wallet `pay_invoice`/`create_invoice`, `create_permanent_unique_task` lifecycle).
**Researched:** 2026-08-28
**Confidence:** HIGH — drawn from `lnurl-mint`'s README security model, its `router.py`/`db.py`/`signing.py` source, nine PoC test files, the LNbits `Database`/`payments`/`wallets` source, and the `giftcards` reference extension.

> **Read this first.** This is a bearer-asset system. The pitfalls in group (A) are **funds-loss bugs**, not crashes — a regression there silently destroys or duplicates real sats. They are distinguished from convenience-class pitfalls throughout.

---

## Critical Pitfalls

### Pitfall 1: Burning a note on a guess instead of positive settlement confirmation (confirm-before-burn)

**What goes wrong (FUNDS-LOSS):**
A melt reserves the note (`mark_pending`), replies `{"status": "OK"}`, then pays the invoice in the background. If the port destroys the note on anything other than a *positive* "payment settled" confirmation — e.g. it treats a `PaymentFailed` exception as proof the payment never went through, or treats an ambiguous/timeout outcome as failure — a hodl-invoice attacker can capture the note's value *and* still settle the held HTLC later: the melter's sats leave the node, the note is gone, the attacker keeps both. Conversely, if the port burns on an *unconfirmable* outcome, a momentary funding-source hiccup permanently destroys a note whose payment may still be in flight. The `test_melt_restore_double_payout_poc.py` suite encodes exactly this: `HodlNode` models a payee that holds the HTLC open after the payer's node reports `FAILED`, and the assertions prove the note must stay **pending** (frozen, unusable, but not destroyed) until the outcome is positively confirmed either way.

**Why it happens:**
The natural instinct is "payment raised → failed → restore the note." But `PaymentFailed` from a Lightning client is a *local* terminal status, not proof no HTLC remains outstanding — a hodl-invoice recipient holding the preimage can settle the held HTLC any time before CLTV expiry, *after* the payer gave up. The standalone code's `_melt_pay` docstring spells this out: a `PaymentFailed` is "a clean failure *response*, not proof no HTLC remains outstanding" and is therefore "handled the same as any other raise below: still confirmed independently before anything is restored, never treated as reason enough on its own."

**How to avoid:**
- Three terminal states only, gated on **positive** confirmation:
  1. `pay_invoice` returns success → `finalize_melt` (burn for good) + `mark_melt_settled`.
  2. `pay_invoice` raises → call `is_payment_complete` (the LNbits equivalent of `check_payment_status`); only if it returns a **definitive False** (payment genuinely never went out — no HTLC held) → `restore`. If it returns **pending/None/raises** → leave the note **pending** for reconcile/operator.
  3. `is_payment_complete` cannot answer after retries → leave **pending**, log to `error.log` (durable record), never assume.
- Never restore on `PaymentFailed` alone. Never finalize on "probably paid." Never destroy on "can't tell."
- Preserve the `_confirm_payment` retry-with-backoff shape (`_CONFIRMATION_RETRY_DELAYS_SECONDS`): a still-failing funding source is retried a few times before giving up and leaving the note pending, so a momentary RPC hiccup doesn't strand every in-flight melt.

**Warning signs:**
- A melt test that asserts `restore` is called directly after a `PaymentFailed`/`PaymentError` — that's the vulnerable shape.
- Any code path where `finalize_melt` runs without a preceding positive settlement signal.
- Notes that disappear (spent=1) while the funding source still shows the outgoing payment as pending/in-flight.
- Reconcile logic that maps "payment unknown / 404 / empty listpays" to "restore" — that's the pre-fix `is_payment_complete` bug (see Pitfall 2).

**Phase to address:**
Phase 1 (core melt path). The confirm-before-burn state machine is the single most load-bearing piece of the port; it must be designed before any melt code is written and locked by the ported `test_melt_restore_double_payout_poc.py` PoCs (`test_variant_a_ambiguous_failure_leaves_the_note_pending_not_restored`, `test_variant_b_payment_failed_is_still_confirmed_before_restoring`, `test_control_benign_failure_restores_and_retry_pays_exactly_once`).

---

### Pitfall 2: Reconcile restoring a note whose melt is still live in-process (in-flight melt tracking)

**What goes wrong (FUNDS-LOSS / double-spend):**
The background reconcile task (runs at boot and on every healthy monitor tick) iterates `pending_melts()` and confirms each. For a melt whose `_melt_pay` is running *right now in this same process*, the funding source can legitimately report "payment unknown" — lnd's `TrackPaymentV2` 404s, cln's `listpays` is empty — *simply because `pay_invoice`'s RPC hasn't landed yet*, not because the payment failed. If reconcile trusts that "not paid" answer and calls `restore`, the note is freed back to circulation while its payment is still going out. The holder rotates the value out, the payment then settles, and the same sats are paid out twice. `test_poc_reconcile_inflight_race.py` (`InFlightNode`) reproduces the exact window: `pay_invoice` is entered but parked before any RPC registers node-side, `is_payment_complete` truthfully returns False, and the pre-fix code restored the note — funds gone AND value still outstanding.

**Why it happens:**
`pending_melts()` (the DB query `SELECT id, pending_payment_hash FROM notes WHERE pending = 1`) cannot distinguish a cross-restart leftover (genuine stranded note, no live attempt) from a melt whose background task is alive in this very process. The database row looks identical. Without an in-process registry of live attempts, reconcile has no way to know which pending notes to skip.

**How to avoid:**
- Preserve the `_in_flight_melts` registry pattern: `get_withdraw_callback` registers the melt's `payment_hash` the **instant `mark_pending` succeeds — before the response goes out and the background task starts** — and `_melt_pay` drops it in a `finally`.
- `reconcile_pending_melts` **skips** any `payment_hash` present in `_in_flight_melts` *without even consulting the funding source* (the test asserts `is_payment_complete_calls == 0` for the in-flight case).
- The registry is **refcounted**: two melts of different notes into the same invoice share one payment hash; the hash stays skipped until *both* attempts finish (`test_in_flight_registry_refcounts_duplicate_payment_hashes`).
- Across a restart the registry is empty by construction (background tasks don't survive), so boot/time reconcile still picks up genuine leftovers — `test_leftover_pending_note_is_still_reconciled` pins this.
- **LNbits port note:** the registry is in-process state, not DB state. It must live in the extension module (a `dict[str, int]` + `asyncio.Lock`), registered in the request handler *before* scheduling the background task, cleared in the task's `finally`. Do NOT try to encode "in-flight" in the DB — a crashed process can't clean it up, and you'd reintroduce the very ambiguity the registry exists to resolve.

**Warning signs:**
- Reconcile code that calls `is_payment_complete` / `check_payment_status` on every pending note unconditionally.
- A melt handler that schedules the background task *before* registering in-flight, or registers *after* the response.
- No `finally` dropping the registration → a crashed background task leaves the hash registered forever, and its note never gets reconciled even after a restart (the opposite failure: stranded notes that never recover).
- Tests that don't exercise the `mark_pending → pay_invoice entered → reconcile runs` interleaving on one event loop.

**Phase to address:**
Phase 1 (melt path + reconcile task together — they are one mechanism). The reconcile task is wired via `create_permanent_unique_task` in `lnurlmint_start`; the registry must exist in the same phase. Locked by `test_poc_reconcile_inflight_race.py` (all three tests).

---

### Pitfall 3: Persisting a raw secret instead of `sha256(k1)` (store-hashes-not-secrets)

**What goes wrong (FUNDS-LOSS / silent theft):**
Notes are stored keyed by `sha256(k1)`, never the raw `k1`. A minted note's id is exactly the payment hash of its funding invoice; a rotated/split/merged note's id is the WALLET-supplied `h`/`h2` (already a hash). If the port accidentally persists the raw preimage/secret — in a column, a log line, an error message, a debug endpoint, or a migration that echoes it — anyone with DB or log access can spend every outstanding note. The README is explicit: "No spendable secret is ever persisted... a leaked database reveals how many notes are outstanding and for how much, but lets nobody spend them." The preimage is discarded at invoice-creation time in `/p/cb`; the mint never sees rotate/split/merge secrets at all.

The related PoC, `test_poc_f2_pending_info_leak.py`, is the *informational* leak variant: pre-fix, `/w` answered a full withdrawRequest for a pending note (see Pitfall 4), which is a *value* leak, not a secret leak. The secret-leak class is more severe: a persisted raw `k1` turns the DB into a spendable treasure map.

**Why it happens:**
- A migration or debug column added "for convenience" that stores the preimage to "make verify easier" — but `_mint_preimage`/`_melt_preimage` deliberately fetch the preimage **live from the funding source on every call, never cached locally**, precisely so the DB never holds it.
- Logging query strings on `/w`/`/w/cb` (the README disables uvicorn's access log entirely for this reason — a bearer `k1` can sit in a withdraw URL far longer than an ephemeral LUD-03 `k1`).
- An error handler that echoes the raw `k1` or `pr` in an exception message handed back on the wire (`log_internal_error` exists specifically to keep backend error text off the wire).
- Storing the `pr` of a mint invoice is fine (it's needed for LUD-21 verify and contains no secret), but storing its *preimage* is not.

**How to avoid:**
- The `notes`, `mints`, `melts` tables store only: `id` (= `sha256(k1)` / payment_hash / `h`/`h2`), `amount_msat`, `spent`, `pending`, `pending_payment_hash`, `pr` (the invoice string, for verify), `comment_hash`, `settled`. **No preimage column, ever.**
- `/p/cb` generates the preimage via `create_invoice`, computes `payment_hash = sha256(preimage)`, stores the hash + `pr`, and **discards the preimage** — it reaches the buyer only through the Lightning payment itself.
- Verify (`/verify/{payment_hash}`) fetches the preimage **live** from the funding source on every call; never cache it in the DB or in-process beyond the single request.
- Disable/scrub access logging on `/w`, `/w/cb`, `/verify` paths (or the whole server, as the standalone does). Never log raw `k1`.
- Error responses use `log_internal_error` (returns a reference id, keeps backend text off the wire); never echo a raw secret in a `reason` string.

**Warning signs:**
- Any DB column named `preimage`, `secret`, `k1`, `raw_k1`, or a `pr` column on `notes` (the `pr` belongs on `mints`/`melts` for verify, not on the spendable-note table).
- A verify implementation that reads the preimage from a local cache/DB instead of fetching it live.
- Access logs that contain `?k1=` query strings.
- A test that asserts a preimage is retrievable from the DB.

**Phase to address:**
Phase 1 (DB schema + `/p/cb` + verify). The schema is fixed at migration time; getting it wrong means a data migration later with outstanding notes. Locked by the store-hashes invariant implicit across the whole suite, and by `test_poc_verify_race.py`'s `test_theft_chain_closed_because_comment_makes_the_preimage_harmless` (which relies on the preimage never being the note's stored key under comment protection).

---

### Pitfall 4: The informational `/w` endpoint advertising a pending note as withdrawable (sell-during-melt scam)

**What goes wrong (FUNDS-LOSS scam):**
While a melt holds a note reserved (`pending=1`), every *mutating* callback (`/w/cb`) correctly rejects it with `{"status": "ERROR", "reason": "pending"}`. But if the informational `/w` endpoint *doesn't* also reject pending notes, it answers a valid LUD-03 withdrawRequest with `min=max=full value`, byte-for-byte indistinguishable from a freely spendable note. That gap is the sell-during-melt scam: seller melts, shows the buyer the healthy-looking `/w`, buyer pays out-of-band, the buyer's rotate fails "pending", the melt settles, the note is gone, the buyer is left with nothing. `test_poc_f2_pending_info_leak.py` reproduces it: during the pending window `/w` must return `{"status": "ERROR", "reason": "pending"}` (the spec's *distinct* pending reason, same as `/w/cb`), and after a *failed* melt (restore) `/w` must show the note as withdrawable again — pending is a transient state, not a one-way taint flag.

**Why it happens:**
`note_amount` filtering `spent = 0` but not `pending = 0` is the natural query — "is there an outstanding note for this id?" The pending flag is easy to forget on a read path that "burns nothing." But the read path's *answer* is what a buyer trusts in an out-of-band sale, so a pending note being advertised as full-value is a lie that costs real sats.

**How to avoid:**
- `get_withdraw` (`/w`) must check `note_pending(note_id)` and reject with `{"status": "ERROR", "reason": "pending"}` — the *same* reason string `/w/cb` uses, per spec.
- `note_amount` / `note_pending` / `note_spent` are three distinct queries returning three distinct states (unknown / pending / spent); `/w` reports each differently ("Unknown note" / "pending" / "Note already spent.").
- After `restore`, `pending` clears to 0 and `/w` advertises the note again — verify with `test_w_shows_the_note_again_after_a_failed_melt_restores_it`.

**Warning signs:**
- `/w` returning a `withdrawRequest` for a note that's mid-melt.
- `note_amount` query lacking `AND pending = 0` (or the read path not consulting `pending`).
- A "pending" reason that differs between `/w` and `/w/cb` (spec requires the same string).
- No test exercising the `/w`-during-melt interleaving on one event loop.

**Phase to address:**
Phase 1 (withdraw endpoint). Locked by `test_poc_f2_pending_info_leak.py` (both tests).

---

### Pitfall 5: Mint fee rounding down / not covering routing at melt (fee conservation)

**What goes wrong (FUNDS-LOSS for the mint, or inflation for the holder):**
The mint fee (`BASE_FEE_MSAT` + `fee_percent_ppm` parts-per-million) is withheld at mint time to cover the routing cost of eventually paying the note back out on melt. Two failure modes:
1. **Rounding down:** `_mint_fee_msat` must round the fee **up** to the nearest whole sat (`-(-fee_msat // 1000) * 1000`). Rounding down (or leaving fractional-msat precision) shorts the mint a sat versus the estimate a wallet derives from the advertised `Mint fees:` metadata, and at scale the rounding gap accumulates.
2. **Melt fee budget too tight:** `_melt_fee_limit_msat` must be `max(round(amount * 0.005), 5000, _mint_fee_msat(amount))` — never less than the fee actually collected at mint, or a fee-free/low-flat-fee mint's melts start failing to route large notes.
3. **Split/merge fee arithmetic allowing inflation:** `test_poc_fee_conservation.py` and `test_poc_fee_loop.py` hunt exactly this. The conservation identity is `paid_in == outstanding + melted_out + fees_collected - refunds`. A split collects `base_fee` once but produces 2 notes; a merge refunds `(n-1) * base_fee`. The load-bearing fact: *every minted note has provably "paid" at least one base fee* (the grid sweep `test_fee_arithmetic_grid_never_attacker_favorable` proves `net <= gross - base_fee`), so the merge refund can never exceed what splits+mints collected. Get the rounding or the refund formula wrong and `attacker_gain > 0` — the holder prints sats.

**Why it happens:**
- Integer msat arithmetic with `//` rounds toward zero (down) — the wrong direction for a fee the mint keeps.
- `base_fee_msat` is sub-sat (e.g. `1`): the mint fee rounds up to 1000 msat, but splits/merges use the raw 1 msat — the rounding gap is always kept by the mint (correct), but a port that "simplifies" by rounding the split/merge fee too would hand the gap to the holder.
- Merge refund using `fee_percent_ppm` instead of only `base_fee_msat` (the ppm was already withheld once at mint; refunding it again is inflation).
- `change_amount == 0` accepted as a valid note (a zero-value note is never valid regardless of settings — `test_dust_split_edges` pins `change < 1` rejection).

**How to avoid:**
- `_mint_fee_msat`: `fee = base_fee_msat + (amount * fee_percent_ppm) // 1_000_000; return -(-fee // 1000) * 1000` (round up to whole sat).
- Split: `change = total - amount - base_fee_msat` (base_fee only, never ppm); reject `change_before_fee < base_fee_msat` (negative) and `change_amount < 1` (zero note).
- Merge: `refund = (n - 1) * base_fee_msat`; `merged = total + refund`.
- `_melt_fee_limit_msat`: `max(round(amount * 0.005), 5000, _mint_fee_msat(amount))`.
- `_min_sendable_msat` walks amount up until `amount - _mint_fee_msat(amount) >= min_mint_msat` (fee-aware floor advertisement); `max_mintable_msat = max_sendable_msat - _mint_fee_msat(max_sendable_msat)` (fee-aware ceiling). Advertising the raw floor when it doesn't clear the net floor means paying the advertised minimum always bounces.
- Port the `Ledger` conservation harness from `test_poc_fee_conservation.py` and assert `attacker_gain <= 0` after every cycle.

**Warning signs:**
- `//` instead of `-(-x // y)` for fee rounding.
- A split/merge test that doesn't assert the conservation identity.
- `minSendable` advertised at the raw `min_sendable_msat` when a fee is configured (will bounce).
- A merge refund that includes `fee_percent_ppm`.
- `change == 0` accepted.

**Phase to address:**
Phase 1 (fee math + `/p/cb` + `/w/cb` split/merge). Locked by `test_poc_fee_conservation.py` (all tests) and `test_poc_fee_loop.py`.

---

### Pitfall 6: The verify observer race — `/verify` handing the preimage to any invoice holder

**What goes wrong (FUNDS-LOSS theft):**
`/verify/{payment_hash}` is unauthenticated by design (LUD-21). The `payment_hash` travels inside the invoice itself, so *anyone* who sees an unpaid mint invoice (a QR on a public page, a screenshot, a forwarded payment request, wallet logs) can poll `/verify` and, the moment it settles, grab the `preimage` — which, for a no-comment mint, **is the note's entire bearer secret** — and rotate the note onto their own secret. First rotater wins. `test_poc_verify_race.py` documents the theft chain. The fix has two halves:
1. **`VERIFY_ENABLED=false` is a real off-switch (404, not just a hidden URL).** Because the preimage is a bearer secret here, an operator who doesn't want it served needs the endpoint to *not exist*, not just be unadvertised — a hidden URL is still hit by anyone who knows the shape.
2. **Verify is gated on comment protection.** For a mint that *skipped* `comment` (LUD-25 comment protection), the preimage IS the note's secret, so `/verify` must refuse (404) for that payment_hash entirely. For a mint that *used* `comment`, the preimage redeems nothing (the note is keyed by the WALLET-held `secret` behind `comment`), so serving it is harmless.

**Why it happens:**
- Implementing `VERIFY_ENABLED` as "hide the `verify` field from the `/p/cb` response" but still serving the endpoint — the URL shape is guessable (`/verify/{payment_hash}`), so hiding the advertisement doesn't close the hole.
- Not gating on `mint_uses_comment`: serving verify for every mint's payment_hash, including no-comment ones where the preimage is the whole secret.
- Caching the preimage locally (compounds Pitfall 3) — verify must fetch it live.
- Forgetting that the melt-direction verify is *harmless* (the melt preimage keys no note; the funding notes are already burned) — over-correcting by 404'ing melt verify too breaks LUD-25 melt proof-of-payment.

**How to avoid:**
- `verify_invoice`: if `not settings.verify_enabled` → 404. Then look up `mint_pr(payment_hash)`; if found and `not mint_uses_comment(payment_hash)` → 404 (no-comment mint: preimage is the secret). If found and comment was used → serve (`settled`, live-fetched `preimage`, `pr`). Then look up `melt_pr(payment_hash)`; if found → serve unconditionally (melt preimage is harmless). Else 404.
- `get_pay_callback`: advertise `verify` only when `verify_enabled AND comment_hash is not None`.
- `VERIFY_ENABLED=false` → the route itself 404s (raise `HTTPException(404)`), not just omitted from responses.
- Preimage always fetched live from the funding source (`_mint_preimage`/`_melt_preimage`), never cached.

**Warning signs:**
- `VERIFY_ENABLED=false` still serving `/verify` (just without the advertisement).
- Verify serving a no-comment mint's preimage.
- A cached preimage anywhere in the verify path.
- Melt verify 404'd when it should be served.

**Phase to address:**
Phase 1 (verify endpoint + comment protection gating). Locked by `test_poc_verify_race.py` (all five tests) and `test_surface_hunter_verification.py`.

---

### Pitfall 7: Duplicate-melt / collision-griefing — burning a note against an already-used payment hash

**What goes wrong (FUNDS-LOSS, silent capture by the mint):**
Funding sources dedupe outgoing payments by payment hash — cln's `xpay` rejects with "This invoice has already been paid," lnd replays the prior payment's status for a repeated hash. A second melt into the same `pr` (a merchant handing the same invoice to two buyers, or a retry of a failed melt with the same invoice) would be confirmed against the *first* payment (`is_payment_complete` True) and `finalize_melt` would burn the second note **with no new funds leaving the node**: the melter's value destroyed, silently captured by the mint. `test_poc_duplicate_melt.py` pins this. The fix: reject any `pr` whose payment hash is already in the `melts` table, synchronously, before any reservation.

The collision-griefing variant (`test_poc_a1_collision_griefing.py`) is the HIGH-severity sibling: pre-fix, `swap`'s INSERT collision-checked only the `notes` table, never `mints` — so a rotate/split/merge with `h`/`h2` = a victim's *pending* mint invoice payment_hash (visible in the victim's BOLT11 `pr`) planted a squatter note under that id. The victim's `/w` then returned a valid, mint-*signed* withdrawRequest for the squatter's dust amount (silent value substitution), and once the squatter was spent, `settle_mint`'s INSERT PK-collided with the kept row and rolled back forever — the paid mint could never materialize, `/verify` 500'd permanently, all for the price of one dust note. The fix: `swap` rejects any new note id present in `mints` (pending OR settled) with the generic safe reason, in the same transaction (atomic — nothing burned).

**Why it happens:**
- Duplicate melt: it *feels* like "the funding source will reject a duplicate," but the rejection is per-backend and inconsistent (lnd replays status, cln errors) — and even when it errors, the *confirmation* check sees the first payment as complete. The only safe place to guard is locally, before reservation.
- Collision griefing: `notes.id` is a PRIMARY KEY, so a collision with an existing *note* rolls back — but a collision with a *pending mint's* payment_hash (not yet a note row) doesn't, because `mints` wasn't checked. The squatter note shadows the future mint.

**How to avoid:**
- Melt branch: `if decoded.has_payment_hash and notes.melt_pr(decoded.payment_hash) is not None: reject "Invoice already used by an earlier melt - use a fresh one."` *Before* `mark_pending`. Also reject `pr` naming an invoice this mint issued itself (`notes.mint_pr(...) is not None`) — paying it back to self is inconsistent across backends.
- `swap`: for each new note id, check `SELECT 1 FROM mints WHERE payment_hash = ?` *in addition to* the `notes` INSERT; collision → `ValueError("Invalid or already spent k1.")` (generic, safe message — never reveal which table collided). All inside one transaction so a collision burns nothing.
- `record_melt` is unconditional (writes the `melts` row even for a melt that later fails) — so the duplicate guard catches retries of failed melts too. Trade-off (pinned by `test_failed_melt_retry_needs_a_fresh_invoice`): retrying a genuinely failed melt needs a fresh invoice; BOLT-11 invoices are single-use anyway.
- `create_mint` with `comment_hash`: collision-check against both `notes.id` and `mints.comment_hash` (same atomic pattern).

**Warning signs:**
- A melt path that calls `pay_invoice` before checking `melt_pr`/`mint_pr` locally.
- A `swap` that only collision-checks the `notes` table (the pre-fix A1 bug).
- A collision error message that reveals which table collided (info leak to an attacker probing ids).
- No test for the duplicate-melt or A1 squat scenario.

**Phase to address:**
Phase 1 (melt branch + swap). Locked by `test_poc_duplicate_melt.py` and `test_poc_a1_collision_griefing.py` (all tests).

---

### Pitfall 8: Losing settlement-confirmation fidelity through LNbits' `pay_invoice` abstraction

**What goes wrong (FUNDS-LOSS — same class as Pitfall 1, but port-specific):**
`lnurl-mint`'s `node.py` exposes `pay_invoice` (returns `PaymentResult(preimage, fee_msat)` or raises), `is_payment_complete` (returns `True`/`False`/`None` or raises — the **tristate** is the whole point: `None`/raise = "can't tell yet," never False), and `payment_preimage`/`invoice_preimage` (live lookups). The confirm-before-burn discipline depends on the **tristate** semantics of `is_payment_complete`: a hodl HTLC held open must *raise*, not resolve to False.

LNbits' `pay_invoice` service (`lnbits.core.services.payments.pay_invoice`) returns a `Payment` object with `status` (`PaymentState.PENDING`/`SUCCESS`/`FAILED`), `preimage`, `fee`. It raises `PaymentError` on failure. The underlying `Wallet.get_payment_status` returns a `PaymentStatus` with `paid: bool | None` (`None` = pending). **The risk:** the service-level `pay_invoice` may block until terminal and raise `PaymentError(status="failed")` for both genuine no-route failures *and* ambiguous/hodl cases — collapsing the tristate into a boolean "failed." If the port treats `PaymentError` as "confirmed not paid" and restores, it reintroduces the double-payout bug from Pitfall 1. The port must, on any `pay_invoice` raise, independently call `check_payment_status`/`get_payment_status` and treat `paid is None` (pending) as "leave the note pending," not "restore."

**Why it happens:**
LNbits' payment service is designed for the common "pay and know it worked" flow, not for the "pay, then carefully distinguish three outcomes before touching a bearer asset" flow. The `PaymentError.status` field (`"failed"`/`"pending"`) is a hint, but the authoritative tristate is `PaymentStatus.paid` from `get_payment_status`. A naive port reads `PaymentError` → restore, and loses the hodl-invoice safety.

**How to avoid:**
- On `pay_invoice` success → `finalize_melt` + `mark_melt_settled` (use `payment.fee_msat` for the log; LNbits does expose the fee on success).
- On `pay_invoice` raise (`PaymentError` or any) → call `check_payment_status(payment_hash)` (or `get_payment_status`):
  - `paid is True` → `finalize_melt` (the raise was a false alarm / race; payment actually went through).
  - `paid is False` → `restore` (genuinely never went out).
  - `paid is None` (pending) → **leave pending**, log to error.log.
  - raises → **leave pending**, log to error.log.
- Preserve the `_confirm_payment` retry-with-backoff for the `paid is None`/raise case.
- For verify's `_mint_preimage`/`_melt_preimage`: LNbits exposes preimage on `Payment.preimage` (success) and via `get_payment_status`'s `preimage` field — fetch live, never cache.
- **Verify the tristate empirically** during research against the actual funding source in `~/lnbits` (VoidWallet won't exercise it; test against a real or fake backend that returns `paid=None`).

**Warning signs:**
- A port where `except PaymentError: notes.restore(...)` with no `check_payment_status` follow-up.
- Treating `PaymentError(status="pending")` as failure.
- No test modeling a hodl/ambiguous outcome against the LNbits payment service.
- `is_payment_complete`'s tristate collapsed to a bool anywhere in the melt path.

**Phase to address:**
Phase 1 (melt path) — this is the LNbits-specific instantiation of Pitfall 1, and the single hardest port translation. Research spike first (confirm `PaymentStatus.paid` tristate behavior against the real `~/lnbits` funding source), then implement. Locked by porting `test_melt_restore_double_payout_poc.py` against LNbits payment mocks that return `paid=None`.

---

### Pitfall 9: The module-level lock → LNbits `Database` transaction boundary (atomicity loss)

**What goes wrong (FUNDS-LOSS / double-spend / double-mint):**
`lnurl-mint`'s `NoteStore` does every burn+mint operation under `with self._lock, self.conn:` — a single `threading.Lock` plus a single sqlite connection = one atomic transaction. `swap` burns N notes and mints M notes in one transaction; `settle_mint` does `UPDATE mints SET minted=1 WHERE minted=0` (compare-and-set) + `INSERT notes` in one transaction; `mark_pending` reserves N notes in one transaction. The lock + compare-and-set together defeat the settle race (`test_poc_a2_settle_race.py`: N concurrent first-resolvers, exactly one wins the `rowcount==1`, the rest get `None`).

LNbits' `Database` (`lnbits/db.py`) is different in a way that silently breaks this:
- `Database.connect()` acquires an **`asyncio.Lock`** (`self.lock`) and yields a single connection; `db.fetchone`/`db.execute`/`db.insert`/`db.update` each call `async with self.connect()` — **each call is its own lock acquisition + its own connection + its own transaction.**
- Calling `db.execute(...)` three times in a row is **three separate transactions** with the lock released between them. The atomic burn+mint of `swap` becomes non-atomic: a concurrent request can interleave between the burn and the mint, double-spending or double-minting.
- The `asyncio.Lock` serializes all DB access across the whole event loop (similar to the module-level lock in single-process), but it does **not** hold across multiple `await`s unless you keep one `async with db.connect() as conn:` block open and run every statement through that one `conn`.

**Why it happens:**
The LNbits `Database` convenience methods (`db.execute`, `db.fetchone`) are designed for single-statement CRUD, not multi-statement atomic state machines. The giftcards `crud.py` uses them one-at-a-time because giftcard redemption is a single `UPDATE ... WHERE status='active'` compare-and-set — atomic by itself. The lnurlmint `swap`/`settle_mint`/`mark_pending` are *multi-statement* atomic operations; using `db.execute` per statement loses the transaction.

**How to avoid:**
- For every multi-statement atomic operation (`swap`, `settle_mint`, `mark_pending`, `finalize_melt`, `restore`, `create_mint` with collision check), open **one** `async with db.connect() as conn:` block and run all statements via `conn.execute(...)` / `conn.fetchone(...)` (the `Connection` methods), NOT `db.execute(...)`. This holds the `asyncio.Lock` and the DB connection/transaction for the whole operation.
- Keep the **compare-and-set** pattern (`UPDATE ... WHERE minted = 0 AND ...` with `rowcount == 1` check) — it's the real protection against the settle race, and it works inside a single transaction. The lock alone is not enough on Postgres (concurrent transactions can both see `minted=0` without `SELECT ... FOR UPDATE` or a compare-and-set).
- For Postgres, consider `SELECT ... FOR UPDATE` on the note rows in `swap`/`mark_pending` to lock the rows for the duration of the transaction (the `asyncio.Lock` serializes within one process, but the LNbits `Database` abstraction supports Postgres, and a multi-process LNbits deployment would defeat a pure in-process lock — the compare-and-set is what survives that).
- The `_in_flight_melts` registry stays in-process (an `asyncio.Lock`-guarded `dict`), NOT in the DB (see Pitfall 2).
- Do NOT add a second `threading.Lock` — LNbits is async-single-loop; an `asyncio.Lock` is the right primitive, and mixing them risks deadlock.

**Warning signs:**
- `swap` implemented as a sequence of `await db.execute(...)` calls (each its own transaction).
- A compare-and-set that checks `rowcount` but runs the check and the subsequent INSERT in different `db.connect()` blocks.
- Tests passing under SQLite (where the global `asyncio.Lock` masks non-atomicity) but failing under Postgres.
- No test racing concurrent `settle_melt` / `swap` calls on one event loop (the A2 PoC shape).

**Phase to address:**
Phase 1 (DB layer / `NoteStore` port). The transaction discipline must be designed when the schema and CRUD are written; retrofitting it is a data migration with outstanding notes. Locked by porting `test_poc_a2_settle_race.py` (HTTP race + DB-layer race) and `test_poc_a3_mark_pending.py`.

---

### Pitfall 10: Cross-wallet note leakage — a missing `WHERE wallet_id = ?` lets a user spend another wallet's notes

**What goes wrong (FUNDS-LOSS / cross-tenant theft):**
The port is per-wallet multi-tenant: each LNbits wallet owns its mint row + notes. If any query that resolves/spends a note misses the `wallet_id` filter, wallet A can spend wallet B's notes by presenting B's `k1`. The note's `id` is `sha256(k1)` — globally unique-looking, so a query `SELECT amount_msat FROM notes WHERE id = ? AND spent = 0` *appears* safe (only the right note matches), but the moment a query joins or filters by mint config, or a "list outstanding notes" management endpoint forgets the wallet filter, B's notes leak to A. The giftcards `crud.py` is explicit about this: every query carries `WHERE wallet = :wallet` "so cross-wallet leakage is impossible (T-03-15)."

**Why it happens:**
- A note's `id` is a hash, not obviously wallet-scoped, so it's tempting to query by `id` alone.
- A management "list my notes" endpoint written as `SELECT * FROM notes` (forgetting the wallet join through the mint row).
- A melt/rotate that looks up the note by `id` and doesn't verify the note belongs to the caller's wallet — but for bearer notes the *caller is anyone holding `k1`*, so the wallet filter is about *which mint issued the note*, not *who is calling*. The note row must carry a `wallet_id` (FK to the mint's owning wallet), and every note lookup that returns value/spends must scope by it so wallet A's `k1` cannot redeem against wallet B's mint even if A somehow obtained B's `k1` (it shouldn't be able to, but defense-in-depth).
- Actually — the bearer model means *anyone* with the `k1` can spend it; the wallet_id filter is about preventing the *mint operator* (wallet A) from accidentally servicing or listing wallet B's notes through A's management UI / endpoints, and about keeping the note tables isolated per-mint. The spend itself is bearer. The leak risk is in the *management* and *reconcile* paths, and in ensuring wallet A's mint endpoints never touch wallet B's note rows.

**How to avoid:**
- Every note row carries `wallet_id` (the owning mint's wallet). Every management/list/reconcile query includes `WHERE wallet_id = :wallet_id`. Follow the giftcards pattern: `get_cards_by_wallet_filtered` always has `WHERE wallet = :wallet` "ALWAYS present so cross-wallet leakage is impossible."
- The public LNURL endpoints (`/w`, `/w/cb`, `/p/cb`, `/verify`) resolve notes by `id`/`payment_hash` but the note row's `wallet_id` determines which mint's funding source pays the melt — never cross streams. A melt of wallet A's note must call `pay_invoice(wallet_id=A, ...)`, never B's.
- The reconcile task iterates pending notes per-wallet (or per-mint) and uses each mint's own funding source.
- Parameterized queries everywhere (giftcards uses `:wallet` named params) — no string interpolation of `wallet_id`.
- Test: create notes under wallet A, then query wallet B's management endpoint, assert B sees zero notes.

**Warning signs:**
- A `SELECT ... FROM notes` without a `WHERE wallet_id = ?` in any management/list/reconcile path.
- A melt that calls `pay_invoice` with a hardcoded or wrong `wallet_id`.
- A reconcile task that uses one global funding source instead of per-mint.
- No cross-wallet isolation test.

**Phase to address:**
Phase 1 (schema: `wallet_id` on every note/mint/melt row) + Phase 2 (management endpoints). Locked by a cross-wallet isolation test in the ported suite.

---

### Pitfall 11: Offline verification signing (`signmessage`) unavailable through LNbits' funding node abstraction

**What goes wrong (SILENT FEATURE LOSS — not funds-loss, but a spec regression):**
LUD-25 offline verification requires the mint to sign each rotated/split/merged note's `(h, amount)` with its funding node's identity key via `signmessage` (lnd's `/v1/signmessage`, cln's `signmessage`), producing a recoverable `sig`/`sig2` that a holder verifies offline against `mintPubkey`. `lnurl-mint`'s `signing.py` calls `node.sign_message`, which hits the backend RPC directly. **LNbits' `Wallet` abstract base (`lnbits/wallets/base.py`) does NOT expose `signmessage`** — no abstract method, no implementation in `lndgrpc.py`/`lndrest.py`/`clnrest.py`/`corelightningrest.py` (grep confirms `signmessage` appears only in the generated `lnd_grpc_files/lightning_pb2_grpc.py`, never in the wallet abstraction). The README warns about this exact silent failure mode for scoped-too-narrow macaroons: "a scoped-too-narrow macaroon shows up as every note silently missing its signature rather than an obvious error."

If the port can't reach `signmessage`, `sign_note` returns `None` (it never raises — offline verification is optional and must not block a rotate/split/merge), and every note silently lacks `sig`/`sig2`. Holders can't verify issuer offline. The mint still works; the spec feature is just gone, invisibly.

**Why it happens:**
LNbits abstracts Lightning per-wallet but only exposes the operations LNbits itself needs (pay/invoice/status/hold). `signmessage` isn't among them. There's no LNbits-native API to sign an arbitrary message with the funding node's identity key.

**How to avoid:**
- **Research spike first:** determine whether the LNbits `Wallet` instance for the active funding source can be reached to call `signmessage` directly (e.g. reach into the lnd gRPC stub's `SignMessage`, or the clnrest `signmessage` RPC), or whether a new method must be added to the wallet abstraction (forbidden by the no-new-dependencies / don't-modify-core rule for an extension).
- If `signmessage` is unreachable through the abstraction, the extension must either:
  1. Document offline verification as unavailable for v1 (omit `mintPubkey`/`sig`/`sig2`, matching the "no funding source" fallback), OR
  2. Make a direct backend call *outside* the LNbits abstraction (re-introducing a sliver of `node.py`'s direct REST/gRPC), accepting that this only works for the configured backend and breaks the "idiomatic LNbits" goal, OR
  3. Use a separate signing key the mint operator configures (deviates from spec — `mintPubkey` should be the funding node's identity).
- Whatever the path, `sign_note` **never raises** — a signing failure logs a warning (`sign_note: could not sign via ... funding source: ...`) and returns `None`, so rotate/split/merge still succeed. The warning is the only detection surface; surface it prominently in the management UI ("offline verification: signing unavailable — check node permissions").
- `coincurve` (for `verify_note`'s `PublicKey.from_signature_and_message`) IS available transitively in LNbits (via `bolt11` and `pyln`), so the *verification* side is fine; only the *signing* RPC is the problem.

**Warning signs:**
- Every rotated/split/merged note missing `sig`/`sig2` in responses.
- No log line for `sign_note` failures (the warning is the only signal).
- An extension that modifies LNbits core `Wallet` to add `signmessage` (breaks the extension contract).
- A `sign_note` that raises instead of returning `None` (would break rotate/split/merge).

**Phase to address:**
Research phase (spike: can `signmessage` be reached?). If yes → Phase 1 (signing). If no → Phase 1 implements the `None` fallback + Phase 2 management UI surfaces the warning. The decision must be made before Phase 1 design because it affects whether `mintPubkey` is advertised at all.

---

### Pitfall 12: `.well-known/lnurlp` route conflict with LNbits' built-in `lnurlp` extension

**What goes wrong (UNREACHABLE payRequest — convenience, not funds-loss):**
`lnurl-mint` serves `GET /.well-known/lnurlp/{username}` as its sole payRequest entry point. LNbits' built-in `lnurlp` extension **already owns that path**: `lnurlp_redirect_paths` (`lnbits/extensions/lnurlp/__init__.py`) registers a redirect `/.well-known/lnurlp` → `/api/v1/well-known`, and `ExtensionsRedirectMiddleware` rewrites the path *before* it reaches any extension router. If `lnurlmint` registers its own `/.well-known/lnurlp/{username}` route, the middleware rewrites the request to `/lnurlp/api/v1/well-known/{username}` and the mint's handler is never hit — the payRequest is unreachable, and the mint's Lightning Address resolves to the lnurlp extension's generic payRequest (which knows nothing about `withdrawLink`/minting). The mint is invisible to LUD-16 address resolution.

**Why it happens:**
The standalone app owns the whole host, so `/.well-known/lnurlp` is natural. In LNbits, that path is a shared, middleware-redirected namespace owned by an existing extension. Two extensions cannot both serve it.

**How to avoid:**
- **Do NOT register `/.well-known/lnurlp/{username}` in lnurlmint.** PROJECT.md already scopes this out: "Lightning Address resolution is delegated to LNbits' existing `lnurlp` extension to avoid route conflicts."
- Integrate with LNbits' lnurlp extension: the mint's payRequest must be advertised *through* the lnurlp address system. Investigate during research whether lnurlp supports a `withdrawLink` extension field or a custom payRequest hook per-address, or whether lnurlmint must register its own LUD-16 addresses in lnurlp's DB with a callback that points to the mint's `/p/cb`.
- The mint's `/p/cb`, `/w`, `/w/cb`, `/verify` live under the extension's own prefix (`/lnurlmint/...`) — but LNURL callbacks must be absolute URLs the wallet can reach, so the callback URLs are built from `public_base_url` + the extension prefix, not `/.well-known`.
- Verify the integration end-to-end: a LUD-16 address lookup returns a payRequest whose `withdrawLink` points at the mint's `/w`, and paying the callback's invoice mints a note.

**Warning signs:**
- A `@router.get("/.well-known/lnurlp/{username}")` in lnurlmint (will be shadowed by the middleware).
- LUD-16 address resolution returning a generic lnurlp payRequest with no `withdrawLink`.
- Callback URLs built from `request.url_for` (Host-header-spoofable) instead of `public_base_url` (the standalone code is explicit about this — `config.public_base_url` is built from settings, not `req.url_for`).

**Phase to address:**
Research phase (lnurlp integration shape) + Phase 1 (endpoint registration under the extension prefix). The integration must be validated before the public one-pager can advertise a Lightning Address.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Using `db.execute` per-statement instead of one `db.connect()` block for `swap`/`settle_mint` | Less code, matches giftcards CRUD style | Non-atomic burn+mint → double-spend/double-mint (Pitfall 9, FUNDS-LOSS) | **Never** for multi-statement state machines |
| Treating `PaymentError` as "confirmed not paid" and restoring | Simpler melt path, one less RPC | Hodl-invoice double-payout (Pitfall 1/8, FUNDS-LOSS) | **Never** |
| Caching the preimage in the DB for verify | Faster verify, no per-call node hit | DB leak = every note spendable (Pitfall 3, FUNDS-LOSS) | **Never** |
| `VERIFY_ENABLED=false` only hides the advertisement, still serves | Less code | Observer race still exploitable by anyone who knows the URL shape (Pitfall 6, FUNDS-LOSS) | **Never** |
| Skipping `signmessage`, silently omitting `sig` | Rotate/split/merge still work | Offline verification feature invisibly gone; holders can't verify issuer | MVP only, with management-UI warning + docs |
| Not porting the PoC tests | Faster ship | Every regression above lands undetected | **Never** — PROJECT.md makes the suite non-negotiable |
| Single global funding source for all wallets | Simpler reconcile | Cross-wallet melt pays from wrong wallet (Pitfall 10) | **Never** — per-wallet is the model |
| `//` fee rounding (down) | One character less | Mint shorted a sat per mint at scale (Pitfall 5) | **Never** |
| `threading.Lock` alongside `asyncio.Lock` | Familiar from standalone | Deadlock risk in async context | **Never** — use `asyncio.Lock` only |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| LNbits `pay_invoice` service | Treating `PaymentError` as definitive failure; restoring immediately | On raise, call `check_payment_status`; `paid is None` → leave pending, `paid is False` → restore, `paid is True` → finalize (Pitfall 8) |
| LNbits `Database` (SQLite/Postgres) | Sequential `db.execute` calls expecting atomicity | One `async with db.connect() as conn:` block per atomic op; compare-and-set + `rowcount` check (Pitfall 9) |
| LNbits `lnurlp` extension | Registering `/.well-known/lnurlp` in lnurlmint | Delegate to lnurlp; mint endpoints under `/lnurlmint/` prefix; integrate via lnurlp's address system (Pitfall 12) |
| LNbits `Wallet` abstraction | Assuming `signmessage` is exposed | It is NOT in the abstract base; research spike required, fallback to `None` + warning (Pitfall 11) |
| LNbits `create_permanent_unique_task` | Reconcile task that doesn't skip in-flight melts | Preserve `_in_flight_melts` registry; reconcile skips live hashes (Pitfall 2) |
| LNbits `public_base_url` / Tor | Building callback URLs from `request.url_for` (Host-header-spoofable) | Build from settings/`public_base_url`, Tor-aware (`ONION_URL` substitution) — standalone code is explicit on this |
| `bolt11` decode | Assuming `decoded.amount_msat` is never None | Validate amount present before melt (standalone does this in the callback) |
| `coincurve` | Assuming it's not in LNbits | It IS present transitively (via `bolt11`/`pyln`) — usable for `verify_note` |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Reconcile retrying every pending note with full backoff at boot | Multi-minute boot with several stuck notes | `reconcile_pending_melts` passes `delays=()` (single attempt per note); next boot is the retry (standalone design) | 10+ stranded notes |
| `_min_sendable_msat` walk not bounded | 100% CPU spin if `fee_percent_ppm` ≥ 100% | Cap the walk at 100k iterations → loud error (standalone does this); validate `fee_percent_ppm` well below 100% in config | Misconfigured fee |
| `cached_fetch_node_info` caching failures | "Unreachable" for an hour after a momentary outage | Never cache a failed fetch (standalone) | Node restart |
| Per-call live preimage fetch in verify | Node RPC on every verify poll | Acceptable (spec mandates live, never cached); rate-limit if abused | High verify poll volume |
| `asyncio.Lock` global DB lock contention | All DB ops serialized | Acceptable for v1 scale; compare-and-set is the real safety, not the lock | High concurrency on one mint |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Persisting raw `k1`/preimage anywhere | DB leak = all notes spendable (FUNDS-LOSS) | Store only `sha256(k1)`; preimage fetched live, never cached (Pitfall 3) |
| Logging `?k1=` query strings | Log leak = note spendable | Disable/scrub access logs on `/w`/`/w/cb`/`/verify` (standalone disables uvicorn access log entirely) |
| `/w` advertising a pending note | Sell-during-melt scam (FUNDS-LOSS) | `note_pending` check + `"pending"` reason on `/w` (Pitfall 4) |
| Verify serving no-comment mint preimage | Observer-race theft (FUNDS-LOSS) | Gate on `mint_uses_comment`; 404 for no-comment mints (Pitfall 6) |
| `VERIFY_ENABLED=false` still serving | Same theft, just unadvertised | Route 404s entirely when off (Pitfall 6) |
| Echoing backend error text on the wire | Info leak (funding source details, hashes) | `log_internal_error` returns a reference id; backend text stays in logs |
| Callback URLs from `request.url_for` | Host-header spoofing redirects callbacks | Build from `public_base_url` setting (Tor-aware) |
| Missing `WHERE wallet_id = ?` | Cross-wallet note leakage (FUNDS-LOSS) | Every management/reconcile query scoped by `wallet_id` (Pitfall 10) |
| Collision error revealing which table | Probing oracle for note/mint ids | Generic `"Invalid or already spent k1."` for all collision types (Pitfall 7) |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| No warning when offline verification signing is unavailable | Holders can't verify notes; operator doesn't know | Management UI surfaces "signing unavailable — check node permissions" prominently (Pitfall 11) |
| `minSendable` advertised below the net-of-fee floor | Paying the advertised minimum always bounces | Fee-aware `_min_sendable_msat` walk (Pitfall 5) |
| Melt failure invisible to the wallet | Wallet waits for a payment that never comes; note reappears silently | Per spec: melt failure only observable as the note becoming spendable again; document this, don't try to report back through the callback |
| Stranded pending notes with no operator visibility | Funds frozen forever, no error.log entry | `log_internal_error` for unconfirmable melts (durable record); management UI lists pending notes |
| `Mint fees:` metadata omitted when fee is 0 | Wallets assume fee-free (correct) but no negative signal | Omit entirely when both `base_fee_msat` and `fee_percent_ppm` are 0 (spec: omission = fee-free) |

## "Looks Done But Isn't" Checklist

- [ ] **Melt path:** Often missing the `PaymentError → check_payment_status → paid is None → leave pending` branch (Pitfall 1/8) — verify with a hodl/ambiguous mock
- [ ] **Reconcile:** Often missing the `_in_flight_melts` skip (Pitfall 2) — verify reconcile doesn't call the funding source for a live melt
- [ ] **`/w` pending check:** Often missing the `note_pending` rejection (Pitfall 4) — verify `/w` returns `"pending"` during a melt window
- [ ] **Verify gating:** Often missing the `mint_uses_comment` check (Pitfall 6) — verify no-comment mints 404 on `/verify`
- [ ] **`VERIFY_ENABLED=false`:** Often only hides the ad, still serves (Pitfall 6) — verify the route 404s
- [ ] **`swap` collision check:** Often checks only `notes`, not `mints` (Pitfall 7) — verify a squat on a pending mint's payment_hash is rejected
- [ ] **Duplicate-melt guard:** Often missing the `melt_pr` check (Pitfall 7) — verify a second melt into the same `pr` is rejected
- [ ] **Fee rounding:** Often `//` (down) instead of `-(-x // 1000) * 1000` (up) (Pitfall 5) — verify `_mint_fee_msat` rounds up
- [ ] **DB atomicity:** Often `db.execute` per statement (Pitfall 9) — verify `swap`/`settle_mint` run in one `db.connect()` block
- [ ] **`wallet_id` scoping:** Often missing on management/reconcile queries (Pitfall 10) — verify cross-wallet isolation test passes
- [ ] **Preimage caching:** Often cached for verify speed (Pitfall 3) — verify preimage is fetched live every call
- [ ] **Access logging:** Often left on (Pitfall 3) — verify `?k1=` doesn't appear in logs
- [ ] **`signmessage`:** Often silently returns `None` with no warning (Pitfall 11) — verify the log warning fires and the management UI shows it
- [ ] **Callback URL base:** Often built from `request.url_for` (Pitfall 12) — verify built from `public_base_url`, Tor-aware
- [ ] **`.well-known/lnurlp`:** Often registered by the extension (Pitfall 12) — verify the mint doesn't register it; lnurlp integration works

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Notes burned on a guess (Pitfall 1) | HIGH | Cannot auto-recover burned notes; operator must audit funding source for the payment and manually recredit. Prevent only. |
| Double-spend via reconcile (Pitfall 2) | HIGH | Funds already gone; audit node payments vs note state. Prevent only. |
| Raw secret persisted in DB (Pitfall 3) | HIGH | Rotate every outstanding note (force rotate on next spend); drop the column; audit logs/DB access. |
| Pending note sold during melt (Pitfall 4) | MEDIUM | Operator refunds the buyer out of band; the note is gone (melt settled). Prevent only. |
| Fee rounding shorting the mint (Pitfall 5) | LOW | Fix the rounding; the accumulated shortfall is small (sats). No note migration. |
| Verify theft (Pitfall 6) | HIGH | Set `VERIFY_ENABLED=false` immediately; affected notes already rotated by the thief. |
| Duplicate-melt capture (Pitfall 7) | HIGH | The captured value is held by the mint operator; operator must audit and recredit. Prevent only. |
| `PaymentError` tristate collapse (Pitfall 8) | HIGH | Same as Pitfall 1. Prevent only. |
| Non-atomic `swap` (Pitfall 9) | HIGH | Stop the service; audit for double-minted/double-spent rows; reconcile note totals vs payments. Prevent only. |
| Cross-wallet leakage (Pitfall 10) | HIGH | Audit which wallet queried which notes; recredit victims. Add the missing `wallet_id` filters. |
| `signmessage` unavailable (Pitfall 11) | LOW | No funds at risk; fix the signing path or document the feature as unavailable. |
| `.well-known` conflict (Pitfall 12) | LOW | No funds at risk; fix the route registration; payRequest becomes reachable. |

## Pitfall-to-Phase Mapping

| Pitfall | Class | Prevention Phase | Verification |
|---------|-------|------------------|--------------|
| 1. Confirm-before-burn | (A) FUNDS-LOSS | Phase 1 (melt) | `test_melt_restore_double_payout_poc.py` passes (all 3 variants) |
| 2. In-flight melt tracking | (A) FUNDS-LOSS | Phase 1 (melt+reconcile) | `test_poc_reconcile_inflight_race.py` passes (all 3) |
| 3. Store-hashes-not-secrets | (A) FUNDS-LOSS | Phase 1 (schema+verify) | No preimage column; verify fetches live; no `k1` in logs |
| 4. `/w` pending rejection | (A) FUNDS-LOSS scam | Phase 1 (withdraw) | `test_poc_f2_pending_info_leak.py` passes (both) |
| 5. Fee conservation | (A) FUNDS-LOSS | Phase 1 (fee math) | `test_poc_fee_conservation.py` + `test_poc_fee_loop.py` pass; `attacker_gain <= 0` |
| 6. Verify observer race | (A) FUNDS-LOSS theft | Phase 1 (verify+comment) | `test_poc_verify_race.py` passes (all 5); `VERIFY_ENABLED=false` 404s |
| 7. Duplicate-melt / collision griefing | (A) FUNDS-LOSS | Phase 1 (melt+swap) | `test_poc_duplicate_melt.py` + `test_poc_a1_collision_griefing.py` pass |
| 8. `pay_invoice` tristate fidelity | (B) port FUNDS-LOSS | Research + Phase 1 (melt) | Hodl/ambiguous mock against LNbits `pay_invoice` → note stays pending |
| 9. DB transaction atomicity | (B) port FUNDS-LOSS | Phase 1 (DB layer) | `test_poc_a2_settle_race.py` + `test_poc_a3_mark_pending.py` pass under Postgres |
| 10. Cross-wallet isolation | (C) multi-tenancy FUNDS-LOSS | Phase 1 (schema) + Phase 2 (mgmt UI) | Cross-wallet isolation test: wallet B sees zero of wallet A's notes |
| 11. `signmessage` unavailable | (B) port feature-loss | Research + Phase 1 (signing) | `sig`/`sig2` present when signing works; warning surfaced when not |
| 12. `.well-known/lnurlp` conflict | (B) port convenience | Research + Phase 1 (routes) | LUD-16 lookup returns mint payRequest with `withdrawLink` |

## Sources

- `lnurl-mint/README.md` — security model: confirm-before-burn, store-hashes-not-secrets, verify observer race, comment protection, fee rounding, single-process rule, macaroon/rune scoping, `signmessage` silent-failure warning
- `lnurl-mint/lnurl_mint/router.py` — `_melt_pay` (confirm-before-burn), `_in_flight_melts` (in-flight tracking), `reconcile_pending_melts` (skip live), `_mint_fee_msat` (round up), `verify_invoice` (comment gating, 404 off-switch), `get_withdraw` (pending rejection), melt branch (duplicate-melt/collision guards)
- `lnurl-mint/lnurl_mint/db.py` — `NoteStore`: lock + compare-and-set, `mark_pending`/`finalize_melt`/`restore`/`swap`/`settle_mint` state machine, store-hashes schema
- `lnurl-mint/lnurl_mint/signing.py` — `sign_note` (never raises, returns None), `coincurve` verify
- `lnurl-mint/tests/test_poc_double_melt.py` — duplicate-melt guard PoC
- `lnurl-mint/tests/test_poc_a2_settle_race.py` — settle-mint race PoC (lock + compare-and-set)
- `lnurl-mint/tests/test_poc_verify_race.py` — verify observer race PoC (comment gating, 404 off-switch)
- `lnurl-mint/tests/test_poc_reconcile_inflight_race.py` — reconcile vs in-flight double-spend PoC
- `lnurl-mint/tests/test_poc_fee_conservation.py` — fee conservation / inflation hunt PoC
- `lnurl-mint/tests/test_poc_a1_collision_griefing.py` — pending-mint note-id squat PoC (HIGH)
- `lnurl-mint/tests/test_poc_f2_pending_info_leak.py` — pending-note info leak / sell-during-melt PoC
- `lnurl-mint/tests/test_melt_restore_double_payout_poc.py` — hodl-invoice double-payout PoC (tristate)
- `lnurl-mint/tests/test_bearer_threat_suite_poc.py` — bearer threat scorecard (T1-T11)
- `giftcards/crud.py` — wallet isolation pattern (`WHERE wallet = :wallet` ALWAYS present), parameterized queries, `Database("ext_giftcards")`
- `giftcards/services.py` — `pay_invoice(wallet_id=..., payment_request=...)`, `PaymentState.SUCCESS` check
- `giftcards/__init__.py` — `create_permanent_unique_task` lifecycle
- `lnbits/lnbits/db.py` — `Database.connect()` uses `asyncio.Lock`; per-call `fetchone`/`execute` are separate transactions
- `lnbits/lnbits/core/services/payments.py` — `pay_invoice` returns `Payment`; `PaymentError`; `check_payment_status` returns `PaymentStatus(paid: bool|None)`
- `lnbits/lnbits/wallets/base.py` — `Wallet` abstract base: NO `signmessage` method; `PaymentStatus.paid` tristate
- `lnbits/lnbits/extensions/lnurlp/__init__.py` + `views_lnurl.py` — `lnurlp_redirect_paths` owns `/.well-known/lnurlp`
- `lnbits/lnbits/middleware.py` — `ExtensionsRedirectMiddleware` rewrites `.well-known` before extension routers
- `lnbits/pyproject.toml` — `bech32`, `pyqrcode`, `bolt11` present; `coincurve` transitively via `bolt11`/`pyln`; NO `signmessage` in wallet abstraction

---
*Pitfalls research for: LUD-25 lnurlcash mint ported to an LNbits extension*
*Researched: 2026-08-28*
