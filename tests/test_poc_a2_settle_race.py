"""TEST-02: compare-and-set settle_mint is atomic (no double-mint).

Lazy note materialization uses a compare-and-set: ``UPDATE mints_records
SET minted=1 WHERE payment_hash=:ph AND minted=0`` then checks
``rowcount==1``. Only the winner proceeds to ``INSERT INTO notes``; a
concurrent (or repeated) call sees ``minted`` already 1, gets
``rowcount==0``, and returns ``None`` without inserting. The note id
PRIMARY KEY is a backstop. This prevents a double-mint when two settlement
polls race for the same payment hash.

The test inserts a pending mint record (minted=0) directly, then calls
``settle_mint`` twice: the first returns the amount (winner), the second
returns ``None`` (loser), and exactly one note exists.
"""

import pytest

from lnurlmint.crud import db, get_note, settle_mint
from lnurlmint.tests.conftest import TEST_MINT_ID

AMOUNT = 5000


@pytest.mark.anyio
async def test_poc_a2_settle_race(node, db_setup):
    """Two settle_mint calls for the same hash produce exactly one note."""
    payment_hash = "deadbeef" * 8
    note_id = "c0ffee" * 10 + "beef"  # hex(Q)-shaped id, 64 chars
    note_id = note_id[:64]

    # Insert a pending mint record (minted=0) directly.
    await db.execute(
        "INSERT INTO lnurlmint.mints_records "
        "(payment_hash, mint_id, pr, amount_msat, minted, note_id) "
        "VALUES (:ph, :mid, :pr, :amount, 0, :nid)",
        {
            "ph": payment_hash,
            "mid": TEST_MINT_ID,
            "pr": "lnbc...",
            "amount": AMOUNT,
            "nid": note_id,
        },
    )

    # First settle — winner: compare-and-set flips minted 0->1, inserts note.
    result1 = await settle_mint(payment_hash)
    assert result1 == AMOUNT, f"first settle should return amount, got {result1}"

    # Second settle — loser: minted is already 1, rowcount==0 -> None.
    result2 = await settle_mint(payment_hash)
    assert result2 is None, "second settle should return None (already settled)"

    # Exactly one note exists, materialized with the recorded amount under
    # the record's note_id.
    note = await get_note(note_id, TEST_MINT_ID)
    assert note is not None, "note should be materialized"
    assert note.amount_msat == AMOUNT
    assert note.spent is False
    assert note.pending is False
    assert note.locked_at > 0  # credited timestamp recorded
