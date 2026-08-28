"""Regression tests for the pending-mint note-id squat (2026-08-17 review,
F-1 - the review's one HIGH finding, originally PoC A1).

Pre-fix, NoteStore.swap's INSERT collision-checked only the `notes` table,
never `mints` - so a rotate/split/merge with h/h2 = a victim's PENDING mint
invoice payment_hash (visible in the victim's BOLT11 pr) planted a squatter
note under that id. The victim's /w then returned a valid, mint-SIGNED
withdrawRequest for the squatter's dust amount (silent value substitution),
and once the squatter was spent, settle_mint's INSERT PK-collided with the
kept row and rolled back forever - the paid mint could never materialize,
/verify 500d permanently, all for the price of one dust note.

The fix: swap rejects any new note id present in `mints_records` (pending OR
settled) with the generic safe reason, in the same transaction - so the
squat fails atomically (nothing burned), and the legitimate mint
materializes normally once paid. These tests pin exactly that, across all
three swap paths (rotate h, split h/h2, merge h), plus the settled-mint
variant (an already-settled invoice's payment_hash stays in `mints_records`
forever, so it must reject too).

Ported from the source's test_poc_a1_collision_griefing.py, adapting to
LNbits async fixtures: endpoint functions called directly, pending victim
mints created via record_mint_record, note values read via get_note, victim
mint materialized via _try_settle_mint.
"""

from hashlib import sha256
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import (
    get_note,
    get_pending_mint_record,
    record_mint_record,
)
from lnurlmint.services import _try_settle_mint
from lnurlmint.views_lnurl import get_withdraw_callback
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    fake_invoice,
    fresh_secret,
    mint_note,
)

VICTIM_AMOUNT = 50_000
PLANT_AMOUNT = 10_000


async def _pending_victim_mint(node) -> tuple[str, str]:
    """A victim mint invoice, requested but not yet paid:
    (payment_hash, preimage_hex). Creates a pending mint record directly
    via record_mint_record (not via /p/cb, since we need the
    payment_hash as the future note id)."""
    from lnurlmint.crud import get_mint_by_id

    mint_obj = await get_mint_by_id(TEST_MINT_ID)
    payment = await node.create_invoice(
        wallet_id=mint_obj.wallet, amount=VICTIM_AMOUNT // 1000
    )
    payment_hash = payment.payment_hash
    k1 = payment.preimage
    await record_mint_record(
        payment_hash=payment_hash,
        mint_id=mint_obj.id,
        pr=payment.bolt11,
        amount_msat=VICTIM_AMOUNT,
    )
    return payment_hash, k1


async def _assert_squat_rejected(resp, attacker_k1: str) -> None:
    """The squat fails with the generic safe reason, atomically - the
    attacker's own note is NOT burned (the whole swap rolls back)."""
    assert resp == {"status": "ERROR", "reason": "Invalid or already spent k1."}, resp
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    attacker_id = sha256(bytes.fromhex(attacker_k1)).hexdigest()
    note = await get_note(attacker_id, mint.id)
    assert note is not None
    assert note.amount_msat == PLANT_AMOUNT


async def _assert_victim_mint_materializes(node, victim_ph: str, victim_k1: str) -> None:
    """After the rejected squat, the victim pays and their mint works
    exactly as if nothing happened."""
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    node.settled.add(victim_ph)
    note_id = sha256(bytes.fromhex(victim_k1)).hexdigest()
    settled = await _try_settle_mint(note_id, mint)
    assert settled, "victim mint should materialize after settlement"
    note = await get_note(victim_ph, mint.id)
    assert note is not None
    assert note.amount_msat == VICTIM_AMOUNT
    # The pending mint record is now settled (minted=1)
    pending = await get_pending_mint_record(victim_ph, mint.id)
    assert pending is None  # minted=1 → query for minted=0 returns None


@pytest.mark.anyio
async def test_rotate_squat_is_rejected_and_victim_mint_survives(node, db_setup):
    attacker_k1, _, _ = await mint_note(node, PLANT_AMOUNT)
    victim_ph, victim_k1 = await _pending_victim_mint(node)
    # The victim's pending mint record exists
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    pending = await get_pending_mint_record(victim_ph, mint.id)
    assert pending is not None
    assert pending.amount_msat == VICTIM_AMOUNT

    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[attacker_k1], h=victim_ph,
    )
    await _assert_squat_rejected(resp, attacker_k1)
    # no squatter note exists under the victim's future id
    assert await get_note(victim_ph, mint.id) is None

    await _assert_victim_mint_materializes(node, victim_ph, victim_k1)


