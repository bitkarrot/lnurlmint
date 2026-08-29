"""Regression tests from the surface lane of the 2026-08-17 security review
(originally PoCs, flipped to pin the fixed behavior).

Ported from the source's ``test_surface_hunter_verification.py``,
adapting to LNbits async fixtures: endpoint functions called directly
(not via TestClient), per-test DB isolation, FakeNode with controllable
tristate behaviour.

- P3/F-1: rotate with h == a pending mint's payment_hash is rejected by
  the swap guard; the victim's mint materializes normally.
- P1/F-3: FIXED (LUD-25 comment protection) - /verify used to hand a
  settled mint's preimage to anyone holding the payment_hash when
  VERIFY_ENABLED=true. Now SERVICE refuses verify outright for any mint
  that skipped ``comment``, and for one that used it the disclosed
  preimage isn't the note's secret to begin with.
- P2/F-5: N/A for LNbits (RPC census / caching is a source-only concern;
  the port has no cached_fetch_node_info). NOT ported.
- P6/F-4: fee_percent_ppm beyond the validated bound can no longer reach
  _min_sendable_msat through Settings at all, and the function's own
  iteration cap converts even a post-construction mutation into a raised
  error instead of a hang.
"""

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
from lnurlmint.services import _min_sendable_msat, _try_settle_mint
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


def _verify_body(resp) -> dict:
    if hasattr(resp, "status_code"):
        return json.loads(resp.body)
    return resp.dict()


@pytest.mark.anyio
async def test_p3_rotate_onto_pending_mint_is_rejected(node, db_setup):
    # attacker owns a note and knows a victim's pending mint payment_hash
    # (learnable from the victim's invoice pr, which embeds it)
    attacker_k1, _, mint = await mint_note(node, PLANT_AMOUNT)

    # victim requests a mint invoice but has not paid it yet
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
    assert await get_pending_mint_record(victim_ph, mint.id) is not None

    # the squat is rejected atomically - nothing planted, nothing burned
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[attacker_k1], h=victim_ph,
    )
    assert resp == {"status": "ERROR", "reason": "Invalid or already spent k1."}, resp
    assert await get_note(victim_ph, mint.id) is None
    attacker_id = sha256(bytes.fromhex(attacker_k1)).hexdigest()
    assert (await get_note(attacker_id, mint.id)).amount_msat == PLANT_AMOUNT

    # victim pays -> their mint materializes for the full amount
    node.settled.add(victim_ph)
    note_id = sha256(bytes.fromhex(victim_preimage)).hexdigest()
    settled = await _try_settle_mint(note_id, mint)
    assert settled
    note = await get_note(victim_ph, mint.id)
    assert note is not None
    assert note.amount_msat == VALUE


@pytest.mark.anyio
async def test_p1_verify_no_longer_hands_out_the_no_comment_fallback_secret(
    node, db_setup
):
    # FIXED by LUD-25 comment protection: a mint that skipped ``comment``
    # falls back to k1=preimage, and that preimage IS the note's entire
    # spend secret - so SERVICE now refuses verify for it outright, even
    # with VERIFY_ENABLED on, instead of handing it to anyone who ever
    # saw the invoice.
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    assert "verify" not in resp, resp
    victim_pr = resp["pr"]
    victim_ph = bolt11.decode(victim_pr).payment_hash
    node.settled.add(victim_ph)

    r = await verify_invoice(TEST_MINT_ID, victim_ph)
    _assert_404(r)

    # the victim's own preimage, learned the ordinary way (their own
    # Lightning payment), still redeems the note normally - the fallback
    # note itself is unaffected, only the remote-disclosure endpoint is
    # closed
    victim_preimage = node.preimages[victim_ph]
    _, victim_h = fresh_secret()
    rotate = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[victim_preimage], h=victim_h,
    )
    assert rotate["status"] == "OK", rotate


@pytest.mark.anyio
async def test_p1b_verify_is_harmless_once_comment_protection_is_used(
    node, db_setup
):
    # the complementary case: a WALLET that DOES use LUD-25 comment
    # protection gets verify served, but the disclosed preimage is no
    # longer the note's spend secret (the WALLET-held ``secret`` behind
    # ``comment`` is), so an observer stealing it from /verify gets nothing
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    secret, comment_hash = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment_hash
    )
    assert resp.get("verify"), resp
    victim_pr = resp["pr"]
    victim_ph = bolt11.decode(victim_pr).payment_hash
    node.settled.add(victim_ph)

    # materialize the note (keyed by comment_hash, not preimage)
    await _try_settle_mint(comment_hash, await get_mint_by_id(TEST_MINT_ID))

    stolen = await verify_invoice(TEST_MINT_ID, victim_ph)
    assert not hasattr(stolen, "status_code"), stolen  # 200 -> model
    body = _verify_body(stolen)
    assert body["settled"] is True, body
    assert body["preimage"] is not None, body

    # the stolen preimage redeems nothing - it was never the note's k1
    _, attacker_h = fresh_secret()
    rotate = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[body["preimage"]], h=attacker_h,
    )
    assert rotate == {"status": "ERROR", "reason": "Invalid or already spent k1."}, rotate

    # only the WALLET-held secret does
    _, victim_h = fresh_secret()
    rotate = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[secret], h=victim_h,
    )
    assert rotate["status"] == "OK", rotate


@pytest.mark.anyio
async def test_p6_pathological_ppm_raises_instead_of_hanging(db_setup):
    # post-construction mutation bypasses pydantic (update_mint does raw
    # SQL) - the iteration cap inside _min_sendable_msat is the second
    # line of defense: a config that used to spin a worker at 100% CPU
    # forever now raises, quickly and loudly
    await update_mint(
        TEST_MINT_ID,
        TEST_WALLET,
        base_fee_msat=0,
        fee_percent_ppm=1_000_000,
        min_mint_msat=10_000,
        min_sendable_msat=10_000,
    )
    mint = await get_mint_by_id(TEST_MINT_ID)

    with pytest.raises(RuntimeError, match="minSendable walk did not terminate"):
        _min_sendable_msat(mint)

    # and a high-but-legal ppm (at the validated bound) still terminates
    await update_mint(TEST_MINT_ID, TEST_WALLET, fee_percent_ppm=100_000)
    mint = await get_mint_by_id(TEST_MINT_ID)
    assert _min_sendable_msat(mint) > 0
