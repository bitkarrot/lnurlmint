"""Regression tests for the /verify preimage observer race (TEST-07).

Originally: the race was spec-shaped and remained BY DESIGN whenever verify
was on, since /verify handed a settled mint's preimage (= the no-comment
fallback note's entire spend secret) to ANYONE who knew the payment_hash
(embedded in the invoice itself), letting the first rotater win the note
regardless of who paid for it.

FIXED by LUD-25 comment protection: SERVICE now refuses verify outright
for any mint that skipped `comment` — there the preimage is the note's
whole secret, so the endpoint that used to hand it to any invoice holder
is closed for that mint instead. A mint that DID use `comment` still gets
verify served, but its disclosed preimage is no longer the note's secret
(the WALLET-held `secret` behind `comment` is), so the same theft chain
fails there too, for a different reason.

``test_theft_chain_closed_by_verify_refusal`` and
``test_theft_chain_closed_because_comment_makes_the_preimage_harmless``
below pin both halves of the fix. ``test_verify_disabled_closes_the_hole``
pins that ``verify_enabled=False`` is a real off switch — the endpoint
404s entirely (not just its advertisement).

Ported from the source's ``test_poc_verify_race.py``, adapting to LNbits
async fixtures: endpoint functions called directly (not via TestClient),
per-test DB isolation, FakeNode with ``get_standalone_payment`` monkeypatch
for live preimage fetch.
"""

import asyncio
import json
from hashlib import sha256
from unittest.mock import MagicMock

import bolt11
import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import get_note, get_mint_by_id, update_mint
from lnurlmint.services import _melt_pay
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    fake_invoice,
    fresh_secret,
    mint_note,
)
from lnurlmint.views_lnurl import get_pay_callback, get_withdraw_callback, verify_invoice

VALUE = 50_000


def _mock_request() -> MagicMock:
    """A minimal Request mock for /p/cb (which calls _public_base_url).

    The test mint has no ``base_url`` set, so _public_base_url falls back
    to ``str(request.base_url)`` — a MagicMock stringifies to a stable
    repr, which is fine for the verify URL (we only assert presence).
    """
    req = MagicMock()
    req.base_url = "http://test/"
    return req


def _assert_404(resp) -> None:
    """Assert a JSONResponse is a 404 with the LNURL error body."""
    assert resp.status_code == 404, resp
    body = json.loads(resp.body)
    assert body == {"status": "ERROR", "reason": "Not found"}, body


def _verify_body(resp) -> dict:
    """Extract the verify response body from either a JSONResponse (404)
    or a LnurlPayVerifyResponse model (200)."""
    if hasattr(resp, "status_code"):
        return json.loads(resp.body)
    return resp.dict()


@pytest.mark.anyio
async def test_theft_chain_closed_by_verify_refusal(node, db_setup):
    """The original theft chain, now closed: a mint that skips `comment`
    (LUD-25 comment protection) still credits k1=preimage exactly as
    before, but SERVICE now refuses to serve verify for it at all, even
    with verify_enabled on — so the attacker's very first step (scraping
    the preimage from /verify) never gets off the ground."""
    # Victim requests a mint invoice for 50_000 msat and pays it, WITHOUT
    # comment protection (a legacy or opted-out wallet).
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    assert "verify" not in resp, resp
    victim_pr = resp["pr"]
    victim_ph = bolt11.decode(victim_pr).payment_hash  # what the attacker knows
    node.settled.add(victim_ph)  # the Lightning payment itself

    # ATTACKER (knowing only payment_hash): verify is refused outright, no
    # comment was ever used for this mint.
    r = await verify_invoice(TEST_MINT_ID, victim_ph)
    _assert_404(r)

    # The victim's own preimage (learned the ordinary way, from paying the
    # invoice) still redeems the note normally — only the remote-disclosure
    # endpoint is closed, not the fallback note itself. Verify's refusal
    # above never touched the lazy-settle path, so the note only actually
    # materializes here, on the rotate itself.
    preimage = node.preimages[victim_ph]
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[preimage], h=victim_h
    )
    assert r["status"] == "OK", r
    note = await get_note(victim_h, TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE


@pytest.mark.anyio
async def test_theft_chain_closed_because_comment_makes_the_preimage_harmless(
    node, db_setup
):
    """The complementary fix: a WALLET that DOES use LUD-25 comment
    protection gets verify served normally, but the disclosed preimage is
    no longer the note's spend secret (the WALLET-held `secret` behind
    `comment` is) — so an attacker stealing it from /verify gets nothing
    to rotate, and the theft chain fails at its second step instead."""
    from lnurlmint.services import _try_settle_mint

    victim_secret, comment_hash = fresh_secret()

    # /p/cb with comment=comment_hash creates a pending mint keyed by the
    # WALLET-supplied comment hash and advertises the verify URL.
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment_hash
    )
    assert resp.get("verify"), resp
    victim_pr = resp["pr"]
    victim_ph = bolt11.decode(victim_pr).payment_hash

    # Settle the payment and materialize the note (lazy settlement, keyed
    # by comment_hash — the WALLET's secret, not the payment preimage).
    node.settled.add(victim_ph)
    await _try_settle_mint(comment_hash, await get_mint_by_id(TEST_MINT_ID))

    # ATTACKER: verify is served (comment protection was used) and does
    # disclose the preimage...
    r = await verify_invoice(TEST_MINT_ID, victim_ph)
    assert not hasattr(r, "status_code"), r  # 200 → model, not JSONResponse
    body = _verify_body(r)
    assert body["settled"] is True, body
    stolen_preimage = body["preimage"]
    assert stolen_preimage is not None, body

    # ...but it redeems nothing — it was never the note's k1 to begin with.
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[stolen_preimage], h=attacker_h,
    )
    assert r == {"status": "ERROR", "reason": "Invalid or already spent k1."}, r
    assert await get_note(attacker_h, TEST_MINT_ID) is None

    # Only the victim's own held secret redeems the note, at their leisure —
    # no race to win, since nobody else ever had anything that works.
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[victim_secret], h=victim_h,
    )
    assert r["status"] == "OK", r
    note = await get_note(victim_h, TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE


@pytest.mark.anyio
async def test_verify_refuses_the_no_comment_fallback_before_and_after_settlement(
    node, db_setup
):
    """The old exposure window in one picture, now closed at both points in
    time: from the moment /p/cb answers, /verify/{ph} 404s for ANY holder of
    the payment_hash of a no-comment mint — both while unpaid and once
    settled, never just its advertisement."""
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    ph = bolt11.decode(resp["pr"]).payment_hash

    # Before settlement — verify 404s.
    r = await verify_invoice(TEST_MINT_ID, ph)
    _assert_404(r)

    # After settlement — verify still 404s.
    node.settled.add(ph)
    r = await verify_invoice(TEST_MINT_ID, ph)
    _assert_404(r)


@pytest.mark.anyio
async def test_melt_direction_verify_is_harmless(node, db_setup):
    """The melt-direction analog: /verify on a melt's payment_hash returns
    the OUTGOING payment's own preimage. Harmless, as the code claims: the
    notes that funded the melt are burned by the time the preimage appears,
    and the melt preimage keys no note — rotating with it fails as
    unknown."""
    k1, note_id, mint = await mint_note(node, VALUE)

    # Victim melts their note into an external invoice.
    melt_invoice = fake_invoice(VALUE)
    melt_ph = bolt11.decode(melt_invoice).payment_hash

    # Reserve the note and register the melt as in-flight, then run
    # _melt_pay to completion (FakeNode pays synchronously). This burns
    # the note and calls mark_melt_settled (settled=1 in melts table).
    decoded = bolt11.decode(melt_invoice)
    from lnurlmint.crud import mark_pending, record_melt
    from lnurlmint.services import _track_melt_start, _track_melt_end

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
    assert not hasattr(r, "status_code"), r  # 200 → model, not JSONResponse
    body = _verify_body(r)
    assert body["settled"] is True, body
    assert body["preimage"] == melt_preimage, body
    assert body["pr"] == melt_invoice, body  # proof-of-payment bundle (LUD-25)

    # The melt preimage is NOT a bearer secret — it keys no note and no
    # mint ever used it as a payment hash. Rotating with it fails.
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[melt_preimage], h=attacker_h,
    )
    assert r == {"status": "ERROR", "reason": "Invalid or already spent k1."}, r
    assert await get_note(attacker_h, TEST_MINT_ID) is None

    # And the original note's secret is equally dead (already burned).
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=attacker_h,
    )
    assert r == {"status": "ERROR", "reason": "Invalid or already spent k1."}, r


@pytest.mark.anyio
async def test_verify_disabled_closes_the_hole(node, db_setup):
    """The review's fix: with verify_enabled=False (a REAL off switch),
    the endpoint 404s even for a settled mint whose preimage is there for
    the taking — an observer holding the payment_hash learns nothing, and
    the victim's slow manual rotate succeeds untouched."""
    # Disable verify on the test mint.
    await update_mint(TEST_MINT_ID, "testwallet", verify_enabled=False)

    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    assert "verify" not in resp, resp  # not advertised when disabled
    victim_ph = bolt11.decode(resp["pr"]).payment_hash
    node.settled.add(victim_ph)

    # The attacker polls verify exactly as in the theft chain above...
    r = await verify_invoice(TEST_MINT_ID, victim_ph)
    _assert_404(r)

    # ...and the victim rotates at human speed, unhurried and unrobbed.
    preimage = node.preimages[victim_ph]
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[preimage], h=victim_h,
    )
    assert r["status"] == "OK", r
    note = await get_note(victim_h, TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE
