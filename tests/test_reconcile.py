"""Reconciliation tests for stranded pending notes (REC-02).

Ported from the source's ``test_reconcile.py``, adapting to LNbits async
fixtures: ``reconcile_pending_melts`` / ``boot_reconcile`` called
directly (not via server lifespan), per-test DB isolation, FakeNode with
controllable tristate behaviour, loguru sink capture for error logging.

The source had 7 tests: 4 direct reconcile tests + 3 server-lifespan
tests (boot_reconcile, periodic monitor). This port keeps 4: one direct
test (finalize on confirmed paid), the error-logging test (adapted to
loguru sink capture), and 2 server-lifespan tests adapted to call
``reconcile_pending_melts`` / ``boot_reconcile`` directly instead of
spinning up a TestClient with a server lifespan.
"""

import bolt11
from hashlib import sha256
from unittest.mock import MagicMock

import pytest
from loguru import logger as loguru_logger

from lnurlmint.crud import get_note, mark_pending, record_melt
from lnurlmint.services import (
    _in_flight_melts,
    boot_reconcile,
    reconcile_pending_melts,
)
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    fake_invoice,
    mint_note,
)


async def _leave_a_note_pending(node, amount_msat: int = 5000) -> str:
    """Mint a note, then mark it pending with an unconfirmable melt.

    Leaves the note in the pending state (pending=1, spent=0) with a
    pending_payment_hash set, and clears the in-flight registry so
    reconcile will process the note (not skip it as in-flight).
    Returns the original k1.
    """
    k1, note_id, mint = await mint_note(node, amount_msat)
    pr = fake_invoice(amount_msat)
    decoded = bolt11.decode(pr)
    payment_hash = decoded.payment_hash

    await mark_pending([note_id], payment_hash, mint.id)
    await record_melt(payment_hash, pr, mint.id, note_id, amount_msat)
    # Clear in-flight registry so reconcile doesn't skip the note
    _in_flight_melts.clear()

    return k1


@pytest.mark.anyio
async def test_reconcile_finalizes_a_pending_note_once_confirmed_paid(
    node, db_setup
):
    k1 = await _leave_a_note_pending(node)

    node.is_payment_complete_raises = False
    node.payment_actually_completed = True
    await reconcile_pending_melts()

    # burned for good, not just still-pending
    note_id = sha256(bytes.fromhex(k1)).hexdigest()
    note = await get_note(note_id, TEST_MINT_ID)
    assert note.spent is True, "note must be spent after reconcile confirms paid"
    assert note.pending is False


@pytest.mark.anyio
async def test_reconcile_writes_still_unconfirmed_notes_to_error_log(
    node, db_setup
):
    # regression: a note interrupted mid-melt by a restart never gets a
    # chance to reach _melt_pay's own log_internal_error call - every
    # later reconcile attempt hits this same still-unconfirmed outcome,
    # so without a durable error log such a note leaves no record.
    k1 = await _leave_a_note_pending(node)

    captured = []
    sink_id = loguru_logger.add(
        lambda msg: captured.append(msg),
        level="ERROR",
        format="{message}",
    )
    try:
        await reconcile_pending_melts()
    finally:
        loguru_logger.remove(sink_id)

    assert any("still unconfirmed" in msg for msg in captured), captured
    # the note's own secret (k1) must never land in a log file
    assert not any(k1 in msg for msg in captured), captured


@pytest.mark.anyio
async def test_reconcile_resolves_a_note_that_becomes_confirmable_later(
    node, db_setup
):
    # adapted from the source's periodic-monitor test: a note whose
    # payment outcome only becomes confirmable after the first reconcile
    # attempt is picked up by a second call to reconcile_pending_melts.
    k1 = await _leave_a_note_pending(node)

    # First reconcile: still unconfirmable (is_payment_complete_raises
    # was set True by _leave_a_note_pending) - note stays pending.
    await reconcile_pending_melts()
    note_id = sha256(bytes.fromhex(k1)).hexdigest()
    note = await get_note(note_id, TEST_MINT_ID)
    assert note.pending is True, "note must stay pending when unconfirmable"

    # Now the underlying payment becomes confirmable - no further boot,
    # only a second reconcile_pending_melts call can pick this up.
    node.is_payment_complete_raises = False
    node.payment_actually_completed = True
    await reconcile_pending_melts()

    note = await get_note(note_id, TEST_MINT_ID)
    assert note.spent is True, "note must be spent after second reconcile"
    assert note.pending is False


@pytest.mark.anyio
async def test_boot_reconcile_survives_a_reconcile_failure(
    node, db_setup, monkeypatch
):
    # adapted from the source's periodic-monitor-survives-failure test:
    # an uncaught exception from reconcile_pending_melts must not prevent
    # boot_reconcile from being called again. boot_reconcile catches
    # exceptions so a failure logs an error but doesn't crash the caller.
    import lnurlmint.services as services_module

    calls = {"n": 0}
    real_reconcile = services_module.reconcile_pending_melts

    async def _flaky_reconcile():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real_reconcile()

    monkeypatch.setattr(services_module, "reconcile_pending_melts", _flaky_reconcile)

    # boot_reconcile catches the first failure (logs error, doesn't raise)
    await boot_reconcile()
    assert calls["n"] == 1

    # A second boot_reconcile call works fine - the loop kept going
    await boot_reconcile()
    assert calls["n"] == 2
