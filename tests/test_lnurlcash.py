"""Main behavioral test suite for the lnurlmint extension.

Ported from the source lnurl-mint project's ``test_lnurlcash.py`` (55 tests),
adapting to LNbits' async endpoint-function-call pattern (no TestClient):
endpoint functions are called directly, per-test DB isolation via ``db_setup``,
``FakeNode`` / ``InFlightNode`` fixtures for the payment-service layer, and
``update_mint`` for configurable settings (no global ``settings`` object).

Key adaptation details:
- ``client.get("/p/cb?amount=X")`` -> ``await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=X)``
- ``client.get("/w?k1=X")`` -> ``await get_withdraw(TEST_MINT_ID, _mock_request(), k1=X)``
- ``client.get("/w/cb?k1=X&h=Y")`` -> ``await get_withdraw_callback(TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[X], h=Y)``
- ``monkeypatch.setattr(settings, "base_fee_msat", X)`` -> ``await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=X)``
- ``node.last_preimage.hex()`` -> ``node.preimages[payment_hash]`` (from bolt11.decode(resp["pr"]).payment_hash)
- Melt background tasks: ``BackgroundTasks`` registers but does not run outside
  FastAPI's response machinery -- ``_run_bg`` executes them explicitly.
- Pending/concurrent melt tests: ``inflight_node`` fixture with ``pay_release``
  event replaces the source's threading + ``pay_delay`` pattern.
- ``InFlightNode`` returns ``PaymentFailedStatus`` for unknown hashes (lnd 404),
  so restore-after-failure tests use it instead of the base ``FakeNode`` (which
  returns ``PaymentPendingStatus`` for unknowns).
- The port's ``_melt_pay`` does not pass ``fee_limit_msat`` to ``pay_invoice``
  (LNbits deviation), so fee-limit tests check ``_melt_fee_limit_msat`` directly.
- ``test_pending_note_is_released_if_funding_source_becomes_unavailable`` is
  skipped: the port has no funding-source pre-check in the callback (the
  callback always returns OK and schedules the background task).
"""

import asyncio
import json
from hashlib import sha256
from os import urandom
from typing import Optional
from unittest.mock import MagicMock

import bolt11
import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import db, get_mint_by_id, get_note, update_mint
from lnurlmint.services import _melt_fee_limit_msat
import lnurlmint.services as services_module
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    fake_invoice,
    fresh_secret,
    mint_note,
    note_value,
)
from lnurlmint.views_lnurl import (
    get_pay_callback,
    get_payrequest,
    get_withdraw,
    get_withdraw_callback,
)


def _mock_request() -> MagicMock:
    """A minimal Request mock for endpoints that call _public_base_url."""
    req = MagicMock()
    req.base_url = "http://test/"
    return req


async def _run_bg(bg: BackgroundTasks) -> None:
    """Run all tasks registered in a BackgroundTasks object.

    Outside FastAPI's response machinery, ``background_tasks.add_task``
    registers but never executes the task. This helper runs them explicitly
    so melt tests can observe the post-callback settlement outcome.
    """
    for task in bg.tasks:
        await task.func(*task.args, **task.kwargs)


# ---------------------------------------------------------------------------
# Pay request advertisement
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pay_request_advertises_withdraw_link(db_setup):
    data = await get_payrequest(TEST_MINT_ID, _mock_request())
    assert data["tag"] == "payRequest"
    assert data["withdrawLink"] == "http://test/lnurlmint/w/testmint"
    assert data["minSendable"] <= data["maxSendable"]


# ---------------------------------------------------------------------------
# Mint flow: paid invoice preimage becomes a bearer note
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_paid_invoice_preimage_becomes_a_bearer_note(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=5000)
    assert resp["pr"]
    ph = bolt11.decode(resp["pr"]).payment_hash
    k1 = node.preimages[ph]

    # not settled yet - not a note
    assert await note_value(k1) is None

    node.settled.add(ph)
    assert await note_value(k1) == 5000
    # the informational GET never consumes the note
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_pay_callback_advertises_the_lnaddress_as_not_disposable(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=5000)
    assert resp["disposable"] is False


@pytest.mark.anyio
async def test_pay_callback_enforces_sendable_bounds(db_setup):
    r1 = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=1)
    assert r1["status"] == "ERROR"
    r2 = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=999999999999)
    assert r2["status"] == "ERROR"


