"""PoC for A3 (auth-data lane): "NoteStore.mark_pending validates only
k1s[0]; later k1s go unvalidated, so restore()/finalize_melt() can hit ids
that were never outstanding." **FALSIFIED** on two independent grounds.

Ported from the source's ``test_poc_a3_mark_pending.py``, adapting to
LNbits async fixtures: crud functions called directly (not via the
source's ``notes`` singleton), per-test DB isolation.

1. The premise does not match the code. mark_pending (crud.py) is two
   loops: the FIRST validates every note_id (each must exist, be unspent,
   and not be pending) before the SECOND writes anything. A garbage id
   at ANY position aborts the whole reservation; nothing is marked.
2. Unreachable via HTTP anyway: the only caller is the melt path
   (views_lnurl.py), and melts reject multiple k1s outright - note_ids
   is always a 1-element list.

The residual sharp edge is real but not a vulnerability: finalize_melt
and restore are blind UPDATEs with no rowcount check (crud.py) - they
trust their caller. The control test below shows finalize_melt WILL
silently burn a never-reserved outstanding note if handed one directly,
i.e. mark_pending's validation is the only line of defense.
"""

from hashlib import sha256
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import (
    PendingNoteError,
    db,
    finalize_melt,
    get_note,
    mark_pending,
    restore,
)
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    fake_invoice,
    fresh_secret,
    mint_note,
)
from lnurlmint.views_lnurl import get_withdraw_callback

GARBAGE_ID = "ff" * 32  # well-formed note id that was never minted


def _note_id(k1: str) -> str:
    return sha256(bytes.fromhex(k1)).hexdigest()


async def _is_pending(note_id: str) -> bool:
    note = await get_note(note_id, TEST_MINT_ID)
    return bool(note and note.pending)


@pytest.mark.anyio
async def test_a3_http_melt_rejects_multiple_k1s_before_any_reservation(
    node, db_setup
):
    """Ground 2: a melt can never even reach mark_pending with >1 k1."""
    k1a, _, _ = await mint_note(node, 10_000)
    k1b, _, _ = await mint_note(node, 10_000)
    pr = fake_invoice(20_000)
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1a, k1b], pr=pr,
    )
    assert resp["status"] == "ERROR", resp
    assert "cannot be combined" in resp["reason"], resp
    # both notes remain fully spendable - nothing was reserved
    assert await _is_pending(_note_id(k1a)) is False
    assert await _is_pending(_note_id(k1b)) is False
    _, h = fresh_secret()
    rotate = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1a], h=h,
    )
    assert rotate["status"] == "OK", rotate


@pytest.mark.anyio
async def test_a3_mark_pending_validates_every_id_at_any_position(
    node, db_setup
):
    """Ground 1: a garbage id anywhere in the list aborts the whole
    reservation - and marks nothing, not even the valid ids."""
    real_k1, real_id, _ = await mint_note(node, 10_000)
    ph = sha256(b"a3-test-payment").hexdigest()

    # garbage LAST: the described bug shape would reserve real_id and skip
    # validating the rest - instead the whole call aborts
    with pytest.raises(ValueError, match="Invalid or already spent k1"):
        await mark_pending([real_id, GARBAGE_ID], ph, TEST_MINT_ID)
    assert await _is_pending(real_id) is False  # NOT silently reserved

    # garbage FIRST: same abort
    with pytest.raises(ValueError, match="Invalid or already spent k1"):
        await mark_pending([GARBAGE_ID, real_id], ph, TEST_MINT_ID)
    assert await _is_pending(real_id) is False

    # a SPENT id anywhere also aborts (rotate a note away, then try it)
    spent_k1, spent_id, _ = await mint_note(node, 10_000)
    _, h = fresh_secret()
    assert (
        await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[spent_k1], h=h,
        )
    )["status"] == "OK"
    with pytest.raises(ValueError, match="Invalid or already spent k1"):
        await mark_pending([real_id, spent_id], ph, TEST_MINT_ID)
    assert await _is_pending(real_id) is False

    # and an already-pending id anywhere aborts with PendingNoteError,
    # leaving the earlier ids untouched
    other_k1, other_id, _ = await mint_note(node, 10_000)
    await mark_pending([other_id], ph, TEST_MINT_ID)  # valid single reservation
    with pytest.raises(PendingNoteError):
        await mark_pending([real_id, other_id], ph, TEST_MINT_ID)
    assert await _is_pending(real_id) is False
    await restore([other_id], TEST_MINT_ID)  # cleanup: release the valid reservation

    # sanity: the real note still rotates fine - nothing above reserved it
    _, h2 = fresh_secret()
    assert (
        await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[real_k1], h=h2,
        )
    )["status"] == "OK"


@pytest.mark.anyio
async def test_a3_finalize_and_restore_on_never_reserved_ids_are_noops_for_unknown_ids(
    db_setup,
):
    """The blind-UPDATE tail of the candidate: finalize/restore don't verify
    prior state - shown here to be harmless for unknown ids (0 rows match),
    with the control that proves WHY mark_pending's validation matters."""
    # unknown ids: both are silent no-ops, no row appears, nothing is spent
    await finalize_melt([GARBAGE_ID], TEST_MINT_ID)
    assert await get_note(GARBAGE_ID, TEST_MINT_ID) is None
    await restore([GARBAGE_ID], TEST_MINT_ID)
    assert await get_note(GARBAGE_ID, TEST_MINT_ID) is None

    # control: finalize_melt WILL burn a never-reserved *outstanding* note if
    # handed one directly - it has no defense of its own. Unreachable today
    # (every caller passes ids mark_pending accepted; every id there was
    # validated), so this is a code-fragility note, not a vulnerability.
    k1_secret, _ = fresh_secret()
    outstanding_id = _note_id(k1_secret)
    await db.execute(
        "INSERT INTO lnurlmint.notes (id, mint_id, amount_msat, spent, pending) "
        "VALUES (:id, :mid, :amount, 0, 0)",
        {"id": outstanding_id, "mid": TEST_MINT_ID, "amount": 10_000},
    )
    await finalize_melt([outstanding_id], TEST_MINT_ID)
    note = await get_note(outstanding_id, TEST_MINT_ID)
    assert note is not None
    assert note.spent is True  # burned without ever being pending
