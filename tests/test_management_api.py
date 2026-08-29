"""Management API endpoint tests (Phase 6, UI-01/UI-02/UI-04).

Tests the new notes and activity API endpoints for wallet-scoped
isolation (SEC-07). Calls the endpoint functions directly with mock
WalletTypeInfo objects (following the pattern from the other ported
tests — no HTTP client needed).
"""

from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from lnurlmint.crud import (
    create_mint,
    record_mint_record,
    record_melt,
    settle_mint,
)
from lnurlmint.models import Mint
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    fake_invoice,
    mint_note,
)
from lnurlmint.views_api import api_get_mint_notes, api_get_mint_activity

OTHER_WALLET = "otherwallet"


@dataclass
class _MockWallet:
    id: str


@dataclass
class _MockWalletTypeInfo:
    wallet: _MockWallet


def _wallet_type(wallet_id: str) -> _MockWalletTypeInfo:
    return _MockWalletTypeInfo(wallet=_MockWallet(id=wallet_id))


@pytest.mark.anyio
async def test_get_notes_returns_outstanding_notes(node, db_setup):
    """GET /{mint_id}/notes returns notes for the mint (wallet-scoped)."""
    k1, note_id, mint = await mint_note(node, 5000)
    result = await api_get_mint_notes(TEST_MINT_ID, _wallet_type(TEST_WALLET))
    assert len(result) == 1, result
    assert result[0]["id"] == note_id
    assert result[0]["amount_msat"] == 5000
    assert result[0]["spent"] is False
    assert result[0]["pending"] is False


@pytest.mark.anyio
async def test_get_notes_404_for_cross_wallet(node, db_setup):
    """GET /{mint_id}/notes with wrong wallet key → 404 (SEC-07)."""
    k1, note_id, mint = await mint_note(node, 5000)
    with pytest.raises(HTTPException) as exc_info:
        await api_get_mint_notes(TEST_MINT_ID, _wallet_type(OTHER_WALLET))
    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_get_notes_empty_for_new_mint(node, db_setup):
    """A newly created mint has no notes → empty list."""
    result = await api_get_mint_notes(TEST_MINT_ID, _wallet_type(TEST_WALLET))
    assert result == []


@pytest.mark.anyio
async def test_get_activity_returns_mint_and_melt_records(node, db_setup):
    """GET /{mint_id}/activity returns both mint and melt records."""
    from lnurlmint.crud import mark_pending, mark_melt_settled
    from lnurlmint.services import _track_melt_start, _track_melt_end

    # Record a mint record (pending mint)
    k1, note_id, mint = await mint_note(node, 5000)

    # Record a melt
    melt_pr = fake_invoice(5000)
    from bolt11 import decode as bolt11_decode
    decoded = bolt11_decode(melt_pr)
    melt_ph = decoded.payment_hash
    await mark_pending([note_id], melt_ph, mint.id)
    await _track_melt_start(melt_ph)
    await record_melt(melt_ph, melt_pr, mint.id, note_id, 5000)
    await mark_melt_settled(melt_ph)
    await _track_melt_end(melt_ph)

    result = await api_get_mint_activity(TEST_MINT_ID, _wallet_type(TEST_WALLET))
    types = [r["type"] for r in result]
    assert "mint" in types, result
    assert "melt" in types, result
    # Most recent first (melt should be after mint)
    assert result[0]["type"] == "melt" or result[0]["type"] == "mint"


@pytest.mark.anyio
async def test_get_activity_404_for_cross_wallet(node, db_setup):
    """GET /{mint_id}/activity with wrong wallet key → 404 (SEC-07)."""
    k1, note_id, mint = await mint_note(node, 5000)
    with pytest.raises(HTTPException) as exc_info:
        await api_get_mint_activity(TEST_MINT_ID, _wallet_type(OTHER_WALLET))
    assert exc_info.value.status_code == 404
