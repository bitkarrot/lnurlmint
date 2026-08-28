"""Service layer for the lnurlmint extension (Phase 2).

Fee math (ECON-01..ECON-04), lazy settlement materialization, the
in-flight melt refcount registry, and the public-base-URL helper. The
fee functions are protocol contracts — they must be preserved exactly
from the source (rounding UP, fee-aware bounds, melt fee budget). No
function here logs a spendable credential or full request URL (SEC-05).
"""

import asyncio
from typing import Optional

from loguru import logger

from lnbits.core.models.payments import PaymentState
from lnbits.core.services.payments import (
    check_transaction_status,
    create_invoice as lnbits_create_invoice,
    pay_invoice as lnbits_pay_invoice,
)
from lnbits.exceptions import PaymentError

from .crud import (
    finalize_melt,
    get_mint_by_id,
    get_mint_id_for_note,
    get_pending_mint_record,
    mark_melt_settled,
    pending_melts,
    restore,
    settle_mint,
)
from .models import Mint

# Retry backoff delays (seconds) for _confirm_payment (Plan 04). Tests
# monkeypatch this to () for fast execution.
_CONFIRMATION_RETRY_DELAYS_SECONDS: tuple[int, ...] = (1, 2, 4, 8, 16)

# ---------------------------------------------------------------------------
# In-flight melt registry (SEC-03 — prevents the reconcile race)
#
# A module-level refcount dict keyed by melt payment_hash. A melt is
# registered as in-flight immediately after mark_pending succeeds and
# BEFORE the background _melt_pay task is scheduled, so reconcile never
# restores a note while an HTLC is still being sent. The entry is
# cleared in _melt_pay's `finally:` block (cleared even on crash).
#
# Uses asyncio.Lock (NOT a thread-level lock) per CONTEXT.md: LNbits is
# async-native and the port's tests use asyncio.gather (not OS threads).
# All access is async/await. The registry is in-process and cleared on
# restart — stranded pending notes are resolved by reconcile on boot.
# ---------------------------------------------------------------------------
_in_flight_melts: dict[str, int] = {}
_in_flight_melts_lock = asyncio.Lock()


