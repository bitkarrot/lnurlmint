"""Regression tests for the pending-mint note-id squat (2026-08-17 review,
F-1 - the review's one HIGH finding, originally PoC A1).

Pre-fix, NoteStore.swap's INSERT collision-checked only the `notes` table,
never `mints` - so a rotate/split/merge with an output equal to a victim's
PENDING mint's note id planted a squatter note under that id. The victim's
/w then returned a valid, mint-SIGNED withdrawRequest for the squatter's
dust amount (silent value substitution), and once the squatter was spent,
settle_mint's INSERT PK-collided with the kept row and rolled back
forever - the paid mint could never materialize, /verify 500d
permanently, all for the price of one dust note.

Under the taproot protocol the same attack applies verbatim: a pending
mint's future note id is its recorded `note_id` (hex Q — visible to the
attacker in the WALLET's comment). The guard now rejects any p1/p2
output colliding with `mints_records.note_id` OR `notes.id` (both
global, since notes.id is a global PRIMARY KEY) with
OutputCollisionError ("already in use"), in the same connect block - so
the squat fails atomically (nothing burned), and the legitimate mint
materializes normally once paid. These tests pin exactly that, across
all three swap paths (rotate p1, split p1/p2, merge p1), plus the
settled-mint variant (a settled invoice's note_id stays in
`mints_records` forever, so it must reject too).
"""

from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import (
    get_note,
    get_pending_mint_record,
    record_mint_record,
)
from lnurlmint.services import _try_settle_mint
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    bearer_id,
    fresh_secret,
    k1_id,
    mint_note,
)
from lnurlmint.views_lnurl import get_withdraw_callback

VICTIM_AMOUNT = 50_000
PLANT_AMOUNT = 10_000


async def _pending_victim_mint(node) -> tuple[str, str, str]:
    """A victim mint invoice, requested but not yet paid:
    (payment_hash, k1, note_id). The victim's wallet chose comment=h —
    so `note_id` = bearer_id(h) is the future note id an attacker could
    squat (it appears in the victim's own mint request)."""
    from lnurlmint.crud import get_mint_by_id

    mint_obj = await get_mint_by_id(TEST_MINT_ID)
    secret, h = fresh_secret()
    note_id = bearer_id(h)
    payment = await node.create_invoice(
        wallet_id=mint_obj.wallet, amount=VICTIM_AMOUNT // 1000
    )
    await record_mint_record(
        payment_hash=payment.payment_hash,
        mint_id=mint_obj.id,
        pr=payment.bolt11,
        amount_msat=VICTIM_AMOUNT,
        note_id=note_id,
    )
    return payment.payment_hash, secret, note_id


async def _assert_squat_rejected(resp, attacker_k1: str) -> None:
    """The squat fails with the collision reason, atomically - the
    attacker's own note is NOT burned (the whole swap rolls back)."""
    assert resp == {"status": "ERROR", "reason": "already in use"}, resp
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    note = await get_note(k1_id(attacker_k1), mint.id)
    assert note is not None
    assert note.amount_msat == PLANT_AMOUNT


async def _assert_victim_mint_materializes(node, victim_ph: str, note_id: str) -> None:
    """After the rejected squat, the victim pays and their mint works
    exactly as if nothing happened."""
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    node.settled.add(victim_ph)
    settled = await _try_settle_mint(note_id, mint)
    assert settled, "victim mint should materialize after settlement"
    note = await get_note(note_id, mint.id)
    assert note is not None
    assert note.amount_msat == VICTIM_AMOUNT
    # The pending mint record is now settled (minted=1)
    pending = await get_pending_mint_record(note_id, mint.id)
    assert pending is None  # minted=1 → query for minted=0 returns None


@pytest.mark.anyio
async def test_rotate_squat_is_rejected_and_victim_mint_survives(node, db_setup):
    attacker_k1, _, _ = await mint_note(node, PLANT_AMOUNT)
    victim_ph, _, note_id, victim_h = await _pending_victim_mint_h(node)
    # The victim's pending mint record exists
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    pending = await get_pending_mint_record(note_id, mint.id)
    assert pending is not None
    assert pending.amount_msat == VICTIM_AMOUNT

    # the squatter names the victim's future output `h` as p1
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[attacker_k1], p1=victim_h,
    )
    await _assert_squat_rejected(resp, attacker_k1)
    # no squatter note exists under the victim's future id
    assert await get_note(note_id, mint.id) is None

    await _assert_victim_mint_materializes(node, victim_ph, note_id)


