"""TEST-03: the tristate settlement contract (SEC-01 — confirm-before-burn).

The single highest-risk port detail. ``pay_invoice`` raising does NOT mean
no HTLC was sent — the payment may still be held open (a hodl invoice or a
lost response). ``_confirm_payment`` must call ``check_transaction_status``
to distinguish the tristate before touching the note:

* ``paid=True``  → finalize (burn the note for good)
* ``paid=False`` → restore  (confirmed not paid — no HTLC)
* ``paid=None``  → leave pending (unconfirmable — an HTLC may be in flight)

A naive ``except PaymentError: restore`` would fail the ``ambiguous`` test:
it would restore a note whose payment is actually still going out, letting
the holder spend it twice (a funds-loss bug). The ``HodlNode`` fixture
models ``paid=None`` via ``PaymentPendingStatus`` while ``pending_hodl`` is
non-empty.

Three scenarios are pinned:

1. ``ambiguous``    — pay raises (status="pending"), check returns paid=None
   → the note is LEFT PENDING (not restored). After ``settle_hodl_payments``
   (reality catches up, paid=True), reconcile finalizes (burn).
2. ``benign_failed`` — pay raises (status="failed"), no HTLC, check returns
   paid=False → the note is RESTORED.
3. ``failed``       — pay raises (status="failed"), HTLC stays live, check
   returns paid=None → the note is LEFT PENDING (the most insidious tristate
   case: a terminal raise that looks like a definitive failure but the
   payment is actually still in flight).
"""

import pytest

import bolt11

from lnurlmint.crud import get_note, mark_pending
from lnurlmint.services import (
    _melt_pay,
    _track_melt_start,
    _melt_in_flight,
    reconcile_pending_melts,
)
from lnurlmint.tests.conftest import fake_invoice, mint_note

VALUE = 50_000


async def _start_melt(note_id, pr, decoded, mint):
    """Reserve the note and register the melt as in-flight (pre-_melt_pay)."""
    await mark_pending([note_id], decoded.payment_hash, mint.id)
    await _track_melt_start(decoded.payment_hash)


@pytest.mark.anyio
async def test_melt_restore_double_payout_ambiguous_leaves_pending(hodl_node, db_setup):
    """paid=None (ambiguous raise) leaves the note pending, NOT restored."""
    k1, note_id, mint = await mint_note(hodl_node, VALUE)
    hodl_node.pay_mode = "ambiguous"

    pr = fake_invoice(VALUE, "aa" * 32)
    decoded = bolt11.decode(pr)
    await _start_melt(note_id, pr, decoded, mint)

    # pay_invoice raises (ambiguous); check_transaction_status returns
    # paid=None (PaymentPendingStatus) while pending_hodl is non-empty.
    await _melt_pay([note_id], pr, decoded, mint)

    # CRITICAL: the note is still pending — paid=None must NOT restore.
    note = await get_note(note_id, mint.id)
    assert note.pending is True, "paid=None must leave the note pending"
    assert note.spent is False, "the note must not be spent"

    # The in-flight registry is cleared in _melt_pay's finally block.
    assert not await _melt_in_flight(decoded.payment_hash)


@pytest.mark.anyio
async def test_melt_restore_double_payout_settle_after_hodl(hodl_node, db_setup):
    """After the hodl settles (reality catches up), reconcile finalizes."""
    k1, note_id, mint = await mint_note(hodl_node, VALUE)
    hodl_node.pay_mode = "ambiguous"

    pr = fake_invoice(VALUE, "bb" * 32)
    decoded = bolt11.decode(pr)
    await _start_melt(note_id, pr, decoded, mint)
    await _melt_pay([note_id], pr, decoded, mint)

    note = await get_note(note_id, mint.id)
    assert note.pending is True, "still pending before reality catches up"

    # Reality catches up — the live HTLC completes.
    hodl_node.settle_hodl_payments()
    # Reconcile now sees paid=True (PaymentSuccessStatus) and finalizes.
    await reconcile_pending_melts()

    note = await get_note(note_id, mint.id)
    assert note.spent is True, "note must be spent after confirmed payment"
    assert note.pending is False


@pytest.mark.anyio
async def test_melt_restore_double_payout_benign_failed_restores(hodl_node, db_setup):
    """A benign failure (no route, no HTLC) → paid=False → note restored."""
    k1, note_id, mint = await mint_note(hodl_node, VALUE)
    hodl_node.pay_mode = "benign_failed"

    pr = fake_invoice(VALUE, "cc" * 32)
    decoded = bolt11.decode(pr)
    await _start_melt(note_id, pr, decoded, mint)

    # pay_invoice raises (failed); no HTLC, so check_transaction_status
    # returns paid=False (PaymentFailedStatus) → _confirm_payment restores.
    await _melt_pay([note_id], pr, decoded, mint)

    note = await get_note(note_id, mint.id)
    assert note.pending is False, "benign failure must restore the note"
    assert note.spent is False, "the note must not be spent"


@pytest.mark.anyio
async def test_melt_restore_double_payout_failed_with_htlc_leaves_pending(
    hodl_node, db_setup
):
    """A terminal FAILED raise with a live HTLC → paid=None → leave pending.

    The most insidious tristate case: ``pay_invoice`` raises with
    ``status="failed"`` (looks like a definitive failure), but the HTLC
    stays live (``pending_hodl`` non-empty), so
    ``check_transaction_status`` returns ``paid=None`` (can't confirm
    either way). A naive ``except PaymentError: restore`` would restore
    the note and enable a double-spend. ``_confirm_payment`` must leave
    the note pending.
    """
    k1, note_id, mint = await mint_note(hodl_node, VALUE)
    hodl_node.pay_mode = "failed"

    pr = fake_invoice(VALUE, "dd" * 32)
    decoded = bolt11.decode(pr)
    await _start_melt(note_id, pr, decoded, mint)

    # pay_invoice raises (status="failed"); the HTLC stays live, so
    # check_transaction_status returns paid=None (PaymentPendingStatus)
    # while pending_hodl is non-empty.
    await _melt_pay([note_id], pr, decoded, mint)

    # CRITICAL: the note is still pending — a terminal raise with a live
    # HTLC must NOT restore (would enable a double-spend).
    note = await get_note(note_id, mint.id)
    assert note.pending is True, "failed with live HTLC must leave pending"
    assert note.spent is False, "the note must not be spent"

    # The in-flight registry is cleared in _melt_pay's finally block.
    assert not await _melt_in_flight(decoded.payment_hash)
