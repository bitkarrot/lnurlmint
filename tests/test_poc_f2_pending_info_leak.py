"""TEST-05: /w rejects pending notes with "pending" reason (SEC-04).

A pending note (a melt is in flight) must never be advertised as
withdrawable. The /w endpoint returns ``{"status":"ERROR","reason":
"pending"}`` for a pending note — it does NOT return a withdrawRequest
with the note's value. This prevents the sell-during-melt scam: selling a
bearer note to a third party while it is simultaneously being melted,
which would let the seller double-spend the note's value.

The test mints a note, marks it pending (simulating a melt in progress),
then calls the /w endpoint directly and asserts the error response carries
no withdrawRequest fields (callback, minWithdrawable, maxWithdrawable).
"""

import pytest
from unittest.mock import MagicMock

from lnurlmint.crud import get_note, mark_pending
from lnurlmint.views_lnurl import get_withdraw
from lnurlmint.tests.conftest import mint_note, TEST_MINT_ID

VALUE = 50_000


@pytest.mark.anyio
async def test_poc_f2_pending_info_leak(node, db_setup):
    """A pending note's /w request returns pending — no value is leaked."""
    k1, note_id, mint = await mint_note(node, VALUE)

    # Mark the note pending (simulating a melt in progress).
    await mark_pending([note_id], "f1" * 32, mint.id)

    note = await get_note(note_id, mint.id)
    assert note.pending is True

    # /w must reject the pending note with "pending" — no withdrawRequest.
    response = await get_withdraw(TEST_MINT_ID, MagicMock(), k1=k1)
    assert response["status"] == "ERROR"
    assert response["reason"] == "pending"

    # The response must NOT carry withdrawRequest fields — the note's value
    # is not advertised, so it cannot be sold while being melted.
    assert "callback" not in response
    assert "minWithdrawable" not in response
    assert "maxWithdrawable" not in response
    assert response.get("tag") != "withdrawRequest"
