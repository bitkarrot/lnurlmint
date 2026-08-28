"""Service layer for the lnurlmint extension (Phase 2).

Fee math (ECON-01..ECON-04), lazy settlement materialization, the
in-flight melt refcount registry, and the public-base-URL helper. The
fee functions are protocol contracts — they must be preserved exactly
from the source (rounding UP, fee-aware bounds, melt fee budget). No
function here logs a spendable credential or full request URL (SEC-05).
"""

import asyncio

from loguru import logger

from lnbits.core.services.payments import (
    check_transaction_status,
    create_invoice as lnbits_create_invoice,
)

from .crud import get_pending_mint_record, settle_mint
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
    status = await check_transaction_status(mint.wallet, note_id)
    if status.success:
        net_amount = await settle_mint(note_id)
        return net_amount is not None
    return False


# ---------------------------------------------------------------------------
# In-flight melt tracking primitives (SEC-03)
#
# _track_melt_start / _track_melt_end maintain the refcount under the
# asyncio.Lock. _melt_in_flight is the skip predicate reconcile uses.
# _melt_pay is the background task stub — Plan 04 replaces it with the
# full tristate settlement (pay_invoice → check_payment_status →
# finalize/restore/leave-pending). The `finally:` block clears the
# in-flight entry even in the stub so the registry never leaks.
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
    """Background melt payment task — STUB (Plan 04 implements tristate settlement).

    Plan 04 replaces this with the full confirm-before-burn flow:
    pay_invoice → on PaymentError check_payment_status → paid=True
    finalize_melt, paid=False restore, paid=None leave pending. The
    `finally:` block clears the in-flight entry so the registry never
    leaks even in the stub (SEC-03).
    """
    try:
        logger.warning(
            f"_melt_pay stub called for note_ids={note_ids} — "
            "Plan 04 implements tristate settlement"
        )
    finally:
        if decoded.has_payment_hash:
            await _track_melt_end(decoded.payment_hash)