@pytest.mark.parametrize("variant", ["split_p1", "split_p2", "merge"])
@pytest.mark.anyio
async def test_split_and_merge_squats_are_rejected_identically(node, db_setup, variant: str):
    """Split (p1 and p2) and merge (p1) all reach the same swap guard."""
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    victim_ph, _, note_id, victim_h = await _pending_victim_mint_h(node)

    if variant == "split_p1":
        k1, _, _ = await mint_note(node, PLANT_AMOUNT)
        _, h2 = fresh_secret()
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1], amount=4000, p1=victim_h, p2=h2,
        )
    elif variant == "split_p2":
        k1, _, _ = await mint_note(node, PLANT_AMOUNT)
        _, h = fresh_secret()
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1], amount=4000, p1=h, p2=victim_h,
        )
    else:  # merge
        k1a, _, _ = await mint_note(node, 6000)
        k1b, _, _ = await mint_note(node, 4000)
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1a, k1b], p1=victim_h,
        )
    assert resp == {"status": "ERROR", "reason": "already in use"}, resp
    assert await get_note(note_id, mint.id) is None  # no squatter planted

    # atomic: nothing was burned - every input note is still outstanding
    if variant == "merge":
        assert (await get_note(k1_id(k1a), mint.id)).amount_msat == 6000
        assert (await get_note(k1_id(k1b), mint.id)).amount_msat == 4000
    else:
        assert (await get_note(k1_id(k1), mint.id)).amount_msat == PLANT_AMOUNT

    await _assert_victim_mint_materializes(node, victim_ph, note_id)


async def _pending_victim_mint_h(node) -> tuple[str, str, str, str]:
    """_pending_victim_mint plus the raw `h` the attacker quotes."""
    victim_ph, secret, note_id = await _pending_victim_mint(node)
    return victim_ph, secret, note_id, _h_of(secret)


def _h_of(secret: str) -> str:
    from hashlib import sha256

    return sha256(bytes.fromhex(secret)).hexdigest()


@pytest.mark.anyio
async def test_squat_on_an_already_settled_mints_id_is_also_rejected(node, db_setup):
    """The guard consults `mints_records` rows regardless of minted state:
    a settled mint's note_id remains recorded (and IS an outstanding
    note's id), so a WALLET-chosen output colliding with it must reject
    the same way."""
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    victim_k1, victim_note_id, _ = await mint_note(node, VICTIM_AMOUNT)
    note = await get_note(victim_note_id, mint.id)
    assert note is not None
    assert note.amount_msat == VICTIM_AMOUNT
    # The mint record is settled (minted=1)
    pending = await get_pending_mint_record(victim_note_id, mint.id)
    assert pending is None  # minted=1 → query for minted=0 returns None

    # the victim's bearer note's `h` collides in BOTH tables at once
    attacker_k1, _, _ = await mint_note(node, PLANT_AMOUNT)
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[attacker_k1], p1=_h_of(victim_k1),
    )
    await _assert_squat_rejected(resp, attacker_k1)
    # the victim's real note is untouched
    assert (await get_note(victim_note_id, mint.id)).amount_msat == VICTIM_AMOUNT


@pytest.mark.anyio
async def test_legitimate_ids_still_pass_the_guard(node, db_setup):
    """No false positives: fresh WALLET-generated p1/p2 (the honest flow)
    rotate, split and merge exactly as before the guard existed."""
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)

    k1, _, _ = await mint_note(node, PLANT_AMOUNT)
    _, h = fresh_secret()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], p1=h,
    )
    assert resp["status"] == "OK"
    assert (await get_note(bearer_id(h), mint.id)).amount_msat == PLANT_AMOUNT

    k1b, _, _ = await mint_note(node, 6000)
    k1c, _, _ = await mint_note(node, 4000)
    _, hm = fresh_secret()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1b, k1c], p1=hm,
    )
    assert resp["status"] == "OK"
    assert (await get_note(bearer_id(hm), mint.id)).amount_msat == 10_000

    k1d, _, _ = await mint_note(node, PLANT_AMOUNT)
    _, hs = fresh_secret()
    _, hs2 = fresh_secret()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1d], amount=4000, p1=hs, p2=hs2,
    )
    assert resp["status"] == "OK"
    assert (await get_note(bearer_id(hs), mint.id)).amount_msat == 4000
    assert (await get_note(bearer_id(hs2), mint.id)).amount_msat == PLANT_AMOUNT - 4000
