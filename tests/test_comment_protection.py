"""LUD-25 comment protection tests — ported from the source
test_comment_protection.py, updated for the taproot protocol.

A WALLET attaches ``comment = <cp1 or hex h>`` to a mint payment, and
once it settles the resulting note is credited under that output key —
for an ``h`` comment, the bearer note ``k1=<secret>`` where
``h = sha256(secret)``. The payment preimage ``P`` plays no role at all —
closing the routing-node preimage race and making LUD-21 verify safe on
every mint. That is why the comment is now MANDATORY on the fixed
identity's callback (upstream's "comment protection on everything").

This file covers the mint-side mechanics: what a valid/invalid/absent
comment does to the resulting note, informational-GET resolution by
secret alone (no prior verify or rotate needed), the commentAllowed
advertisement, and output collisions.
"""

from unittest.mock import MagicMock

import bolt11
import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import get_note, update_mint
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    bearer_id,
    fresh_secret,
    k1_hash,
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
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[preimage], p1=h
    )
    assert r["status"] == "ERROR"


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
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[secret], p1=h
    )
    assert r["status"] == "OK", r
    note = await get_note(bearer_id(h), TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE


@pytest.mark.anyio
async def test_missing_comment_is_rejected(node, db_setup):
    """No comment on the fixed identity -> ERROR: the preimage is never
    the note's key (upstream removed the fallback entirely)."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), 5000)
    assert resp["status"] == "ERROR"


@pytest.mark.anyio
async def test_malformed_comment_is_rejected_on_the_fixed_identity(node, db_setup):
    """A comment that decodes to no output (not cp1, not hex h) is an
    error on the fixed identity — there is no preimage fallback left.
    (On a registered username's address the same comment would be an
    ordinary human message and auto-mint on the branch.)"""
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0)
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), 5000, comment="not-a-hash"
    )
    assert resp["status"] == "ERROR"


@pytest.mark.anyio
async def test_verify_advertised_for_every_comment_protected_mint(node, db_setup):
    """Every new-protocol mint is comment-protected (a wallet-chosen note
    id or a branch-derived key — never the preimage), so verify is
    advertised on every mint while verify_enabled is on."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)

    _, comment = fresh_secret()
    minted = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    assert minted.get("verify")

    # verify_enabled=0 still suppresses the advertisement entirely
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=False)
    _, comment2 = fresh_secret()
    disabled = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment2
    )
    assert "verify" not in disabled


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

    assert await get_note(bearer_id(comment), TEST_MINT_ID) is None
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=secret)
    assert data["maxWithdrawable"] == VALUE
    note = await get_note(bearer_id(comment), TEST_MINT_ID)
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
    """A comment naming an output already in use as an outstanding note's
    id is rejected — the mint must refuse rather than let a later settle
    silently shadow or fail against that note."""
    existing_k1, existing_note_id, mint = await mint_note(node, VALUE)
    r = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=existing_k1)
    assert r["maxWithdrawable"] == VALUE
    # the existing bearer note's `h` is sha256 of its preimage — a comment
    # equal to it resolves to the same note id
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=k1_hash(existing_k1)
    )
    assert resp == {"status": "ERROR", "reason": "already in use"}
    # the existing note is completely unaffected
    note = await get_note(existing_note_id, TEST_MINT_ID)
    assert note is not None and note.amount_msat == VALUE


@pytest.mark.anyio
async def test_comment_colliding_with_another_pending_mint_is_rejected(
    node, db_setup
):
    """A comment already used by another pending mint is rejected."""
    _, comment = fresh_secret()
    first = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    assert first["pr"]

    second = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    assert second == {"status": "ERROR", "reason": "already in use"}


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
        p1=h,
        p2=h2,
        amount=5000,
    )
    assert r["status"] == "OK", r
    note_h = await get_note(bearer_id(h), TEST_MINT_ID)
    assert note_h is not None and note_h.amount_msat == 5000
    note_h2 = await get_note(bearer_id(h2), TEST_MINT_ID)
    assert note_h2 is not None and note_h2.amount_msat == VALUE - 5000
