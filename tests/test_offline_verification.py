"""Offline verification PoC — LUD-25 per-mint signatures (Phase 5, TEST-09 partial).

Ports ``test_offline_verification.py`` from the source, adapted to LNbits
async fixtures (endpoint functions called directly, not via TestClient).

Option B (per-mint keypair) replaces the source's Option A (node
``signmessage`` RPC). ``mintPubkey`` is the mint's own secp256k1 public
key, not the node's identity — portable across FakeWallet/VoidWallet.
``sign_note`` uses coincurve's recoverable ECDSA over
``LNURLcash:<amount>:<h>`` (default sha256 hasher, no Lightning Signed
Message prefix). ``verify_note`` recovers the pubkey from the signature
(test-only — never imported by production code).

The two source tests about "absent without a funding source" are N/A
for Option B (every mint always has a keypair) — replaced by
``test_mint_pubkey_matches_derived_pubkey``.
"""

from unittest.mock import MagicMock

import pytest
from coincurve import PrivateKey
from fastapi import BackgroundTasks

from lnurlmint.signing import mint_pubkey, verify_note
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    fake_invoice,
    fresh_secret,
    mint_note,
)
from lnurlmint.views_lnurl import get_withdraw, get_withdraw_callback


def _mock_request() -> MagicMock:
    req = MagicMock()
    req.base_url = "http://test/"
    return req


# ---------------------------------------------------------------------------
# mintPubkey advertisement
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mint_pubkey_matches_derived_pubkey(node, db_setup):
    """The /w response advertises mintPubkey = the compressed pubkey
    derived from mint.mint_privkey (Option B — the mint's own key, not
    the node's)."""
    k1, note_id, mint = await mint_note(node, 5000)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert data["mintPubkey"] == mint_pubkey(mint), data
    # 33-byte compressed pubkey → 66 hex chars
    assert len(data["mintPubkey"]) == 66


# ---------------------------------------------------------------------------
# rotate / split / merge carry verifiable signatures
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rotate_returns_a_valid_signature(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], h=h
    )
    assert data["status"] == "OK", data
    assert "sig" in data, data
    assert verify_note(mint_pubkey(mint), h, 5000, data["sig"]), "sig invalid"
    assert "sig2" not in data, data


@pytest.mark.anyio
async def test_split_returns_valid_signatures_for_both_notes(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    _, h2 = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(),
        k1=[k1], amount=2000, h=h, h2=h2,
    )
    assert data["status"] == "OK", data
    assert verify_note(mint_pubkey(mint), h, 2000, data["sig"]), "sig invalid"
    assert verify_note(mint_pubkey(mint), h2, 3000, data["sig2"]), "sig2 invalid"


@pytest.mark.anyio
async def test_merge_returns_a_valid_signature(node, db_setup):
    k1_a, _, mint = await mint_note(node, 2000)
    k1_b, _, _ = await mint_note(node, 3000)
    _, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1_a, k1_b], h=h
    )
    assert data["status"] == "OK", data
    assert verify_note(mint_pubkey(mint), h, 5000, data["sig"]), "sig invalid"


# ---------------------------------------------------------------------------
# melt carries no signature
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_melt_carries_no_signature(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    pr = fake_invoice(5000)
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], pr=pr
    )
    assert data == {"status": "OK"}, data


# ---------------------------------------------------------------------------
# signatures do not verify against wrong amount / k1 / pubkey
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_signature_does_not_verify_against_wrong_amount(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], h=h
    )
    assert not verify_note(mint_pubkey(mint), h, 5001, data["sig"])


@pytest.mark.anyio
async def test_signature_does_not_verify_against_wrong_k1(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    _, other_h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], h=h
    )
    assert not verify_note(mint_pubkey(mint), other_h, 5000, data["sig"])


@pytest.mark.anyio
async def test_signature_does_not_verify_against_wrong_pubkey(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], h=h
    )
    wrong_pubkey = PrivateKey().public_key.format(compressed=True).hex()
    assert not verify_note(wrong_pubkey, h, 5000, data["sig"])


# ---------------------------------------------------------------------------
# signing failure is swallowed (never blocks) and logged
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_signing_failure_is_swallowed_not_raised(node, db_setup, monkeypatch):
    """A rotate/split/merge must still succeed even if signing fails —
    offline verification is optional. sign_note returns None on any
    error (never raises), and the response omits the sig key.

    We break the coincurve PrivateKey constructor so sign_note's own
    try/except catches the error, logs a warning, and returns None —
    exactly the path a real signing backend failure would take."""

    class _BrokenPrivateKey:
        def __init__(self, *args, **kwargs):
            raise ConnectionError("signing backend unreachable")

    import lnurlmint.signing as signing_module

    monkeypatch.setattr(signing_module, "PrivateKey", _BrokenPrivateKey)

    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], h=h
    )
    assert data["status"] == "OK", data
    assert "sig" not in data, data


@pytest.mark.anyio
async def test_signing_failure_is_still_logged(node, db_setup, monkeypatch):
    """Regression: sign_note used to swallow every exception with zero
    trace anywhere — a persistently broken signing backend was
    indistinguishable from "offline verification just isn't configured"
    from the logs alone. Now a warning containing 'sign_note' is logged."""

    class _BrokenPrivateKey:
        def __init__(self, *args, **kwargs):
            raise ConnectionError("missing signmessage permission")

    import lnurlmint.signing as signing_module
    from loguru import logger as loguru_logger

    monkeypatch.setattr(signing_module, "PrivateKey", _BrokenPrivateKey)

    captured = []
    sink_id = loguru_logger.add(
        lambda msg: captured.append(msg),
        level="WARNING",
        format="{message}",
    )
    try:
        k1, note_id, mint = await mint_note(node, 5000)
        _, h = fresh_secret()
        await get_withdraw_callback(
            TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], h=h
        )
    finally:
        loguru_logger.remove(sink_id)

    assert any(
        "sign_note" in msg and "missing signmessage permission" in msg
        for msg in captured
    ), captured
