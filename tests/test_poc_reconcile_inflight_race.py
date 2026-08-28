"""TEST-04: reconcile skips in-flight melts (SEC-03 — no double-spend).

Before the ``pay_invoice`` RPC lands, the node may truthfully report "not
found" (lnd 404 / cln empty ``listpays``) for a payment that is still being
sent. An innocent reconcile that called ``check_transaction_status`` on
such a payment would see ``paid=False`` and ``restore`` the note while the
HTLC is still going out — a double-spend.

The fix is the in-flight registry: a melt is registered
(``_track_melt_start``) immediately after ``mark_pending`` succeeds and
BEFORE ``_melt_pay`` is scheduled, and reconcile skips any payment hash
that is still in-flight (``_melt_in_flight``). The entry is cleared in
``_melt_pay``'s ``finally`` block.

The ``InFlightNode`` fixture models this window: ``pay_invoice`` sets
``pay_started`` then blocks on ``pay_release``; ``check_transaction_status``
reports ``paid=False`` for an unregistered payment. The test runs
``_melt_pay`` in the background, waits for ``pay_started``, then runs
reconcile — which must SKIP the in-flight melt and leave the note pending.
Releasing the payment lets ``_melt_pay`` complete and finalize the note.
"""

import asyncio

import pytest
import bolt11

from lnurlmint.crud import get_note, mark_pending
from lnurlmint.services import (
    _melt_pay,
    _track_melt_start,
    reconcile_pending_melts,
)
from lnurlmint.tests.conftest import fake_invoice, mint_note

VALUE = 50_000


@pytest.mark.anyio
async def test_poc_reconcile_inflight_race(inflight_node, db_setup):
    """Reconcile skips in-flight melts — the note is not restored while live."""
    k1, note_id, mint = await mint_note(inflight_node, VALUE)

    pr = fake_invoice(VALUE, "dd" * 32)
    decoded = bolt11.decode(pr)

    # Reserve the note and register the melt as in-flight.
    await mark_pending([note_id], decoded.payment_hash, mint.id)
    await _track_melt_start(decoded.payment_hash)

    # Start _melt_pay in the background — it sets pay_started and blocks
    # on pay_release (the payment is now in-flight).
    melt_task = asyncio.create_task(_melt_pay([note_id], pr, decoded, mint))
    await inflight_node.pay_started.wait()

    # Run reconcile while the payment is in-flight. InFlightNode would
    # report paid=False (lnd 404 for an unregistered payment), BUT
    # _melt_in_flight must return True so reconcile skips this hash.
    await reconcile_pending_melts()

    # CRITICAL: the note is still pending — reconcile did NOT restore it.
    note = await get_note(note_id, mint.id)
    assert note.pending is True, "reconcile must skip in-flight melts"
    assert note.spent is False, "the note must not be spent"

    # Release the payment — let _melt_pay complete (pay_invoice succeeds).
    inflight_node.pay_release.set()
    await melt_task

    # Now the note is finalized (spent) — the payment succeeded.
    note = await get_note(note_id, mint.id)
    assert note.spent is True, "note must be spent after successful payment"
    assert note.pending is False
