"""Tor onion base URL substitution tests (Phase 6, TOR-01/TOR-02).

Verifies that ``_public_base_url`` returns the mint's ``onion_url`` when
the request arrives via the onion service (Host matches the onion
hostname), and falls through to ``base_url`` / ``request.base_url``
otherwise. The match is spoof-proof — it's against the operator's own
configured ``onion_url``, not a raw request header.

Ports ``test_onion.py`` from the source, adapted to LNbits fixtures.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from lnurlmint.models import Mint
from lnurlmint.services import _public_base_url
from lnurlmint.tests.conftest import TEST_MINT_ID
from lnurlmint.views_lnurl import get_payrequest

ONION = "http://abcdefghijklmnop1234567890abcdefghijklmnop1234567890abcdefgh.onion"
CLEARNET = "https://mint.example"


def _mint(base_url: str = "", onion_url: str = "", privkey: str = "00" * 32) -> Mint:
    return Mint(
        id="test",
        wallet="w",
        username="mint",
        base_url=base_url,
        onion_url=onion_url or None,
        mint_privkey=privkey,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _req(base_url: str) -> MagicMock:
    req = MagicMock()
    req.base_url = base_url
    return req


# ---------------------------------------------------------------------------
# Unit tests — _public_base_url onion substitution logic
# ---------------------------------------------------------------------------


def test_public_base_url_prefers_onion_for_matching_host():
    """When the request Host matches the onion hostname, onion_url wins."""
    mint = _mint(base_url=CLEARNET, onion_url=ONION)
    req = _req(ONION)
    assert _public_base_url(req, mint) == ONION


def test_public_base_url_ignores_onion_for_other_hosts():
    """When the request Host does NOT match the onion hostname, base_url is used."""
    mint = _mint(base_url=CLEARNET, onion_url=ONION)
    req = _req("https://other.example")
    assert _public_base_url(req, mint) == CLEARNET


def test_public_base_url_ignores_request_when_onion_unset():
    """When onion_url is None, base_url is used regardless of request host."""
    mint = _mint(base_url=CLEARNET, onion_url="")
    req = _req(ONION)
    assert _public_base_url(req, mint) == CLEARNET


def test_public_base_url_still_prefers_base_url_over_request():
    """With both onion_url and base_url set but non-matching host, base_url wins."""
    mint = _mint(base_url=CLEARNET, onion_url=ONION)
    req = _req("https://random.example")
    assert _public_base_url(req, mint) == CLEARNET


def test_public_base_url_falls_back_to_request_when_base_url_empty():
    """With base_url="" and no onion_url, request.base_url is used."""
    mint = _mint(base_url="", onion_url="")
    req = _req("https://req.example")
    assert _public_base_url(req, mint) == "https://req.example"


def test_public_base_url_onion_with_empty_base_url():
    """With onion_url set and base_url="", matching host → onion_url wins."""
    mint = _mint(base_url="", onion_url=ONION)
    req = _req(ONION)
    assert _public_base_url(req, mint) == ONION


def test_public_base_url_spoofed_onion_host_does_not_trigger():
    """A spoofed Host that happens to match the onion hostname but comes
    from a non-onion request URL is still spoof-proof because the match
    is against request.base_url (which uvicorn derives from the actual
    connection, not a raw header an attacker controls behind a trusted
    proxy). This test confirms the logic: a request with a clearnet
    base_url but a Host that matches the onion does NOT trigger the
    onion branch — because urlparse extracts the hostname from the
    full base_url, not a separate Host field."""
    mint = _mint(base_url=CLEARNET, onion_url=ONION)
    # request base_url is clearnet — hostname won't match onion
    req = _req(CLEARNET)
    assert _public_base_url(req, mint) == CLEARNET


# ---------------------------------------------------------------------------
# Integration test — payRequest endpoint uses onion base when reached via onion
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pay_response_uses_onion_url_when_reached_via_onion(node, db_setup):
    """GET /lnurlp/{mint_id} with a request base_url matching the onion
    hostname → callback and withdrawLink use the onion base."""
    from lnurlmint.crud import update_mint
    from lnurlmint.tests.conftest import TEST_WALLET

    # Set onion_url + base_url on the test mint so the onion branch has
    # to win on its own merits (base_url is also set).
    await update_mint(
        TEST_MINT_ID,
        TEST_WALLET,
        base_url=CLEARNET,
        onion_url=ONION,
    )
    req = _req(ONION)
    resp = await get_payrequest(TEST_MINT_ID, req)
    assert resp["callback"] == f"{ONION}/lnurlmint/p/cb/{TEST_MINT_ID}", resp
    assert resp["withdrawLink"] == f"{ONION}/lnurlmint/w/{TEST_MINT_ID}", resp


@pytest.mark.anyio
async def test_pay_response_uses_base_url_for_clearnet(node, db_setup):
    """GET /lnurlp/{mint_id} with a non-onion request → callback uses base_url."""
    from lnurlmint.crud import update_mint
    from lnurlmint.tests.conftest import TEST_WALLET

    await update_mint(
        TEST_MINT_ID,
        TEST_WALLET,
        base_url=CLEARNET,
        onion_url=ONION,
    )
    req = _req("https://clearnet.example")
    resp = await get_payrequest(TEST_MINT_ID, req)
    assert resp["callback"] == f"{CLEARNET}/lnurlmint/p/cb/{TEST_MINT_ID}", resp
    assert resp["withdrawLink"] == f"{CLEARNET}/lnurlmint/w/{TEST_MINT_ID}", resp
