"""Regression tests for the /verify preimage observer race (TEST-07).

Originally: the race was spec-shaped and remained BY DESIGN whenever verify
was on, since /verify handed a settled mint's preimage (= the no-comment
fallback note's entire spend secret) to ANYONE who knew the payment_hash
(embedded in the invoice itself), letting the first rotater win the note
regardless of who paid for it.

Under the taproot protocol the whole shape changed: no NEW mint can ever
be preimage-keyed (a wallet-chosen comment output or a branch-derived key
is mandatory), so a disclosed preimage is harmless by construction. The
only preimage-keyed notes left are MIGRATED pre-m004 records
(comment_protected=0, note_id = Q(payment_hash)) — and verify still
refuses those outright. These tests pin both halves: migrated notes keep
working with their old preimage k1, and verify never hands that preimage
back out.
"""

import json
from unittest.mock import MagicMock

import bolt11
import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import get_mint_by_id, get_note, update_mint
from lnurlmint.services import _melt_pay
from lnurlmint.taproot import preimage_note
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    bearer_id,
    fake_invoice,
    fresh_secret,
    mint_note,
)
from lnurlmint.views_lnurl import (
    get_pay_callback,
    get_withdraw_callback,
    verify_invoice,
)

VALUE = 50_000


def _mock_request() -> MagicMock:
    """A minimal Request mock for /p/cb (which calls _public_base_url)."""
    req = MagicMock()
    req.base_url = "http://test/"
    return req


def _assert_404(resp) -> None:
    """Assert a JSONResponse is a 404 with the LNURL error body."""
    assert resp.status_code == 404, resp
    body = json.loads(resp.body)
    assert body == {"status": "ERROR", "reason": "Not found"}, body


def _verify_body(resp) -> dict:
    """Extract the verify response body from a JSONResponse."""
    return json.loads(resp.body)


