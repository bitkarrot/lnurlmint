"""LUD-21 verify endpoint tests — ported from the source test_verify.py.

Covers the /verify endpoint's settlement-status reporting for both the
mint direction (incoming payments) and the melt direction (outgoing
payments), the verify URL advertisement in /p/cb and /w/cb responses,
and the verify_enabled off-switch.

Two migration tests from the source (test_mints_table_migrates_*,
test_melts_table_migrates_*) are NOT ported — the port uses LNbits'
migration framework from the start, so there is no pre-LUD-21 schema
to migrate from.

Adapted to LNbits async fixtures: direct endpoint calls (no TestClient),
per-test DB isolation, FakeNode with get_standalone_payment monkeypatch
for live preimage fetch, verify_enabled toggled via update_mint (not
monkeypatching settings).
"""

import asyncio
import json
from hashlib import sha256
from unittest.mock import MagicMock

import bolt11
import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import get_note, update_mint
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
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
    """A minimal Request mock for endpoints that call _public_base_url."""
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


# ---------------------------------------------------------------------------
# Mint-direction verify URL advertisement
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_url_absent_by_default(node, db_setup):
    """verify is not advertised in /p/cb when verify_enabled is off."""
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    assert "verify" not in resp, resp


@pytest.mark.anyio
async def test_verify_url_advertised_when_enabled(node, db_setup):
    """verify is advertised for a comment-protected mint when verify_enabled."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    _, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    assert "verify" in resp, resp
    pr = resp["pr"]
    payment_hash = bolt11.decode(pr).payment_hash
    assert payment_hash in resp["verify"]


@pytest.mark.anyio
async def test_verify_url_absent_without_comment(node, db_setup):
    """Per LUD-25's Security considerations, SERVICE MUST NOT offer verify
    in the no-comment fallback: there the preimage IS the note's entire
    bearer secret, and verify would hand it to anyone holding the URL."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    assert "verify" not in resp, resp
    payment_hash = bolt11.decode(resp["pr"]).payment_hash
    _assert_404(await verify_invoice(TEST_MINT_ID, payment_hash))
    node.settled.add(payment_hash)
    # ...even after settlement, when the preimage would otherwise be served
    _assert_404(await verify_invoice(TEST_MINT_ID, payment_hash))


# ---------------------------------------------------------------------------
# Mint-direction verify settlement status
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_reports_unsettled_before_payment(node, db_setup):
    """verify reports settled=False (with pr, no preimage) before payment."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    _, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    payment_hash = bolt11.decode(resp["pr"]).payment_hash

    result = _verify_body(await verify_invoice(TEST_MINT_ID, payment_hash))
    assert result == {"status": "OK", "settled": False, "pr": resp["pr"], "preimage": None}


@pytest.mark.anyio
async def test_verify_reports_settled_after_payment(node, db_setup):
    """verify reports settled=True (with pr and preimage) after payment."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    _, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    payment_hash = bolt11.decode(resp["pr"]).payment_hash
    node.settled.add(payment_hash)

    result = _verify_body(await verify_invoice(TEST_MINT_ID, payment_hash))
    assert result["status"] == "OK"
    assert result["settled"] is True
    assert result["pr"] == resp["pr"]
    assert result["preimage"] == node.preimages[payment_hash]


@pytest.mark.anyio
async def test_verify_withholds_the_preimage_before_settlement(node, db_setup):
    """Plain LUD-21 behavior: the preimage is only handed over once settled,
    so an unsettled invoice's verify response must not leak it."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    _, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    payment_hash = bolt11.decode(resp["pr"]).payment_hash

    body = _verify_body(await verify_invoice(TEST_MINT_ID, payment_hash))
    assert node.preimages[payment_hash] not in body
    assert body.get("preimage") is None


@pytest.mark.anyio
async def test_verify_unknown_payment_hash_is_not_found(node, db_setup):
    """An unknown payment_hash returns 404, not an error dict."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    bogus = "00" * 32
    _assert_404(await verify_invoice(TEST_MINT_ID, bogus))


@pytest.mark.anyio
async def test_verify_stays_settled_after_the_note_is_spent(node, db_setup):
    """LUD-21 verify answers 'was this invoice ever paid', not 'is there a
    spendable note right now' — those diverge once the note is rotated,
    but the preimage is still handed back regardless."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    secret, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    assert resp["pr"]
    payment_hash = bolt11.decode(resp["pr"]).payment_hash
    preimage = node.preimages[payment_hash]
    node.settled.add(payment_hash)

    result = _verify_body(await verify_invoice(TEST_MINT_ID, payment_hash))
    assert result["settled"] is True
    assert result["preimage"] == preimage

    # Rotate the note (burn + mint a new one)
    _, h = fresh_secret()
    rotated = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[secret], h=h
    )
    assert rotated["status"] == "OK", rotated

    # Verify still reports settled — the invoice was ever paid
    result = _verify_body(await verify_invoice(TEST_MINT_ID, payment_hash))
    assert result["settled"] is True
    assert result["preimage"] == preimage


# ---------------------------------------------------------------------------
# verify_enabled off-switch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_endpoint_is_disabled_entirely_when_verify_enabled_is_false(
    node, db_setup
):
    """VERIFY_ENABLED=false is a real off switch, not just a hidden URL:
    the endpoint 404s even when hit directly with a known payment_hash."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=False)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), VALUE)
    assert "verify" not in resp, resp
    payment_hash = bolt11.decode(resp["pr"]).payment_hash
    _assert_404(await verify_invoice(TEST_MINT_ID, payment_hash))
    # ...even after settlement, when the preimage would be served
    node.settled.add(payment_hash)
    _assert_404(await verify_invoice(TEST_MINT_ID, payment_hash))


