"""LUD-25 comment protection tests — ported from the source
test_comment_protection.py.

A WALLET attaches ``comment = hex(sha256(secret))`` to a mint payment, and
once it settles the resulting note is credited as ``k1=<secret>`` instead
of the payment preimage ``P`` — closing the routing-node preimage race and,
since ``P`` no longer redeems anything, making it safe for SERVICE to serve
LUD-21 verify on that invoice too.

This file covers the mint-side mechanics: what a valid/invalid/absent
comment does to the resulting note, informational-GET resolution by
secret alone (no prior verify or rotate needed), the commentAllowed
advertisement, and comment-hash collisions.

Adapted to LNbits async fixtures: direct endpoint calls (no TestClient),
per-test DB isolation, verify_enabled toggled via update_mint (not
monkeypatching settings), mint_note / mint_note_with_comment helpers
from conftest.py.
"""

from hashlib import sha256
from unittest.mock import MagicMock

import bolt11
import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import get_note, update_mint
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    fresh_secret,
    mint_note,
)
from lnurlmint.views_lnurl import (
    get_pay_callback,
    get_payrequest,
    get_withdraw,
    get_withdraw_callback,
)

VALUE = 21_000


def _mock_request() -> MagicMock:
    """A minimal Request mock for endpoints that call _public_base_url."""
    req = MagicMock()
    req.base_url = "http://test/"
    return req


def _ph(resp: dict) -> str:
    """Decode the payment_hash from a /p/cb response dict."""
    return bolt11.decode(resp["pr"]).payment_hash


@pytest.mark.anyio
async def test_pay_response_advertises_comment_allowed(node, db_setup):
    """The payRequest advertises commentAllowed >= 64 (a sha256 digest)."""
    data = await get_payrequest(TEST_MINT_ID, _mock_request())
    assert data["commentAllowed"] >= 64


@pytest.mark.anyio
async def test_valid_comment_credits_the_note_under_the_secret_not_the_preimage(
    node, db_setup
):
    """A valid comment keys the note by the WALLET-supplied secret, not the
    payment preimage — the preimage plays no further role."""
    secret, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    assert resp["pr"]
    payment_hash = _ph(resp)
    preimage = node.preimages[payment_hash]
    node.settled.add(payment_hash)

    # the note resolves under the secret...
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=secret)
    assert data["tag"] == "withdrawRequest"
    assert data["maxWithdrawable"] == VALUE

    # ...never under the raw preimage, which played no further role
    err = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=preimage)
    assert err == {"status": "ERROR", "reason": "Unknown note."}
    _, h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[preimage], h=h
    )
    assert r == {"status": "ERROR", "reason": "Invalid or already spent k1."}


@pytest.mark.anyio
async def test_valid_comment_note_redeems_normally_by_secret(node, db_setup):
    """A comment-protected note redeems normally via the WALLET-held secret."""
    secret, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    node.settled.add(_ph(resp))

    _, h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[secret], h=h
    )
    assert r["status"] == "OK", r
    note = await get_note(h, TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE


@pytest.mark.anyio
async def test_missing_comment_falls_back_to_preimage_keyed_note(node, db_setup):
    """No comment → the note is keyed by the payment preimage (legacy path)."""
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    assert resp["pr"]
    payment_hash = _ph(resp)
    node.settled.add(payment_hash)
    preimage = node.preimages[payment_hash]

    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=preimage)
    assert data["tag"] == "withdrawRequest"
    assert data["maxWithdrawable"] == VALUE


@pytest.mark.anyio
async def test_malformed_comment_falls_back_to_preimage_keyed_note(node, db_setup):
    """A non-hex64 comment is never a hard error — it just doesn't engage
    comment protection, falling back to the preimage-keyed note."""
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment="not-a-hash"
    )
    assert resp["pr"]
    payment_hash = _ph(resp)
    node.settled.add(payment_hash)
    preimage = node.preimages[payment_hash]

    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=preimage)
    assert data["tag"] == "withdrawRequest"
    assert data["maxWithdrawable"] == VALUE


@pytest.mark.anyio
async def test_verify_advertised_only_with_a_valid_comment(node, db_setup):
    """verify is advertised only when a valid hex64 comment was used —
    not for no-comment or malformed-comment mints."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)

    _, comment = fresh_secret()
    with_comment = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    assert with_comment.get("verify")

    no_comment = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    assert "verify" not in no_comment

    malformed = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment="nope"
    )
    assert "verify" not in malformed


@pytest.mark.anyio
async def test_informational_get_lazily_settles_a_comment_protected_mint_without_verify(
    node, db_setup
):
    """A WALLET need not touch /verify at all to claim a comment-protected
    note — plain GET /w?k1=<secret> (the ordinary LUD-03 informational
    query) must lazily materialize it too."""
    secret, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    node.settled.add(_ph(resp))

    assert await get_note(comment, TEST_MINT_ID) is None  # not yet materialized
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=secret)
    assert data["maxWithdrawable"] == VALUE
    note = await get_note(comment, TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE  # now it is


@pytest.mark.anyio
async def test_unsettled_comment_protected_mint_is_not_yet_a_note(node, db_setup):
    """An unsettled comment-protected mint is not yet a note — /w?k1=<secret>
    returns an error before the payment settles."""
    secret, comment = fresh_secret()
    await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    # not settled — the fake node hasn't been told this payment_hash paid
    err = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=secret)
    assert err == {"status": "ERROR", "reason": "Unknown note."}


@pytest.mark.anyio
async def test_comment_colliding_with_an_outstanding_note_is_rejected(
    node, db_setup
):
    """A comment hash that's already in use as an outstanding note's id is
    rejected — create_mint must refuse rather than let a later settle
    silently shadow or fail against that note."""
    existing_k1, existing_note_id, mint = await mint_note(node, VALUE)
    # materialize via the informational GET (mint_note already materializes,
    # but exercise the full path)
    r = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=existing_k1)
    assert r["maxWithdrawable"] == VALUE
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=existing_note_id
    )
    assert resp == {"status": "ERROR", "reason": "comment already in use"}
    # the existing note is completely unaffected
    note = await get_note(existing_note_id, TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE


@pytest.mark.anyio
async def test_comment_colliding_with_another_pending_mint_is_rejected(
    node, db_setup
):
    """A comment hash already used by another pending mint is rejected."""
    _, comment = fresh_secret()
    first = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    assert first["pr"]

    second = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    assert second == {"status": "ERROR", "reason": "comment already in use"}


@pytest.mark.anyio
async def test_comment_protected_note_can_split_rotate_and_merge_like_any_other(
    node, db_setup
):
    """A comment-protected note can split, rotate, and merge like any other."""
    secret, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    node.settled.add(_ph(resp))

    _, h = fresh_secret()
    _, h2 = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID,
        MagicMock(),
        BackgroundTasks(),
        k1=[secret],
        h=h,
        h2=h2,
        amount=5000,
    )
    assert r["status"] == "OK", r
    note_h = await get_note(h, TEST_MINT_ID)
    assert note_h is not None and note_h.amount_msat == 5000
    note_h2 = await get_note(h2, TEST_MINT_ID)
    assert note_h2 is not None and note_h2.amount_msat == VALUE - 5000