async def _legacy_no_comment_mint(node) -> tuple[str, str]:
    """Simulate a MIGRATED pre-m004 mint record (comment_protected=0):
    the invoice's own preimage remains the bearer secret — note_id is
    Q(payment_hash), and verify must keep refusing it forever."""
    from lnurlmint.crud import db

    payment = await node.create_invoice(wallet_id=TEST_WALLET, amount=VALUE // 1000)
    payment_hash = payment.payment_hash
    note_id = preimage_note(bytes.fromhex(payment_hash))[0].hex()
    await db.execute(
        "INSERT INTO lnurlmint.mints_records "
        "(payment_hash, mint_id, pr, amount_msat, minted, note_id, "
        "comment_protected) VALUES (:ph, :mid, :pr, :amt, 0, :nid, 0)",
        {
            "ph": payment_hash,
            "mid": TEST_MINT_ID,
            "pr": payment.bolt11,
            "amt": VALUE,
            "nid": note_id,
        },
    )
    return payment_hash, node.preimages[payment_hash]


@pytest.mark.anyio
async def test_theft_chain_closed_by_verify_refusal(node, db_setup):
    """A migrated no-comment mint: the preimage IS still the note's spend
    secret, so verify refuses to serve it at all — the attacker's first
    step (scraping the preimage from /verify) never gets off the ground.
    The note itself still redeems by preimage, exactly as a migrated
    holder expects."""
    victim_ph, preimage = await _legacy_no_comment_mint(node)
    node.settled.add(victim_ph)

    # ATTACKER (knowing only payment_hash): verify refused outright.
    _assert_404(await verify_invoice(TEST_MINT_ID, victim_ph))

    # The victim's own preimage still redeems the migrated note —
    # the m004 upgrade did not strand it.
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[preimage], p1=victim_h
    )
    assert r["status"] == "OK", r
    note = await get_note(bearer_id(victim_h), TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE


@pytest.mark.anyio
async def test_theft_chain_closed_because_comment_makes_the_preimage_harmless(
    node, db_setup
):
    """The complementary fix: a WALLET that uses comment protection gets
    verify served normally, but the disclosed preimage is no longer the
    note's spend secret (the WALLET-held secret behind `comment` is) — so
    an attacker stealing it from /verify gets nothing to rotate, and the
    theft chain fails at its second step instead."""
    from lnurlmint.services import _try_settle_mint

    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    victim_secret, comment_hash = fresh_secret()

    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment_hash
    )
    assert resp.get("verify"), resp
    victim_pr = resp["pr"]
    victim_ph = bolt11.decode(victim_pr).payment_hash

    # Settle the payment and materialize the note (lazy settlement, keyed
    # by the comment's output — the WALLET's secret, not the preimage).
    node.settled.add(victim_ph)
    mint = await get_mint_by_id(TEST_MINT_ID)
    await _try_settle_mint(bearer_id(comment_hash), mint)

    # ATTACKER: verify is served (comment protection was used) and does
    # disclose the preimage...
    r = await verify_invoice(TEST_MINT_ID, victim_ph)
    assert r.status_code == 200, r
    body = _verify_body(r)
    assert body["settled"] is True, body
    stolen_preimage = body["preimage"]
    assert stolen_preimage is not None, body

    # ...but it redeems nothing — it was never the note's k1 to begin with.
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[stolen_preimage], p1=attacker_h,
    )
    assert r["status"] == "ERROR", r
    assert await get_note(bearer_id(attacker_h), TEST_MINT_ID) is None

    # Only the victim's own held secret redeems the note, at their leisure —
    # no race to win, since nobody else ever had anything that works.
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[victim_secret], p1=victim_h,
    )
    assert r["status"] == "OK", r
    note = await get_note(bearer_id(victim_h), TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE


@pytest.mark.anyio
async def test_verify_refuses_the_legacy_fallback_before_and_after_settlement(
    node, db_setup
):
    """The old exposure window in one picture, now closed at both points
    in time for migrated records: /verify/{ph} 404s for ANY holder of
    the payment_hash of a no-comment mint — both while unpaid and once
    settled."""
    victim_ph, _ = await _legacy_no_comment_mint(node)

    # Before settlement — verify 404s.
    _assert_404(await verify_invoice(TEST_MINT_ID, victim_ph))

    # After settlement — verify still 404s.
    node.settled.add(victim_ph)
    _assert_404(await verify_invoice(TEST_MINT_ID, victim_ph))


@pytest.mark.anyio
async def test_melt_direction_verify_is_harmless(node, db_setup):
    """The melt-direction analog: /verify on a melt's payment_hash returns
    the OUTGOING payment's own preimage. Harmless, as the code claims: the
    notes that funded the melt are burned by the time the preimage appears,
    and the melt preimage keys no note — rotating with it fails as
    unknown."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    k1, note_id, mint = await mint_note(node, VALUE)

    # Victim melts their note into an external invoice.
    melt_invoice = fake_invoice(VALUE)
    melt_ph = bolt11.decode(melt_invoice).payment_hash

    # Reserve the note and register the melt as in-flight, then run
    # _melt_pay to completion (FakeNode pays synchronously). This burns
    # the note and calls mark_melt_settled (settled=1 in melts table).
    decoded = bolt11.decode(melt_invoice)
    from lnurlmint.crud import mark_pending, record_melt
    from lnurlmint.services import _track_melt_start

    await mark_pending([note_id], melt_ph, mint.id)
    await _track_melt_start(melt_ph)
    await record_melt(melt_ph, melt_invoice, mint.id, note_id, VALUE)
    # Simulate the outgoing payment's preimage becoming available (on a
    # real node, sha256(preimage) == melt_ph by the BOLT-11 commitment).
    melt_preimage = "ee" * 32
    node.preimages[melt_ph] = melt_preimage
    await _melt_pay([note_id], melt_invoice, decoded, mint)

    # The note is burned (spent=1, pending=0).
    note = await get_note(note_id, mint.id)
    assert note.spent is True, "note must be spent after successful melt"
    assert note.pending is False

    # Attacker polls the melt's verify once it completes.
    r = await verify_invoice(TEST_MINT_ID, melt_ph)
    assert r.status_code == 200, r
    body = _verify_body(r)
    assert body["settled"] is True, body
    assert body["preimage"] == melt_preimage, body
    assert body["pr"] == melt_invoice, body  # proof-of-payment bundle (LUD-25)

    # The melt preimage is NOT a bearer secret — it keys no note and no
    # mint ever used it as a payment hash. Rotating with it fails.
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[melt_preimage], p1=attacker_h,
    )
    assert r["status"] == "ERROR", r
    assert await get_note(bearer_id(attacker_h), TEST_MINT_ID) is None

    # And the original note's secret is equally dead (already burned).
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], p1=attacker_h,
    )
    assert r["status"] == "ERROR", r


@pytest.mark.anyio
async def test_verify_disabled_closes_the_hole(node, db_setup):
    """With verify_enabled=False (a REAL off switch), the endpoint 404s
    even for a settled migrated mint whose preimage is there for the
    taking — an observer holding the payment_hash learns nothing, and
    the victim's slow manual rotate succeeds untouched."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=False)

    victim_ph, preimage = await _legacy_no_comment_mint(node)
    node.settled.add(victim_ph)

    # The attacker polls verify exactly as in the theft chain above...
    _assert_404(await verify_invoice(TEST_MINT_ID, victim_ph))

    # ...and the victim rotates at human speed, unhurried and unrobbed.
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[preimage], p1=victim_h,
    )
    assert r["status"] == "OK", r
    note = await get_note(bearer_id(victim_h), TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE
