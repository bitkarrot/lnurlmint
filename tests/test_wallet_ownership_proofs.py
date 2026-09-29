"""LUD-25 key-path notes (25.md): mint via comment=cp1<Q>, redeem via
k1=ck1<Q><sig> - a signature over the canonical spend transaction's
sighash for this mint's domain - and the cs1 certificates issued
alongside rotate/split/merge and the informational GET.

Ported from upstream's test_wallet_ownership_proofs.py, adapted to the
async direct-call harness.
"""

from hashlib import sha256
from os import urandom
from unittest.mock import MagicMock

import pytest
from coincurve import PrivateKey, PublicKey
from fastapi import BackgroundTasks

from lnurlmint import bech32m
from lnurlmint.signing import lightning_signed_message_digest, mint_pubkey
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    ck1_for,
    k1_id,
    mint_note,
)
from lnurlmint.views_lnurl import get_pay_callback, get_withdraw, get_withdraw_callback

_ck1 = ck1_for


def _mock_request() -> MagicMock:
    req = MagicMock()
    req.base_url = "http://test/"
    return req


def _note_keypair() -> tuple[PrivateKey, str]:
    """A fresh (sk, cp1<Q>) pair for a key-path note, the way a real
    WALLET would generate one (see 25.md's Key-path notes)."""
    sk = PrivateKey()
    pk_xonly = sk.public_key.format(compressed=True)[1:]
    return sk, bech32m.encode_cp1(pk_xonly)


async def _mint_cp1_note(node, amount_msat: int) -> tuple[PrivateKey, str]:
    from lnurlmint.crud import update_mint
    from lnurlmint.tests.conftest import TEST_WALLET

    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0)
    sk, cp1 = _note_keypair()
    response = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), amount_msat, comment=cp1
    )
    assert response.get("pr"), response
    import bolt11

    node.settled.add(bolt11.decode(response["pr"]).payment_hash)
    return sk, cp1


def test_ck1_matches_lud25_spec_test_vector_3():
    """Cross-implementation check against 25.md's own "Test vector 3:
    Key-path spend (ck1)": sk_0/pk_0 from Test Vector 1, at mint.example."""
    sk = PrivateKey(bytes.fromhex("944a9631dbda27cf989e27df8be7317a5a9dfb517a6b71358d175f58dd2dc99f"))
    assert sk.public_key.format(compressed=True)[1:].hex() == (
        "aad3a0e36c083eb0d2d92ec0860977dc46d10c952f31830e6443b1faa1997634"
    )
    assert _ck1(sk, "mint.example") == (
        "ck14tf6pcmvpqltp5ke9mqgvzthm3rdzry49uccxrnygwcl4gvewc6g8wlplczy60g4e5wp3dyyz6xr07fpse9flp0fy50"
        "cg4a4w64av6eprdctjlan6cu9dt38re9nu08etk5w3dmknlhuxzwcm3ycjysw3c9dpmpy"
    )


@pytest.mark.anyio
async def test_mint_cp1_note_and_check_value(node, db_setup):
    sk, cp1 = await _mint_cp1_note(node, 5000)
    k1 = _ck1(sk)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert data["minWithdrawable"] == data["maxWithdrawable"] == 5000


@pytest.mark.anyio
async def test_informational_get_includes_cs1_certificate(node, db_setup):
    sk, cp1 = await _mint_cp1_note(node, 5000)
    k1 = _ck1(sk)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert "c" in data
    decoded = bech32m.decode_cs1(data["c"])
    assert decoded is not None
    amount_msat, sig = decoded
    assert amount_msat == 5000
    digest = lightning_signed_message_digest(
        f"LNURLcash:{amount_msat}:{bech32m.decode_cp1(cp1).hex()}"
    )
    recovered = PublicKey.from_signature_and_message(sig, digest, hasher=None)
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    assert recovered.format(compressed=True).hex() == mint_pubkey(mint)


@pytest.mark.anyio
async def test_bearer_note_informational_get_includes_cs1_too(node, db_setup):
    """Every note is a taproot output key, so a bearer note - minted and
    redeemed in its hex short forms - gets a cs1 certificate over its Q
    exactly like a key-path note."""
    k1, note_id, mint = await mint_note(node, 5000)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    decoded = bech32m.decode_cs1(data["c"])
    assert decoded is not None
    amount_msat, sig = decoded
    assert amount_msat == 5000
    digest = lightning_signed_message_digest(
        f"LNURLcash:{amount_msat}:{k1_id(k1)}"
    )
    recovered = PublicKey.from_signature_and_message(sig, digest, hasher=None)
    assert recovered.format(compressed=True).hex() == mint_pubkey(mint)


@pytest.mark.anyio
async def test_ck1_signed_for_another_domain_is_rejected(node, db_setup):
    """A ck1 signs the canonical spend transaction for one mint's domain:
    one this mint never answers on is a replay from elsewhere."""
    sk, cp1 = await _mint_cp1_note(node, 5000)
    foreign = _ck1(sk, "other.example")
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=foreign)
    assert info == {"status": "ERROR", "reason": "Unknown note."}
    _, new_cp1 = _note_keypair()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[foreign], p1=new_cp1
    )
    assert data["status"] == "ERROR"
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=_ck1(sk))
    assert info["maxWithdrawable"] == 5000


@pytest.mark.anyio
async def test_recovery_scan_finds_a_note_via_p_equals_cp1(node, db_setup):
    """25.md's Seed & derivation: a WALLET recovering on a fresh install
    re-derives pk_0, pk_1, ... and GETs the withdraw LNURL with
    `?p=cp1<pk_i>` for each - this must find an outstanding cp1 note."""
    sk, cp1 = await _mint_cp1_note(node, 5000)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), p=cp1)
    assert data.get("maxWithdrawable") == 5000, data


@pytest.mark.anyio
async def test_recovery_scan_via_p_equals_cp1_also_includes_cs1_certificate(node, db_setup):
    """A `cs1` certificate is just this mint's signature over (pubkey,
    amount) - not a spend authorization - so a `?p=cp1<pk>` lookup (which
    names a cp1 note but proves no ownership) is exactly as safe a place
    to hand one out."""
    sk, cp1 = await _mint_cp1_note(node, 5000)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), p=cp1)
    assert "c" in data
    decoded = bech32m.decode_cs1(data["c"])
    assert decoded is not None
    amount_msat, sig = decoded
    assert amount_msat == 5000
    digest = lightning_signed_message_digest(
        f"LNURLcash:{amount_msat}:{bech32m.decode_cp1(cp1).hex()}"
    )
    recovered = PublicKey.from_signature_and_message(sig, digest, hasher=None)
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    assert recovered.format(compressed=True).hex() == mint_pubkey(mint)


@pytest.mark.anyio
async def test_recovery_scan_reports_a_spent_cp1_note_as_spent_not_unknown(node, db_setup):
    sk, cp1 = await _mint_cp1_note(node, 5000)
    k1 = _ck1(sk)
    new_sk, new_cp1 = _note_keypair()
    r = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], p1=new_cp1
    )
    assert r["status"] == "OK"

    data = await get_withdraw(TEST_MINT_ID, _mock_request(), p=cp1)
    assert data == {"status": "ERROR", "reason": "Note already spent."}


@pytest.mark.anyio
async def test_rotate_cp1_note_produces_cp1_output_with_certificate(node, db_setup):
    sk, cp1 = await _mint_cp1_note(node, 5000)
    k1 = _ck1(sk)
    new_sk, new_cp1 = _note_keypair()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], p1=new_cp1
    )
    assert data["status"] == "OK"
    decoded = bech32m.decode_cs1(data["c"])
    assert decoded is not None
    amount_msat, sig = decoded
    assert amount_msat == 5000
    new_pk = bech32m.decode_cp1(new_cp1)
    digest = lightning_signed_message_digest(
        f"LNURLcash:{amount_msat}:{new_pk.hex()}"
    )
    recovered = PublicKey.from_signature_and_message(sig, digest, hasher=None)
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)
    assert recovered.format(compressed=True).hex() == mint_pubkey(mint)

    # old note is burned, new one is spendable under the new key
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert info["reason"] == "Note already spent."
    new_k1 = _ck1(new_sk)
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=new_k1)
    assert info["maxWithdrawable"] == 5000


