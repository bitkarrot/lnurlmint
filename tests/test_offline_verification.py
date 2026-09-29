"""Offline verification PoC — LUD-25 per-mint signatures (TEST-09 partial).

Ports ``test_offline_verification.py`` from the source, adapted to LNbits
async fixtures (endpoint functions called directly, not via TestClient)
and to the taproot protocol's `cs1` certificates.

The mint signs ``LNURLcash:<amount_msat>:<hex(Q)>`` under the standard
"Lightning Signed Message" double-sha256 wrap, with a recoverable ECDSA
signature emitted as a `cs1` bech32m certificate (its amount folded into
the HRP — ``cs10n`` for 1000 msat etc., BOLT-11 style). ``verify_note``
recovers the pubkey from the signature (test-only — never imported by
production code). A WALLET compares it to the advertised ``mintPubkey``.
"""

from unittest.mock import MagicMock

import pytest
from coincurve import PrivateKey
from fastapi import BackgroundTasks

from lnurlmint.bech32m import decode_cs1
from lnurlmint.signing import mint_pubkey, verify_note
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    bearer_id,
    fake_invoice,
    fresh_secret,
    mint_note,
)
from lnurlmint.views_lnurl import get_withdraw, get_withdraw_callback


def _mock_request() -> MagicMock:
    req = MagicMock()
    req.base_url = "http://test/"
    return req


def _cert_check(mint, p_value: str, amount_msat: int, cs1: str) -> bool:
    """Decode a cs1 certificate and verify it signs (note_id, amount).

    `p_value` is the p1/p2 the caller passed — a hex `h` short form for
    the bearer notes these tests use, so the note id is bearer_id(h).
    The certificate's own HRP commits to the amount too, checked here."""
    decoded = decode_cs1(cs1)
    if decoded is None:
        return False
    cert_amount, sig = decoded
    if cert_amount != amount_msat:
        return False
    return verify_note(mint_pubkey(mint), bearer_id(p_value), amount_msat, sig.hex())


# ---------------------------------------------------------------------------
# mintPubkey advertisement
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mint_pubkey_matches_derived_pubkey(node, db_setup):
    """The /w response advertises mintPubkey = the compressed pubkey
    derived from mint.mint_privkey — the mint's own key, not the node's."""
    k1, note_id, mint = await mint_note(node, 5000)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert data["mintPubkey"] == mint_pubkey(mint), data
    # 33-byte compressed pubkey → 66 hex chars
    assert len(data["mintPubkey"]) == 66


@pytest.mark.anyio
async def test_withdraw_serves_a_ready_made_certificate(node, db_setup):
    """LUD-25: /w carries `c`, a cs1 certificate for the note itself —
    a holder need not force a rotate just to obtain one."""
    k1, note_id, mint = await mint_note(node, 5000)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert data["c"], data
    decoded = decode_cs1(data["c"])
    assert decoded is not None and decoded[0] == 5000
    assert verify_note(mint_pubkey(mint), note_id, 5000, decoded[1].hex())


# ---------------------------------------------------------------------------
# rotate / split / merge carry verifiable certificates
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rotate_returns_a_valid_certificate(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], p1=h
    )
    assert data["status"] == "OK", data
    assert "c" in data, data
    assert _cert_check(mint, h, 5000, data["c"]), "c invalid"
    assert "c2" not in data, data


@pytest.mark.anyio
async def test_split_returns_valid_certificates_for_both_notes(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    _, h2 = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(),
        k1=[k1], amount=2000, p1=h, p2=h2,
    )
    assert data["status"] == "OK", data
    assert _cert_check(mint, h, 2000, data["c"]), "c invalid"
    assert _cert_check(mint, h2, 3000, data["c2"]), "c2 invalid"


@pytest.mark.anyio
async def test_merge_returns_a_valid_certificate(node, db_setup):
    k1_a, _, mint = await mint_note(node, 2000)
    k1_b, _, _ = await mint_note(node, 3000)
    _, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1_a, k1_b], p1=h
    )
    assert data["status"] == "OK", data
    assert _cert_check(mint, h, 5000, data["c"]), "c invalid"


# ---------------------------------------------------------------------------
# melt carries no certificate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_melt_carries_no_certificate(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    pr = fake_invoice(5000)
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], pr=pr
    )
    assert data["status"] == "OK", data
    # melt carries no certificate (only rotate/split do)
    assert "c" not in data


# ---------------------------------------------------------------------------
# certificates do not verify against wrong amount / note / pubkey
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_certificate_does_not_verify_against_wrong_amount(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], p1=h
    )
    decoded = decode_cs1(data["c"])
    assert decoded is not None
    assert not verify_note(mint_pubkey(mint), bearer_id(h), 5001, decoded[1].hex())


@pytest.mark.anyio
async def test_certificate_does_not_verify_against_wrong_note(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    _, other_h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], p1=h
    )
    decoded = decode_cs1(data["c"])
    assert decoded is not None
    assert not verify_note(
        mint_pubkey(mint), bearer_id(other_h), 5000, decoded[1].hex()
    )


@pytest.mark.anyio
async def test_certificate_does_not_verify_against_wrong_pubkey(node, db_setup):
    k1, note_id, mint = await mint_note(node, 5000)
    _, h = fresh_secret()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], p1=h
    )
    wrong_pubkey = PrivateKey().public_key.format(compressed=True).hex()
    decoded = decode_cs1(data["c"])
    assert decoded is not None
    assert not verify_note(wrong_pubkey, bearer_id(h), 5000, decoded[1].hex())


# ---------------------------------------------------------------------------
# signing failure is swallowed (never blocks) and logged
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_signing_failure_is_swallowed_not_raised(node, db_setup, monkeypatch):
    """A rotate/split/merge must still succeed even if signing fails —
    offline verification is optional. sign_note returns None on any
    error (never raises), and the response omits the certificate key.

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
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], p1=h
    )
    assert data["status"] == "OK", data
    assert "c" not in data, data


@pytest.mark.anyio
async def test_signing_failure_is_still_logged(node, db_setup, monkeypatch):
    """Regression: sign_note used to swallow every exception with zero
    trace anywhere — a persistently broken signing backend was
    indistinguishable from "offline verification just isn't configured"
    from the logs alone. Now a warning containing 'sign_note' is logged."""

    class _BrokenPrivateKey:
        def __init__(self, *args, **kwargs):
            raise ConnectionError("missing signmessage permission")

    from loguru import logger as loguru_logger

    import lnurlmint.signing as signing_module

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
            TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], p1=h
        )
    finally:
        loguru_logger.remove(sink_id)

    assert any(
        "sign_note" in msg and "missing signmessage permission" in msg
        for msg in captured
    ), captured