# ---------------------------------------------------------------------------
# Melt-direction verify
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_melt_response_carries_no_verify_by_default(node, db_setup):
    """verify_enabled is False — a melt's response must stay a bare
    {"status": "OK"}, same as before LUD-25's melt verify."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=False)
    k1, note_id, mint = await mint_note(node, VALUE)
    pr = fake_invoice(VALUE)
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], pr=pr
    )
    assert resp == {"status": "OK"}


@pytest.mark.anyio
async def test_melt_response_carries_pr_and_verify_url_when_enabled(
    node, db_setup
):
    """When verify_enabled, a melt's response carries the pr and verify URL."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    k1, note_id, mint = await mint_note(node, VALUE)
    pr = fake_invoice(VALUE)
    payment_hash = bolt11.decode(pr).payment_hash
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"
    assert resp["pr"] == pr
    assert payment_hash in resp["verify"]


@pytest.mark.anyio
async def test_melt_verify_reports_settled_and_a_matching_preimage_once_paid(
    node, db_setup
):
    """The preimage handed back here is proof of the *outgoing* payment's
    own settlement, not a bearer secret — the note(s) that funded the melt
    are already burned by the time anyone could use it."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    k1, note_id, mint = await mint_note(node, VALUE)
    pr = fake_invoice(VALUE)
    payment_hash = bolt11.decode(pr).payment_hash
    # Simulate the outgoing payment's preimage becoming available
    melt_preimage = "ee" * 32
    node.preimages[payment_hash] = melt_preimage

    bt = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bt, k1=[k1], pr=pr
    )
    await bt()  # run the background _melt_pay task to completion

    result = _verify_body(await verify_invoice(TEST_MINT_ID, payment_hash))
    assert result["status"] == "OK"
    assert result["settled"] is True
    assert result["pr"] == pr
    assert result["preimage"] == melt_preimage


@pytest.mark.anyio
async def test_melt_verify_reports_unsettled_while_genuinely_pending(
    inflight_node, db_setup
):
    """Real in-flight state: the melt has been accepted and its note marked
    pending, but pay_invoice hasn't returned yet — verify must report
    unsettled during this window."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    k1, note_id, mint = await mint_note(inflight_node, VALUE)
    pr = fake_invoice(VALUE)
    payment_hash = bolt11.decode(pr).payment_hash

    bt = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bt, k1=[k1], pr=pr
    )
    # Run the background _melt_pay task — it blocks at pay_invoice
    # (InFlightNode blocks on pay_release)
    task = asyncio.create_task(bt())
    await inflight_node.pay_started.wait()

    result = _verify_body(await verify_invoice(TEST_MINT_ID, payment_hash))
    assert result["status"] == "OK"
    assert result["settled"] is False
    assert result["pr"] == pr
    assert result.get("preimage") is None

    # Release the payment and let _melt_pay complete
    inflight_node.pay_release.set()
    await task


@pytest.mark.anyio
async def test_melt_verify_reports_settled_immediately_once_finalized_even_if_the_node_lags(
    node, db_setup
):
    """Once _melt_pay finalizes a melt (pay_invoice itself already
    succeeded), verify must report settled right away via
    mark_melt_settled — not by re-asking the funding source, which right
    after a payment lands can still lag or answer inconsistently. Pinned
    here by leaving node.payment_actually_completed at its default False:
    a live is_payment_complete call would report unsettled, but the melt
    already completed and the note is spent."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    k1, note_id, mint = await mint_note(node, VALUE)
    pr = fake_invoice(VALUE)
    payment_hash = bolt11.decode(pr).payment_hash

    bt = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bt, k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"
    await bt()  # run _melt_pay to completion

    note = await get_note(note_id, TEST_MINT_ID)
    assert note.spent is True

    assert node.payment_actually_completed is False  # the node's live view lags
    result = _verify_body(await verify_invoice(TEST_MINT_ID, payment_hash))
    assert result["settled"] is True


@pytest.mark.anyio
async def test_melt_verify_is_also_disabled_when_verify_enabled_is_false(
    node, db_setup
):
    """The same off switch covers the melt direction: the melts row is still
    recorded, but /verify 404s regardless."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=False)
    k1, note_id, mint = await mint_note(node, VALUE)
    pr = fake_invoice(VALUE)
    node.payment_actually_completed = True
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], pr=pr
    )
    assert "verify" not in resp, resp

    payment_hash = bolt11.decode(pr).payment_hash
    _assert_404(await verify_invoice(TEST_MINT_ID, payment_hash))
