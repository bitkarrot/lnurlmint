"""Regression tests from the auth-data lane of the 2026-08-17 security
review (originally PoCs, flipped to pin the fixed behavior).

Ported from the source's ``test_auth_data_hunter_poc.py``, adapting to
LNbits async fixtures: endpoint functions called directly (not via
TestClient), per-test DB isolation, FakeNode/InFlightNode with
controllable tristate behaviour.

- F1/F-3: /verify discloses a settled mint's preimage (= the bearer
  note's spend secret) to anyone holding only the payment_hash -
  embedded in the invoice itself. Post-fix this requires
  verify_enabled=True; false 404s the endpoint entirely.
- F3/F-2: GET /w on a note reserved by an in-flight melt (pending=1)
  now rejects with the spec's reason "pending" instead of reporting it
  fully withdrawable - the sell-during-melt scam's one lie.
- F4/F-1: rotating ONTO a pending mint's payment_hash is rejected by
  the swap guard (ids may never collide with ``mints_records`` rows),
  so the victim's settled mint materializes normally.
"""

import asyncio
import json
from hashlib import sha256
from unittest.mock import MagicMock

import bolt11
import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import (
    get_mint_by_id,
    get_note,
    get_pending_mint_record,
    record_mint_record,
    update_mint,
)
from lnurlmint.services import _melt_pay, _track_melt_start, _try_settle_mint
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    fake_invoice,
    fresh_secret,
    mint_note,
)
from lnurlmint.views_lnurl import (
    get_pay_callback,
    get_withdraw,
    get_withdraw_callback,
    verify_invoice,
)

VALUE = 50_000
PLANT_AMOUNT = 10_000


def _mock_request() -> MagicMock:
    req = MagicMock()
    req.base_url = "http://test/"
    return req


def _assert_404(resp) -> None:
    assert resp.status_code == 404, resp
    body = json.loads(resp.body)
    assert body == {"status": "ERROR", "reason": "Not found"}, body


@pytest.mark.anyio
async def test_f1_verify_disclosure_requires_verify_enabled(node, db_setup):
    # verify_enabled pinned False; /p/cb does not advertise verify
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=False)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    assert "verify" not in resp, resp

    victim_pr = resp["pr"]
    payment_hash = bolt11.decode(victim_pr).payment_hash  # embedded in pr, not secret
    node.settled.add(payment_hash)

    # an attacker holding only the pr (and thus the payment_hash) gets
    # nothing from the unadvertised endpoint - not even after settlement
    verify = await verify_invoice(TEST_MINT_ID, payment_hash)
    _assert_404(verify)

    # the note is the victim's to rotate, at whatever speed they like
    preimage = node.preimages[payment_hash]
    _, victim_h = fresh_secret()
    rotate = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[preimage], h=victim_h,
    )
    assert rotate["status"] == "OK", rotate
    note = await get_note(victim_h, TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE


@pytest.mark.anyio
async def test_f3_withdraw_rejects_pending_note_with_spec_reason(
    inflight_node, db_setup
):
    from lnurlmint.crud import mark_pending, record_melt

    k1, note_id, mint = await mint_note(inflight_node, 10_000)

    pr = fake_invoice(10_000)
    decoded = bolt11.decode(pr)

    # Reserve the note and register the melt as in-flight (SEC-03).
    await mark_pending([note_id], decoded.payment_hash, mint.id)
    await _track_melt_start(decoded.payment_hash)
    await record_melt(decoded.payment_hash, pr, mint.id, note_id, 10_000)

    # Start _melt_pay in the background - it blocks on pay_release
    # (the payment is now in-flight, note is pending).
    melt_task = asyncio.create_task(_melt_pay([note_id], pr, decoded, mint))
    await inflight_node.pay_started.wait()

    try:
        # the informational endpoint now tells the same truth as the
        # mutating one, with the spec's own distinct reason
        info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
        assert info == {"status": "ERROR", "reason": "pending"}, info

        _, h = fresh_secret()
        rotate = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1], h=h,
        )
        assert rotate["reason"] == "pending"
    finally:
        inflight_node.pay_release.set()
        await melt_task

    # after the melt completes, the note is spent (not pending)
    note = await get_note(note_id, mint.id)
    assert note.spent is True


@pytest.mark.anyio
async def test_f4_rotate_onto_pending_mint_rejected_victim_unharmed(
    node, db_setup
):
    attacker_k1, _, mint = await mint_note(node, PLANT_AMOUNT)

    # victim requests a mint invoice (unpaid); its pr embeds the payment_hash
    victim_payment = await node.create_invoice(
        wallet_id=mint.wallet, amount=VALUE // 1000
    )
    victim_ph = victim_payment.payment_hash
    victim_preimage = victim_payment.preimage
    await record_mint_record(
        payment_hash=victim_ph,
        mint_id=mint.id,
        pr=victim_payment.bolt11,
        amount_msat=VALUE,
    )

    # the squat attempt fails atomically - nothing planted, nothing burned
    r1 = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[attacker_k1], h=victim_ph,
    )
    assert r1 == {"status": "ERROR", "reason": "Invalid or already spent k1."}, r1
    assert await get_note(victim_ph, mint.id) is None  # no squatter row
    attacker_id = sha256(bytes.fromhex(attacker_k1)).hexdigest()
    assert (await get_note(attacker_id, mint.id)).amount_msat == PLANT_AMOUNT

    # victim pays: the mint materializes for its full value, exactly as if
    # the attack never happened
    node.settled.add(victim_ph)
    note_id = sha256(bytes.fromhex(victim_preimage)).hexdigest()
    settled = await _try_settle_mint(note_id, mint)
    assert settled
    note = await get_note(victim_ph, mint.id)
    assert note is not None
    assert note.amount_msat == VALUE