@pytest.mark.parametrize("variant", ["split_h", "split_h2", "merge"])
@pytest.mark.anyio
async def test_split_and_merge_squats_are_rejected_identically(node, db_setup, variant: str):
    """Split (h and h2) and merge (h) all reach the same swap guard."""
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    victim_ph, victim_k1 = await _pending_victim_mint(node)

    if variant == "split_h":
        k1, _, _ = await mint_note(node, PLANT_AMOUNT)
        _, h2 = fresh_secret()
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1], amount=4000, h=victim_ph, h2=h2,
        )
    elif variant == "split_h2":
        k1, _, _ = await mint_note(node, PLANT_AMOUNT)
        _, h = fresh_secret()
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1], amount=4000, h=h, h2=victim_ph,
        )
    else:  # merge
        k1a, _, _ = await mint_note(node, 6000)
        k1b, _, _ = await mint_note(node, 4000)
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1a, k1b], h=victim_ph,
        )
    assert resp == {"status": "ERROR", "reason": "Invalid or already spent k1."}, resp
    assert await get_note(victim_ph, mint.id) is None  # no squatter planted

    # atomic: nothing was burned - every input note is still outstanding
    if variant == "merge":
        assert (await get_note(sha256(bytes.fromhex(k1a)).hexdigest(), mint.id)).amount_msat == 6000
        assert (await get_note(sha256(bytes.fromhex(k1b)).hexdigest(), mint.id)).amount_msat == 4000
    else:
        assert (await get_note(sha256(bytes.fromhex(k1)).hexdigest(), mint.id)).amount_msat == PLANT_AMOUNT

    await _assert_victim_mint_materializes(node, victim_ph, victim_k1)


@pytest.mark.anyio
async def test_squat_on_an_already_settled_mints_id_is_also_rejected(node, db_setup):
    """The guard consults `mints_records` rows regardless of minted state: a settled
    mint's payment_hash remains a note id (the note it produced), so a
    WALLET-chosen id colliding with it must reject the same way - not just
    for consistency, but because that id IS an outstanding note's id."""
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    victim_k1, victim_ph, _ = await mint_note(node, VICTIM_AMOUNT)
    # The note is materialized (mint_note settles + materializes)
    note = await get_note(victim_ph, mint.id)
    assert note is not None
    assert note.amount_msat == VICTIM_AMOUNT
    # The mint record is settled (minted=1)
    pending = await get_pending_mint_record(victim_ph, mint.id)
    assert pending is None  # minted=1 → query for minted=0 returns None

    attacker_k1, _, _ = await mint_note(node, PLANT_AMOUNT)
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[attacker_k1], h=victim_ph,
    )
    await _assert_squat_rejected(resp, attacker_k1)
    # the victim's real note is untouched
    assert (await get_note(victim_ph, mint.id)).amount_msat == VICTIM_AMOUNT


@pytest.mark.anyio
async def test_legitimate_ids_still_pass_the_guard(node, db_setup):
    """No false positives: fresh WALLET-generated h/h2 (the honest flow)
    rotate, split and merge exactly as before the guard existed."""
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)

    k1, _, _ = await mint_note(node, PLANT_AMOUNT)
    _, h = fresh_secret()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=h,
    )
    assert resp["status"] == "OK"
    assert (await get_note(h, mint.id)).amount_msat == PLANT_AMOUNT

    k1b, _, _ = await mint_note(node, 6000)
    k1c, _, _ = await mint_note(node, 4000)
    _, hm = fresh_secret()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1b, k1c], h=hm,
    )
    assert resp["status"] == "OK"
    assert (await get_note(hm, mint.id)).amount_msat == 10_000

    k1d, _, _ = await mint_note(node, PLANT_AMOUNT)
    _, hs = fresh_secret()
    _, hs2 = fresh_secret()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1d], amount=4000, h=hs, h2=hs2,
    )
    assert resp["status"] == "OK"
    assert (await get_note(hs, mint.id)).amount_msat == 4000
    assert (await get_note(hs2, mint.id)).amount_msat == PLANT_AMOUNT - 4000