@pytest.mark.anyio
async def test_split_cp1_note_produces_two_cp1_outputs(node, db_setup):
    sk, cp1 = await _mint_cp1_note(node, 5000)
    k1 = _ck1(sk)
    out_sk, out_cp1 = _note_keypair()
    change_sk, change_cp1 = _note_keypair()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(),
        k1=[k1], amount=2000, p1=out_cp1, p2=change_cp1,
    )
    assert data["status"] == "OK"
    assert bech32m.decode_cs1(data["c"])[0] == 2000
    assert bech32m.decode_cs1(data["c2"])[0] == 3000

    out_k1 = _ck1(out_sk)
    change_k1 = _ck1(change_sk)
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=out_k1)
    assert info["maxWithdrawable"] == 2000
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=change_k1)
    assert info["maxWithdrawable"] == 3000


@pytest.mark.anyio
async def test_merge_mixes_legacy_and_cp1_notes(node, db_setup):
    legacy_k1, _, _ = await mint_note(node, 3000)
    sk, cp1 = await _mint_cp1_note(node, 2000)
    ck1 = _ck1(sk)
    out_sk, out_cp1 = _note_keypair()

    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(),
        k1=[legacy_k1, ck1], p1=out_cp1,
    )
    assert data["status"] == "OK"
    assert bech32m.decode_cs1(data["c"])[0] == 5000

    out_k1 = _ck1(out_sk)
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=out_k1)
    assert info["maxWithdrawable"] == 5000


@pytest.mark.anyio
async def test_bare_pubkey_cannot_redeem_a_cp1_note(node, db_setup):
    """A cp1 pubkey alone (no ck1 signature) must never be accepted as a
    spend-capable k1 - pk is public information, so accepting it as k1
    would let anyone burn any cp1 note they've merely seen."""
    sk, cp1 = await _mint_cp1_note(node, 5000)
    _, new_cp1 = _note_keypair()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[cp1], p1=new_cp1
    )
    assert data["status"] == "ERROR"


@pytest.mark.anyio
async def test_forged_ck1_signature_rejected(node, db_setup):
    sk, cp1 = await _mint_cp1_note(node, 5000)
    other_sk = PrivateKey()
    forged = _ck1(other_sk)  # a signature valid for a note that was never minted
    _, new_cp1 = _note_keypair()
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[forged], p1=new_cp1
    )
    assert data["status"] == "ERROR"


@pytest.mark.anyio
async def test_malformed_ck1_rejected(node, db_setup):
    """A ck1 string with a bad checksum, wrong HRP, or wrong length must
    fail the same way a malformed k1 does, never a 500."""
    _, new_cp1 = _note_keypair()
    bad = "ck1" + "q" * 110
    data = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[bad], p1=new_cp1
    )
    assert data["status"] == "ERROR"


@pytest.mark.anyio
async def test_comment_rejects_wrong_length_cp1_lookalike(node, db_setup):
    # a cp1-shaped string but wrong payload length must still be rejected
    bogus = bech32m.encode("cp", urandom(31))
    response = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), 5000, comment=bogus
    )
    assert response["status"] == "ERROR"


def _old_ck1s(sk: PrivateKey) -> list[str]:
    """The ck1 shapes older WALLETs signed, none of which a spend accepts
    any more: the pre-schnorr bare 65-byte recoverable signature, and the
    current Q||sig shape signed over a fixed message instead of the spend
    transaction's sighash (sha256("LNURLcash"), and the raw string)."""
    pk_xonly = sk.public_key.format(compressed=True)[1:]
    recoverable = sk.sign_recoverable(
        lightning_signed_message_digest("LNURLcash"), hasher=None
    )
    return [
        bech32m.encode("ck", recoverable),
        bech32m.encode("ck", pk_xonly + sk.sign_schnorr(sha256(b"LNURLcash").digest())),
        bech32m.encode(
            "ck", pk_xonly + sk.sign_schnorr(sha256(b"LNURLcash-legacy").digest())
        ),
    ]


@pytest.mark.anyio
async def test_old_ck1_shapes_are_refused(node, db_setup):
    sk, cp1 = await _mint_cp1_note(node, 5000)
    _, new_cp1 = _note_keypair()
    for old_k1 in _old_ck1s(sk):
        info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=old_k1)
        assert info == {"status": "ERROR", "reason": "Unknown note."}
        data = await get_withdraw_callback(
            TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[old_k1], p1=new_cp1
        )
        assert data["status"] == "ERROR"
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=_ck1(sk))
    assert info["maxWithdrawable"] == 5000
