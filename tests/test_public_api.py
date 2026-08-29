"""Public mint info API endpoint tests (Phase 6, UI-03/UI-04).

Tests the GET /api/v1/public/{mint_id} endpoint — the unauthenticated
data source for the public one-pager. Verifies LNURL generation (with
Tor substitution from Plan 06-01), 404 handling, sunset notice, onion
URL exposure, mint_pubkey, and graceful node_info degradation.
"""

from unittest.mock import MagicMock

import pytest
from bech32 import bech32_decode, convertbits
from fastapi import HTTPException

from lnurlmint.tests.conftest import TEST_MINT_ID, TEST_WALLET
from lnurlmint.views_api import api_get_public_mint_info

ONION = "http://abcdefghijklmnop1234567890abcdefghijklmnop1234567890abcdefgh.onion"
CLEARNET = "https://mint.example"


def _req(base_url: str = "http://testserver") -> MagicMock:
    req = MagicMock()
    req.base_url = base_url
    return req


def lnurl_decode(lnurl: str) -> str:
    """Decode a bech32-encoded LNURL back to the URL string."""
    hrp, data = bech32_decode(lnurl.lower())
    decoded = convertbits(data, 5, 8, False)
    return bytes(decoded).decode()


@pytest.mark.anyio
async def test_public_mint_info_returns_lnurl_and_limits(node, db_setup):
    """GET /public/{mint_id} returns username, lnurl, limits, sunset, pubkey."""
    resp = await api_get_public_mint_info(TEST_MINT_ID, _req())
    assert resp["username"] == "testuser"
    assert resp["lnurl"].startswith("LNURL")
    assert resp["min_mint_msat"] > 0
    assert resp["max_mintable_msat"] > 0
    assert resp["sunset_mint"] is False
    assert resp["mint_pubkey"] is not None
    assert resp["node_info"] is None  # test env has no real node


@pytest.mark.anyio
async def test_public_mint_info_404_for_unknown_mint(node, db_setup):
    """Unknown mint_id → 404."""
    with pytest.raises(HTTPException) as exc_info:
        await api_get_public_mint_info("nonexistent", _req())
    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_public_mint_info_includes_onion_url(node, db_setup):
    """Mint with onion_url set → response includes onion_url."""
    from lnurlmint.crud import update_mint

    await update_mint(TEST_MINT_ID, TEST_WALLET, onion_url=ONION)
    resp = await api_get_public_mint_info(TEST_MINT_ID, _req())
    assert resp["onion_url"] == ONION


@pytest.mark.anyio
async def test_public_mint_info_lnurl_uses_base_url(node, db_setup):
    """Mint with base_url set → LNURL decodes to base_url/lnurlmint/lnurlp/{id}."""
    from lnurlmint.crud import update_mint

    await update_mint(TEST_MINT_ID, TEST_WALLET, base_url=CLEARNET)
    resp = await api_get_public_mint_info(TEST_MINT_ID, _req("http://testserver"))
    decoded = lnurl_decode(resp["lnurl"])
    assert decoded == f"{CLEARNET}/lnurlmint/lnurlp/{TEST_MINT_ID}"


@pytest.mark.anyio
async def test_public_mint_info_lnurl_uses_onion_when_on_tor(node, db_setup):
    """Mint with onion_url, request via onion Host → LNURL uses onion base (06-01)."""
    from lnurlmint.crud import update_mint

    await update_mint(
        TEST_MINT_ID, TEST_WALLET, base_url=CLEARNET, onion_url=ONION
    )
    resp = await api_get_public_mint_info(TEST_MINT_ID, _req(ONION))
    decoded = lnurl_decode(resp["lnurl"])
    assert decoded == f"{ONION}/lnurlmint/lnurlp/{TEST_MINT_ID}"


@pytest.mark.anyio
async def test_public_mint_info_includes_sunset(node, db_setup):
    """Mint with sunset_mint=True → response has sunset_mint: true."""
    from lnurlmint.crud import update_mint

    await update_mint(TEST_MINT_ID, TEST_WALLET, sunset_mint=True)
    resp = await api_get_public_mint_info(TEST_MINT_ID, _req())
    assert resp["sunset_mint"] is True


@pytest.mark.anyio
async def test_public_mint_info_node_info_null_without_funding_source(
    node, db_setup
):
    """Test env (FakeWallet) doesn't implement Node API → node_info is null."""
    resp = await api_get_public_mint_info(TEST_MINT_ID, _req())
    assert resp["node_info"] is None


@pytest.mark.anyio
async def test_public_mint_info_includes_mint_pubkey(node, db_setup):
    """Response includes a non-null mint_pubkey (mint has a privkey)."""
    resp = await api_get_public_mint_info(TEST_MINT_ID, _req())
    assert resp["mint_pubkey"] is not None
    # Compressed pubkey is 33 bytes → 66 hex chars
    assert len(resp["mint_pubkey"]) == 66