def _mint_fee_msat(amount_msat: int, mint: Mint) -> int:
    """Mint fee: base_fee + ppm, rounded UP to nearest whole sat (ECON-01).

    fee_msat = base_fee_msat + (amount_msat * fee_percent_ppm) // 1_000_000
    The `-(-x // 1000) * 1000` idiom is ceil-rounding to sat — the mint
    is never shorted a sat. Never use floor rounding here.
    """
    fee_msat = mint.base_fee_msat + (amount_msat * mint.fee_percent_ppm) // 1_000_000
    return -(-fee_msat // 1000) * 1000


def _min_sendable_msat(mint: Mint) -> int:
    """Fee-aware minSendable: walk up until net >= min_mint_msat (ECON-02).

    Starts at max(min_sendable_msat, min_mint_msat) and walks up by 1000
    msat until amount - mint_fee >= min_mint_msat. This guarantees that
    paying the advertised minimum always succeeds (the net note value
    meets the min_mint floor). The 100_000 iteration cap is a safety
    valve — with fee_percent_ppm <= 100_000 (10%), the walk always
    terminates quickly.
    """
    amount_msat = max(mint.min_sendable_msat, mint.min_mint_msat)
    for _ in range(100_000):
        if amount_msat - _mint_fee_msat(amount_msat, mint) >= mint.min_mint_msat:
            return amount_msat
        amount_msat += 1000
    raise RuntimeError(
        "minSendable walk did not terminate - check fee settings "
        "(fee_percent_ppm too high?)"
    )


def max_mintable_msat(mint: Mint) -> int:
    """Max note value net of fee (ECON-03).

    The max note the mint can issue = max_sendable_msat - mint_fee at
    that amount. The payRequest advertises the gross max_sendable_msat
    (what the payer pays); this function gives the net note value.
    """
    return mint.max_sendable_msat - _mint_fee_msat(mint.max_sendable_msat, mint)


def _melt_fee_limit_msat(amount_msat: int, mint: Mint) -> int:
    """Melt fee budget: max(0.5%, 5000 msat, mint_fee) (ECON-04).

    At least 0.5% of the amount, at least 5000 msat, and at least the
    mint fee — whichever is greatest. Note: LNbits' pay_invoice does
    not accept a per-payment fee limit (it uses its own fee_reserve);
    this formula is preserved for accounting/logging but not enforced
    at the LNbits payment layer (documented deviation).
    """
    return max(round(amount_msat * 0.005), 5000, _mint_fee_msat(amount_msat, mint))


def _public_base_url(request, mint: Mint) -> str:
    """Derive the public base URL for callback/withdrawLink URLs.

    If the mint has a per-mint `base_url` set (non-empty), it takes
    priority — this is the Host-header-spoof-proof path. Otherwise fall
    back to the request's base_url. Tor-aware onion substitution is
    Phase 6; for now, per-mint base_url is the override mechanism.
    """
    if mint.base_url:
        return mint.base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


async def _try_settle_mint(note_id: str, mint: Mint) -> bool:
    """Lazy settlement: materialize a note if its invoice has settled.

    Called from the /w endpoint (Plan 03) when a note isn't found in the
    DB yet — the holder's first poll after payment. Checks the pending
    mint record, then checks the transaction status live. If settled,
    calls settle_mint (compare-and-set) to materialize the note.

    Returns True if the note was materialized by this call, False
    otherwise (no pending record, not yet settled, or already settled
    by a concurrent request).
    """
    record = await get_pending_mint_record(note_id, mint.id)
    if record is None:
        return False
    # If the funding source is unreachable (connection error, timeout),
    # catch the exception and return False (settlement not confirmed,
    # try again later) instead of propagating a 500 to the /w or /w/cb
    # endpoint (W-03 — preserves the LNURL error format invariant).
    try:
        status = await check_transaction_status(mint.wallet, note_id)
    except Exception as exc:
        logger.warning(
            f"settle_mint: check_transaction_status failed for {note_id}: {exc}"
        )
        return False
    if status.success:
        net_amount = await settle_mint(note_id)
        return net_amount is not None
    return False


# ---------------------------------------------------------------------------
# Confirm-before-burn settlement (SEC-01, REC-01)
#
# _confirm_payment retries check_transaction_status with backoff to
# distinguish the tristate: paid=True (finalize), paid=False (restore),
# paid=None (leave pending). CRITICAL: it uses status.success,
# status.failed, and `status.paid is None` directly — NEVER the
# `.pending` property, which is `self.paid is not True` and thus True
# for BOTH paid=None AND paid=False (RQ7 gotcha #1). Using `.pending`
# would treat a confirmed failure as pending and retry forever.
# ---------------------------------------------------------------------------


async def _confirm_payment(
    payment_hash: str,
    wallet_id: str,
    delays: tuple[int, ...] | None = None,
) -> Optional[bool]:
    """Retry check_transaction_status with backoff. Returns True/False/None.

    True  = confirmed paid (finalize). False = confirmed not paid
    (restore). None = unconfirmable after all retries (leave pending).

    Default delays = _CONFIRMATION_RETRY_DELAYS_SECONDS (1,2,4,8,16 ~31s).
    delays=() does a single attempt with no sleep — used by reconcile
    for a single-attempt confirmation.
    """
    if delays is None:
        delays = _CONFIRMATION_RETRY_DELAYS_SECONDS
    for delay in (0, *delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            status = await check_transaction_status(wallet_id, payment_hash)
            if status.success:
                return True
            if status.failed:
                return False
            # status.paid is None → still pending, retry.
            # CRITICAL: do NOT use the `.pending` property (True for
            # both None and False) — it would treat confirmed failure
            # as pending.
            if status.paid is None:
                continue
            # paid is False but status.failed didn't catch — defensive.
            return False
        except Exception as exc:
            logger.warning(
                f"confirm payment {payment_hash}: attempt failed, retrying: {exc}"
            )
    return None


# ---------------------------------------------------------------------------
# In-flight melt tracking primitives (SEC-03)
#
# _track_melt_start / _track_melt_end maintain the refcount under the
# asyncio.Lock. _melt_in_flight is the skip predicate reconcile uses.
# _melt_pay is the background tristate settlement task: pay_invoice →
# on raise (or pending return) _confirm_payment → paid=True finalize,
# paid=False restore, paid=None leave pending. The `finally:` block
# always clears the in-flight entry so the registry never leaks (SEC-03).
# ---------------------------------------------------------------------------


async def _track_melt_start(payment_hash: str) -> None:
    """Register a melt as in-flight (refcount under asyncio.Lock).

    Called AFTER mark_pending succeeds and BEFORE the background
    _melt_pay task is scheduled (SEC-03). Prevents reconcile from
    restoring a note while the HTLC is still being sent.
    """
    async with _in_flight_melts_lock:
        _in_flight_melts[payment_hash] = _in_flight_melts.get(payment_hash, 0) + 1


async def _track_melt_end(payment_hash: str) -> None:
    """Clear a melt from the in-flight registry (refcount under asyncio.Lock).

    Called in _melt_pay's `finally:` block — cleared even on crash or
    exception. Decrements the refcount; removes the key when it reaches
    zero (supports multiple notes melted into the same invoice).
    """
    async with _in_flight_melts_lock:
        remaining = _in_flight_melts.get(payment_hash, 0) - 1
        if remaining > 0:
            _in_flight_melts[payment_hash] = remaining
        else:
            _in_flight_melts.pop(payment_hash, None)


async def _melt_in_flight(payment_hash: str) -> bool:
    """Skip predicate for reconcile: is a melt still being paid?

    Reconcile skips in-flight melts entirely (doesn't call
    check_payment_status) — prevents a false "not found" → restore
    while the HTLC is still being sent (TEST-04).
    """
    async with _in_flight_melts_lock:
        return payment_hash in _in_flight_melts


async def _melt_pay(note_ids: list[str], pr: str, decoded, mint: Mint) -> None:
    """Background melt payment task — tristate settlement (SEC-01, REC-01).

    Pays the melt invoice and settles the note based on the tristate
    outcome: paid=True → finalize (burn), paid=False → restore,
    paid=None → leave pending. NEVER restores on a raise alone — every
    restore path goes through _confirm_payment first (SEC-01). The
    `finally:` block always clears the in-flight registry (SEC-03).

    pay_invoice can return a pending Payment (not raise) if the backend
    times out — we check payment.status and fall through to confirmation
    if it isn't a clean success.

    No logger call includes pr, k1, or preimage (SEC-05) — only
    note_ids and payment_hash (both hashes/ids, not secrets).
    """
    payment_hash = decoded.payment_hash
    wallet_id = mint.wallet
    try:
        try:
            payment = await lnbits_pay_invoice(
                wallet_id=wallet_id,
                payment_request=pr,
                max_sat=decoded.amount_msat // 1000,
                description="lnurlcash melt",
                tag="lnurlmint",
            )
            if payment.status == PaymentState.SUCCESS.value:
                await finalize_melt(note_ids, mint.id)
                if decoded.has_payment_hash:
                    await mark_melt_settled(payment_hash)
                return
            # pay_invoice returned a pending Payment (timeout) — fall
            # through to confirmation via _confirm_payment.
            raise PaymentError("Payment timed out", status="pending")
        except PaymentError as exc:
            if not decoded.has_payment_hash:
                logger.error(
                    f"melt {note_ids}: error paying invoice, nothing to "
                    "confirm against - left pending"
                )
                return
            completed = await _confirm_payment(payment_hash, wallet_id)
            if completed is None:
                logger.error(
                    f"melt {note_ids}: could not confirm payment status "
                    "after retries - left pending"
                )
                return
            if not completed:
                logger.info(
                    f"melt {note_ids}: confirmed not paid - restoring"
                )
                await restore(note_ids, mint.id)
                return
            # Confirmed paid despite the raise — finalize.
            await finalize_melt(note_ids, mint.id)
            if decoded.has_payment_hash:
                await mark_melt_settled(payment_hash)
            return
        except Exception as exc:
            if not decoded.has_payment_hash:
                logger.error(
                    f"melt {note_ids}: unexpected error - left pending: {exc}"
                )
                return
            completed = await _confirm_payment(payment_hash, wallet_id)
            if completed is None:
                logger.error(
                    f"melt {note_ids}: could not confirm after unexpected "
                    f"error - left pending: {exc}"
                )
                return
            if not completed:
                logger.info(
                    f"melt {note_ids}: confirmed not paid after unexpected "
                    f"error - restoring"
                )
                await restore(note_ids, mint.id)
                return
            await finalize_melt(note_ids, mint.id)
            if decoded.has_payment_hash:
                await mark_melt_settled(payment_hash)
            return
    finally:
        await _track_melt_end(payment_hash)


# ---------------------------------------------------------------------------
# Background reconciliation (REC-02)
#
# reconcile_pending_melts resolves every note left pending by a crashed
# or restarted melt. It skips in-flight melts (SEC-03 — prevents
# restoring a note while the HTLC is still being sent), resolves
# stranded notes with a single-attempt confirmation (delays=()), and
# logs+leaves pending for unconfirmable melts (NEVER auto-restores —
# that would risk a double-spend if the HTLC is actually in flight).
# boot_reconcile is a one-shot at startup, guarded against exceptions.
# ---------------------------------------------------------------------------


async def reconcile_pending_melts() -> None:
    """Resolve every note left pending by a crashed/restarted melt.

    For each pending melt: skip if in-flight (SEC-03), resolve the
    wallet_id via mint_id → mints.wallet, confirm with a single attempt
    (delays=()), finalize on paid=True, restore on paid=False, and
    log+leave pending on paid=None (NEVER auto-restore unconfirmable).

    No logger call includes pr, k1, or preimage (SEC-05) — only
    note_ids and payment_hash (both hashes/ids, not secrets).
    """
    pending = await pending_melts()
    for payment_hash, note_ids in pending.items():
        if await _melt_in_flight(payment_hash):
            continue  # skip live attempts (SEC-03)
        # Resolve wallet_id for check_transaction_status.
        mint_id = await get_mint_id_for_note(note_ids[0])
        if mint_id is None:
            logger.error(
                f"reconcile: could not find mint for note {note_ids[0]}"
            )
            continue
        mint = await get_mint_by_id(mint_id)
        if mint is None:
            logger.error(
                f"reconcile: mint {mint_id} not found for note {note_ids[0]}"
            )
            continue
        completed = await _confirm_payment(payment_hash, mint.wallet, delays=())
        if completed is None:
            logger.error(
                f"reconcile: melt {note_ids} still unconfirmed - left pending"
            )
            continue  # NOT auto-restore — operator must investigate
        if completed:
            await finalize_melt(note_ids, mint.id)
            await mark_melt_settled(payment_hash)
            logger.info(
                f"reconcile: melt {note_ids} confirmed paid - finalized"
            )
        else:
            await restore(note_ids, mint.id)
            logger.info(
                f"reconcile: melt {note_ids} confirmed not paid - restored"
            )


async def boot_reconcile() -> None:
    """One-shot reconcile at boot. Guarded against exceptions.

    Runs in lnurlmint_start as an asyncio.create_task before the
    periodic reconcile task is registered — resolves stranded notes
    from a crashed process immediately on startup (REC-02). Exceptions
    are caught and logged so a boot-reconcile failure never blocks
    startup.
    """
    try:
        await reconcile_pending_melts()
    except Exception as exc:
        logger.error(f"boot reconcile failed: {exc}")
