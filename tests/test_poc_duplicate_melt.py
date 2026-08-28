"""TEST-01: a note melted twice is rejected (pending state prevents second melt).

The confirm-before-burn state machine reserves a note (pending=1) via
``mark_pending`` before sending the melt payment. A second melt attempt
targeting an already-pending note is rejected with
``{"status":"ERROR","reason":"pending"}`` — the /w/cb endpoint checks
``note.pending`` (and ``mark_pending`` raises ``PendingNoteError`` as a
backstop) before any second payment is attempted. This prevents a double
spend of the same bearer note.

The first melt's background ``_melt_pay`` is NOT executed here (a bare
``BackgroundTasks`` registers the task but does not run it outside FastAPI's
response machinery), so the note stays pending between the two calls —
exactly the window the guard protects.
"""

import pytest
from fastapi import BackgroundTasks
from unittest.mock import MagicMock

from lnurlmint.crud import get_note
from lnurlmint.views_lnurl import get_withdraw_callback
from lnurlmint.tests.conftest import fake_invoice, mint_note, TEST_MINT_ID

VALUE = 50_000


@pytest.mark.anyio
async def test_poc_duplicate_melt(node, db_setup):
    """A note melted twice is rejected — the second melt returns pending."""
    k1, note_id, mint = await mint_note(node, VALUE)

    # First melt — accepted; the note is reserved (pending=1).
    pr1 = fake_invoice(VALUE)
    bg1 = BackgroundTasks()
    resp1 = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg1, k1=[k1], pr=pr1
    )
    assert resp1["status"] == "OK", resp1

    note = await get_note(note_id, mint.id)
    assert note.pending is True, "note must be pending after the first melt"
    assert note.spent is False

    # Second melt of the SAME note (different invoice) — rejected because
    # the note is already pending. No second payment is attempted.
    pr2 = fake_invoice(VALUE)
    bg2 = BackgroundTasks()
    resp2 = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg2, k1=[k1], pr=pr2
    )
    assert resp2["status"] == "ERROR"
    assert resp2["reason"] == "pending"

    # The note is still pending, not spent and not restored.
    note = await get_note(note_id, mint.id)
    assert note.pending is True
    assert note.spent is False