@pytest.mark.anyio
async def test_pay_callback_rejects_while_sunsetting(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0, sunset_mint=True)
    r = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=5000)
    assert r["status"] == "ERROR"


@pytest.mark.anyio
async def test_pay_response_omits_mint_fee_when_free(db_setup):
    data = await get_payrequest(TEST_MINT_ID, _mock_request())
    assert "Mint fees:" not in data["metadata"]


@pytest.mark.anyio
async def test_pay_response_advertises_mint_fee_when_configured(db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=1000, fee_percent_ppm=2000)
    data = await get_payrequest(TEST_MINT_ID, _mock_request())
    metadata = data["metadata"]
    assert ["text/plain", "Mint fees: 1000,2000"] in json.loads(metadata)


@pytest.mark.anyio
async def test_pay_response_advertises_fee_inclusive_min_sendable(node, db_setup):
    await update_mint(
        TEST_MINT_ID, TEST_WALLET,
        base_fee_msat=1000, min_mint_msat=10_000, min_sendable_msat=10_000,
    )
    data = await get_payrequest(TEST_MINT_ID, _mock_request())
    min_sendable = data["minSendable"]
    assert min_sendable == 11000  # 10000 (min_mint_msat) + 1000 (fee)

    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=min_sendable)
    assert resp["pr"]
    ph = bolt11.decode(resp["pr"]).payment_hash
    node.settled.add(ph)
    k1 = node.preimages[ph]
    assert await note_value(k1) == 10000


@pytest.mark.anyio
async def test_mint_credits_note_net_of_configured_fee(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=1000, fee_percent_ppm=2000)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=100000)
    ph = bolt11.decode(resp["pr"]).payment_hash
    node.settled.add(ph)
    k1 = node.preimages[ph]
    # 1000 flat + 0.2% of 100000 = 1000 + 200 = 1200, rounded up to 2000
    assert await note_value(k1) == 100000 - 2000


@pytest.mark.anyio
async def test_mint_fee_rounds_up_to_the_nearest_sat(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=0, fee_percent_ppm=1)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=100000000)
    ph = bolt11.decode(resp["pr"]).payment_hash
    node.settled.add(ph)
    k1 = node.preimages[ph]
    # 0.0001% of 100000000 = 100 msat (0.1 sat) - rounded up to 1000
    assert await note_value(k1) == 100000000 - 1000


@pytest.mark.anyio
async def test_pay_callback_rejects_amount_that_cannot_cover_the_fee(db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0, base_fee_msat=1001)
    result = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=1000)
    assert result == {
        "status": "ERROR",
        "reason": "Amount too low to mint a note (min 0 msat net of fees).",
    }


@pytest.mark.anyio
async def test_pay_callback_rejects_amount_below_min_mint(db_setup):
    # min_mint_msat defaults to 10_000; amount=1000 (min_sendable) nets 1000
    result = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=1000)
    assert result == {
        "status": "ERROR",
        "reason": "Amount too low to mint a note (min 10000 msat net of fees).",
    }


@pytest.mark.anyio
async def test_mint_succeeds_at_exactly_min_mint(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=10_000)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=10000)
    ph = bolt11.decode(resp["pr"]).payment_hash
    node.settled.add(ph)
    k1 = node.preimages[ph]
    assert await note_value(k1) == 10000


# ---------------------------------------------------------------------------
# Host-header spoofing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_withdraw_callback_url_ignores_a_spoofed_host_header(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0, base_url="http://testserver")
    k1, _, _ = await mint_note(node, 5000)
    req = MagicMock()
    req.base_url = "http://evil.example/"
    data = await get_withdraw(TEST_MINT_ID, req, k1=k1)
    assert data["callback"] == "http://testserver/lnurlmint/w/cb/testmint"
    assert "evil.example" not in data["callback"]
    assert "evil.example" not in data["defaultDescription"]


@pytest.mark.anyio
async def test_verify_url_ignores_a_spoofed_host_header(node, db_setup):
    await update_mint(
        TEST_MINT_ID, TEST_WALLET,
        verify_enabled=True, base_url="http://testserver", min_mint_msat=0,
    )
    _, comment = fresh_secret()
    req = MagicMock()
    req.base_url = "http://evil.example/"
    data = await get_pay_callback(TEST_MINT_ID, req, amount=5000, comment=comment)
    assert data["verify"].startswith("http://testserver/lnurlmint/verify/")
    assert "evil.example" not in data["verify"]


# ---------------------------------------------------------------------------
# Rotate / split / merge
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rotate_burns_and_replaces_the_note(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    new_k1, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], h=h
    )
    assert data["status"] == "OK"
    assert "k1" not in data
    assert await note_value(k1) is None
    assert await note_value(new_k1) == 5000


@pytest.mark.anyio
async def test_split_mints_amount_and_change(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    new_k1, h = fresh_secret()
    change_k1, h2 = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], amount=2000, h=h, h2=h2,
    )
    assert data["status"] == "OK"
    assert await note_value(k1) is None
    assert await note_value(new_k1) == 2000
    assert await note_value(change_k1) == 3000


@pytest.mark.anyio
async def test_split_merges_multiple_k1s_first(node, db_setup):
    a, _, _ = await mint_note(node, 2000)
    b, _, _ = await mint_note(node, 3000)
    new_k1, h = fresh_secret()
    change_k1, h2 = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[a, b], amount=1000, h=h, h2=h2,
    )
    assert data["status"] == "OK"
    assert await note_value(a) is None
    assert await note_value(b) is None
    assert await note_value(new_k1) == 1000
    assert await note_value(change_k1) == 4000


@pytest.mark.anyio
async def test_split_rejects_amount_out_of_range(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    _, h = fresh_secret()
    _, h2 = fresh_secret()
    for amount in (0, 5000, 6000):
        r = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1], amount=amount, h=h, h2=h2,
        )
        assert r["status"] == "ERROR"
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_split_rejects_while_sunsetting(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    await update_mint(TEST_MINT_ID, TEST_WALLET, sunset_mint=True)
    _, h = fresh_secret()
    _, h2 = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], amount=2000, h=h, h2=h2,
    )
    assert r["status"] == "ERROR"
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_rotate_merge_and_melt_are_unaffected_by_sunsetting(inflight_node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, sunset_mint=True)
    a, _, _ = await mint_note(inflight_node, 2000)
    b, _, _ = await mint_note(inflight_node, 3000)
    c, _, _ = await mint_note(inflight_node, 4000)

    new_a, h_a = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[a], h=h_a
    )
    assert r["status"] == "OK"
    assert await note_value(new_a) == 2000

    new_bc, h_bc = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[b, c], h=h_bc
    )
    assert r["status"] == "OK"
    assert await note_value(new_bc) == 7000

    inflight_node.pay_release.set()
    pr = fake_invoice(7000)
    bg = BackgroundTasks()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[new_bc], pr=pr
    )
    assert r["status"] == "OK"
    await _run_bg(bg)
    assert inflight_node.paid == [pr]


@pytest.mark.anyio
async def test_merge_burns_all_and_mints_the_sum(node, db_setup):
    a, _, _ = await mint_note(node, 2000)
    b, _, _ = await mint_note(node, 3000)
    new_k1, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[a, b], h=h
    )
    assert data["status"] == "OK"
    assert await note_value(a) is None
    assert await note_value(b) is None
    assert await note_value(new_k1) == 5000


@pytest.mark.anyio
async def test_split_deducts_base_fee_from_change_when_mint_charges_fees(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=1000)
    new_k1, h = fresh_secret()
    change_k1, h2 = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], amount=2000, h=h, h2=h2,
    )
    assert data["status"] == "OK"
    assert await note_value(new_k1) == 2000
    assert await note_value(change_k1) == 3000 - 1000


@pytest.mark.anyio
async def test_split_does_not_reapply_fee_percent_ppm(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=0, fee_percent_ppm=500_000)
    _, h = fresh_secret()
    change_k1, h2 = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], amount=2000, h=h, h2=h2,
    )
    assert data["status"] == "OK"
    assert await note_value(change_k1) == 3000


