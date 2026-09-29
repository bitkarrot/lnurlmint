"""Regression tests from the surface lane of the 2026-08-17 security review
(originally PoCs, flipped to pin the fixed behavior).

Ported from the source's ``test_surface_hunter_verification.py``,
adapting to LNbits async fixtures: endpoint functions called directly
(not via TestClient), per-test DB isolation, FakeNode with controllable
tristate behaviour.

- P3/F-1: rotate onto a pending mint's note_id is rejected by the swap
  guard; the victim's mint materializes normally.
- P1/F-3: /verify no longer hands a settled mint's preimage to anyone
  holding the payment_hash. For migrated no-comment records the preimage
  is still the bearer secret, so verify refuses outright; for every
  comment-protected mint the disclosed preimage isn't the note's secret
  to begin with.
- P2/F-5: N/A for LNbits (RPC census / caching is a source-only concern;
  the port has no cached_fetch_node_info). NOT ported.
- P6/F-4: fee_percent_ppm beyond the validated bound can no longer reach
  _min_sendable_msat through Settings at all, and the function's own
  iteration cap converts even a post-construction mutation into a raised
  error instead of a hang.
"""

import json
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
from lnurlmint.taproot import preimage_note
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    bearer_id,
    fresh_secret,
    k1_id,
    mint_note,
)
from lnurlmint.views_lnurl import (
    get_pay_callback,
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
    return json.loads(resp.body)


@pytest.mark.anyio
async def test_p3_rotate_onto_pending_mint_is_rejected(node, db_setup):
    # attacker owns a note and knows a victim's pending mint's note id
    # (learnable from the victim's own comment value)
    attacker_k1, _, mint = await mint_note(node, PLANT_AMOUNT)

    # victim requests a mint invoice with comment=h (unpaid)
    victim_secret, victim_h = fresh_secret()
    victim_note_id = bearer_id(victim_h)
    victim_payment = await node.create_invoice(
        wallet_id=mint.wallet, amount=VALUE // 1000
    )
    victim_ph = victim_payment.payment_hash
    await record_mint_record(
        payment_hash=victim_ph,
        mint_id=mint.id,
        pr=victim_payment.bolt11,
        amount_msat=VALUE,
        note_id=victim_note_id,
    )
    assert await get_pending_mint_record(victim_note_id, mint.id) is not None

    # the squat is rejected atomically - nothing planted, nothing burned
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[attacker_k1], p1=victim_h,
    )
    assert resp == {"status": "ERROR", "reason": "already in use"}, resp
    assert await get_note(victim_note_id, mint.id) is None
    assert (await get_note(k1_id(attacker_k1), mint.id)).amount_msat == PLANT_AMOUNT

    # victim pays -> their mint materializes for the full amount
    node.settled.add(victim_ph)
    settled = await _try_settle_mint(victim_note_id, mint)
    assert settled
    note = await get_note(victim_note_id, mint.id)
    assert note is not None
    assert note.amount_msat == VALUE


@pytest.mark.anyio
async def test_p1_verify_no_longer_hands_out_the_legacy_fallback_secret(
    node, db_setup
):
    """A migrated no-comment mint: the preimage IS still the bearer
    secret, so verify refuses it even with verify_enabled on."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    from lnurlmint.crud import db

    payment = await node.create_invoice(wallet_id=TEST_WALLET, amount=VALUE // 1000)
    victim_ph = payment.payment_hash
    note_id = preimage_note(bytes.fromhex(victim_ph))[0].hex()
    await db.execute(
        "INSERT INTO lnurlmint.mints_records "
        "(payment_hash, mint_id, pr, amount_msat, minted, note_id, "
        "comment_protected) VALUES (:ph, :mid, :pr, :amt, 0, :nid, 0)",
        {
            "ph": victim_ph,
            "mid": TEST_MINT_ID,
            "pr": payment.bolt11,
            "amt": VALUE,
            "nid": note_id,
        },
    )
    node.settled.add(victim_ph)

    _assert_404(await verify_invoice(TEST_MINT_ID, victim_ph))

    # the victim's own preimage still redeems the migrated note normally
    victim_preimage = node.preimages[victim_ph]
    _, victim_h = fresh_secret()
    rotate = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[victim_preimage], p1=victim_h,
    )
    assert rotate["status"] == "OK", rotate


@pytest.mark.anyio
async def test_p1b_verify_is_harmless_once_comment_protection_is_used(
    node, db_setup
):
    # the complementary case: a WALLET that DOES use comment protection
    # gets verify served, but the disclosed preimage is no longer the
    # note's spend secret (the WALLET-held ``secret`` behind ``comment``
    # is), so an observer stealing it from /verify gets nothing
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    secret, comment_hash = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment_hash
    )
    assert resp.get("verify"), resp
    victim_pr = resp["pr"]
    victim_ph = bolt11.decode(victim_pr).payment_hash
    node.settled.add(victim_ph)

    # materialize the note (keyed by the comment's output, not preimage)
    await _try_settle_mint(bearer_id(comment_hash), await get_mint_by_id(TEST_MINT_ID))

    stolen = await verify_invoice(TEST_MINT_ID, victim_ph)
    assert stolen.status_code == 200, stolen
    body = _verify_body(stolen)
    assert body["settled"] is True, body
    assert body["preimage"] is not None, body

    # the stolen preimage redeems nothing - it was never the note's k1
    _, attacker_h = fresh_secret()
    rotate = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[body["preimage"]], p1=attacker_h,
    )
    assert rotate["status"] == "ERROR", rotate

    # only the WALLET-held secret does
    _, victim_h = fresh_secret()
    rotate = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[secret], p1=victim_h,
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