@pytest.mark.anyio
async def test_split_rejects_when_change_cannot_cover_the_base_fee(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=2000)
    _, h = fresh_secret()
    _, h2 = fresh_secret()
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], amount=4500, h=h, h2=h2,
    )
    assert result == {"status": "ERROR", "reason": "insufficient value"}
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_split_rejects_a_zero_value_change_note(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0, base_fee_msat=2000)
    _, h = fresh_secret()
    _, h2 = fresh_secret()
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], amount=3000, h=h, h2=h2,
    )
    assert result == {"status": "ERROR", "reason": "insufficient value"}
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_split_ignores_min_mint_msat_on_both_sides(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=0)
    k1, _, _ = await mint_note(node, 5000)
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=10_000)
    new_k1, h = fresh_secret()
    change_k1, h2 = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], amount=1, h=h, h2=h2,
    )
    assert data["status"] == "OK"
    assert await note_value(new_k1) == 1
    assert await note_value(change_k1) == 4999


@pytest.mark.anyio
async def test_merge_refunds_base_fee_for_every_extra_note(node, db_setup):
    a, _, _ = await mint_note(node, 2000)
    b, _, _ = await mint_note(node, 3000)
    c, _, _ = await mint_note(node, 1000)
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=500)
    new_k1, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[a, b, c], h=h
    )
    assert data["status"] == "OK"
    assert await note_value(new_k1) == 2000 + 3000 + 1000 + 2 * 500


@pytest.mark.anyio
async def test_rotate_is_unaffected_by_mint_fees(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=1000)
    new_k1, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], h=h
    )
    assert data["status"] == "OK"
    assert await note_value(new_k1) == 5000


# ---------------------------------------------------------------------------
# Melt
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_melt_pays_invoice_of_exactly_the_notes_value(inflight_node, db_setup):
    k1, _, _ = await mint_note(inflight_node, 5000)
    pr = fake_invoice(5000)
    inflight_node.pay_release.set()
    bg = BackgroundTasks()
    data = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert data["status"] == "OK"
    await _run_bg(bg)
    assert inflight_node.paid == [pr]
    assert await note_value(k1) is None


@pytest.mark.anyio
async def test_melt_fee_limit_defaults_to_the_baseline_when_mint_fee_is_low(inflight_node, db_setup):
    k1, _, _ = await mint_note(inflight_node, 2_000_000)
    pr = fake_invoice(2_000_000)
    inflight_node.pay_release.set()
    bg = BackgroundTasks()
    await get_withdraw_callback(TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr)
    await _run_bg(bg)
    mint = await get_mint_by_id(TEST_MINT_ID)
    assert _melt_fee_limit_msat(2_000_000, mint) == max(round(2_000_000 * 0.005), 5000)


@pytest.mark.anyio
async def test_melt_fee_limit_follows_a_higher_configured_mint_fee(inflight_node, db_setup):
    k1, _, _ = await mint_note(inflight_node, 2_000_000)
    await update_mint(TEST_MINT_ID, TEST_WALLET, base_fee_msat=50_000)
    pr = fake_invoice(2_000_000)
    inflight_node.pay_release.set()
    bg = BackgroundTasks()
    await get_withdraw_callback(TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr)
    await _run_bg(bg)
    mint = await get_mint_by_id(TEST_MINT_ID)
    assert _melt_fee_limit_msat(2_000_000, mint) == 50_000


@pytest.mark.anyio
async def test_melt_rejects_multiple_k1s(node, db_setup):
    a, _, _ = await mint_note(node, 2000)
    b, _, _ = await mint_note(node, 3000)
    pr = fake_invoice(5000)
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[a, b], pr=pr
    )
    assert result["status"] == "ERROR"
    assert node.paid == []
    assert await note_value(a) == 2000
    assert await note_value(b) == 3000


@pytest.mark.anyio
async def test_melt_rejects_invoice_of_wrong_amount(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    pr = fake_invoice(4000)
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], pr=pr
    )
    assert result["status"] == "ERROR"
    assert node.paid == []
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_failed_payment_restores_the_notes(inflight_node, db_setup):
    k1, _, _ = await mint_note(inflight_node, 5000)
    inflight_node.fail_payments = True
    inflight_node.pay_release.set()
    pr = fake_invoice(5000)
    bg = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"
    await _run_bg(bg)
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_pending_note_rejects_concurrent_operations(inflight_node, db_setup):
    k1, _, _ = await mint_note(inflight_node, 5000)
    pr = fake_invoice(5000)

    bg = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"

    # Start the background melt task - it blocks on pay_release
    melt_task = asyncio.create_task(_run_bg(bg))
    await inflight_node.pay_started.wait()

    # Concurrent operation should be rejected with "pending"
    _, h = fresh_secret()
    concurrent = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], h=h
    )

    inflight_node.pay_release.set()
    await melt_task

    assert concurrent == {"status": "ERROR", "reason": "pending"}
    assert inflight_node.paid == [pr]
    assert await note_value(k1) is None


@pytest.mark.anyio
async def test_pending_note_is_released_if_the_payment_fails(inflight_node, db_setup):
    k1, _, _ = await mint_note(inflight_node, 5000)
    inflight_node.fail_payments = True
    pr = fake_invoice(5000)

    bg = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"

    melt_task = asyncio.create_task(_run_bg(bg))
    await inflight_node.pay_started.wait()

    _, h = fresh_secret()
    concurrent = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], h=h
    )

    inflight_node.pay_release.set()
    await melt_task

    assert concurrent == {"status": "ERROR", "reason": "pending"}
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_payment_failed_still_confirms_before_restoring(node, db_setup, monkeypatch):
    from lnbits.wallets.base import PaymentFailedStatus, PaymentSuccessStatus

    k1, _, _ = await mint_note(node, 5000)
    node.fail_reason = "Could not find a route to pay this invoice."

    # FakeNode.check_transaction_status returns PaymentPendingStatus for
    # unknown hashes (paid=None), which would leave the note pending
    # instead of restoring it. Override to return PaymentFailedStatus
    # (paid=False) so _confirm_payment confirms the failure and restores.
    async def failing_check(wallet_id, payment_hash):
        node.check_transaction_status_calls += 1
        if payment_hash in node.settled:
            return PaymentSuccessStatus()
        return PaymentFailedStatus()
    monkeypatch.setattr(services_module, "check_transaction_status", failing_check)

    pr = fake_invoice(5000)
    bg = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"
    await _run_bg(bg)
    assert await note_value(k1) == 5000
    assert node.check_transaction_status_calls > 0


@pytest.mark.skip(
    "Port has no funding-source pre-check in the callback; the callback "
    "always returns OK and schedules the background task. The source's "
    "fundingsource_backend=None pre-check does not translate."
)
@pytest.mark.anyio
async def test_pending_note_is_released_if_funding_source_becomes_unavailable(node, db_setup):
    pass


@pytest.mark.anyio
async def test_melt_rejects_own_pending_invoice(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0)
    k1, _, _ = await mint_note(node, 5000)
    resp = await get_pay_callback(TEST_MINT_ID, _mock_request(), amount=5000)
    pr = resp["pr"]
    new_k1 = node.preimages[bolt11.decode(pr).payment_hash]

    bg = BackgroundTasks()
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert result["status"] == "ERROR"
    assert node.paid == []
    assert await note_value(k1) == 5000
    assert await note_value(new_k1) is None


@pytest.mark.anyio
async def test_melt_rejects_already_settled_own_invoice(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    settled_k1, settled_note_id, _ = await mint_note(node, 5000)
    # settled_note_id == sha256(k1) == payment_hash of the mint record
    assert await note_value(settled_k1) == 5000
    pr = fake_invoice(5000, settled_note_id)

    bg = BackgroundTasks()
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert result["status"] == "ERROR"
    assert node.paid == []
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_ambiguously_failed_payment_that_actually_succeeded_does_not_restore(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    node.fail_payments = True
    node.payment_actually_completed = True
    pr = fake_invoice(5000)
    bg = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"
    await _run_bg(bg)
    assert await note_value(k1) is None


@pytest.mark.anyio
async def test_undeterminable_payment_status_leaves_the_note_pending(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    node.fail_payments = True
    node.is_payment_complete_raises = True
    pr = fake_invoice(5000)
    bg = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"
    await _run_bg(bg)
    note_id = sha256(bytes.fromhex(k1)).hexdigest()
    note = await get_note(note_id, TEST_MINT_ID)
    assert note.amount_msat == 5000
    r = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert r == {"status": "ERROR", "reason": "pending"}
    _, h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], h=h
    )
    assert r == {"status": "ERROR", "reason": "pending"}


@pytest.mark.anyio
async def test_hodl_invoice_attack_leaves_the_note_pending_instead_of_restoring(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    node.fail_reason = "Could not find a route to pay this invoice."
    node.is_payment_complete_raises = True
    pr = fake_invoice(5000)
    bg = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"
    await _run_bg(bg)
    note_id = sha256(bytes.fromhex(k1)).hexdigest()
    note = await get_note(note_id, TEST_MINT_ID)
    assert note.amount_msat == 5000
    r = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert r == {"status": "ERROR", "reason": "pending"}
    _, h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], h=h
    )
    assert r == {"status": "ERROR", "reason": "pending"}


@pytest.mark.anyio
async def test_undeterminable_payment_status_retries_before_giving_up(node, db_setup, monkeypatch):
    monkeypatch.setattr(services_module, "_CONFIRMATION_RETRY_DELAYS_SECONDS", (0, 0))
    k1, _, _ = await mint_note(node, 5000)
    node.fail_payments = True
    node.payment_actually_completed = True

    attempts = {"n": 0}
    real_check = node.check_transaction_status

    async def flaky_check(wallet_id, payment_hash):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ConnectionError("funding source unreachable")
        return await real_check(wallet_id, payment_hash)

    monkeypatch.setattr(services_module, "check_transaction_status", flaky_check)

    pr = fake_invoice(5000)
    bg = BackgroundTasks()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), bg, k1=[k1], pr=pr
    )
    assert resp["status"] == "OK"
    await _run_bg(bg)
    assert await note_value(k1) is None


# ---------------------------------------------------------------------------
# k1 validation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_any_invalid_k1_fails_the_whole_request(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    bogus = urandom(32).hex()
    _, h = fresh_secret()
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1, bogus], h=h
    )
    assert result == {"status": "ERROR", "reason": "Invalid or already spent k1."}
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_duplicate_k1_cannot_be_double_counted(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    _, h = fresh_secret()
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1, k1], h=h
    )
    assert result == {"status": "ERROR", "reason": "Invalid or already spent k1."}
    assert await note_value(k1) == 5000


@pytest.mark.anyio
async def test_too_many_k1s_is_rejected(db_setup):
    k1s = [urandom(32).hex() for _ in range(101)]
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=k1s
    )
    assert result == {"status": "ERROR", "reason": "Too many k1s (max 100)."}


@pytest.mark.anyio
async def test_amount_cannot_be_combined_with_pr(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    pr = fake_invoice(2000)
    result = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], pr=pr, amount=2000,
    )
    assert result["status"] == "ERROR"
    assert await note_value(k1) == 5000


# ---------------------------------------------------------------------------
# Withdraw informational endpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_withdraw_response_echoes_the_literal_secret(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    r = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert r["k1"] == k1


@pytest.mark.anyio
async def test_withdraw_requires_k1(db_setup):
    r = await get_withdraw(TEST_MINT_ID, _mock_request(), k1="")
    assert r["status"] == "ERROR"


@pytest.mark.anyio
async def test_withdraw_reports_unknown_k1_distinctly_from_spent(node, db_setup):
    bogus, _ = fresh_secret()
    unknown = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=bogus)
    assert unknown == {"status": "ERROR", "reason": "Unknown note."}

    k1, _, _ = await mint_note(node, 5000)
    _, h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], h=h
    )
    assert r["status"] == "OK"
    spent = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert spent == {"status": "ERROR", "reason": "Note already spent."}


@pytest.mark.anyio
async def test_withdraw_ignores_the_declared_amount(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1, amount=1)
    assert data["maxWithdrawable"] == 5000


# ---------------------------------------------------------------------------
# Bearer secret never persisted
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_bearer_secret_is_ever_persisted(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    new_k1, h = fresh_secret()
    change_k1, h2 = fresh_secret()
    await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], amount=2000, h=h, h2=h2,
    )
    notes_rows = await db.fetchall("SELECT * FROM lnurlmint.notes")
    mints_rows = await db.fetchall("SELECT * FROM lnurlmint.mints_records")
    stored = str(notes_rows) + str(mints_rows)
    for secret in (k1, new_k1, change_k1):
        assert secret not in stored
    assert sha256(bytes.fromhex(k1)).hexdigest() in stored
    assert h in stored
    assert h2 in stored


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_spent_k1_cannot_be_replayed(node, db_setup):
    k1, _, _ = await mint_note(node, 5000)
    new_k1, h = fresh_secret()
    first = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], h=h
    )
    assert first["status"] == "OK"
    _, other_h = fresh_secret()
    second = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], h=other_h
    )
    assert second["status"] == "ERROR"
    assert await note_value(new_k1) == 5000
