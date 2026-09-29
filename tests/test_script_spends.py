"""LUD-25 script-path spends (`cw1`): a note locked to a leaf script under
its output key Q, redeemed by revealing that leaf, its control block and
a witness.

Ported from upstream's test_script_spends.py, adapted to the async
direct-call harness and the vendored spend/taproot modules. Every spend
here is built from scratch — leaf, taproot tweak, and a real BIP-342
signature over the canonical spend transaction's sighash — the way a
WALLET would build one.

Arbitrary script leaves need `lnurlcashkernel` (Bitcoin Core's own
interpreter, optional import); without it only the bearer preimage leaf
is verifiable, so kernel-dependent tests skip.
"""

import types
from dataclasses import dataclass
from os import urandom
from unittest.mock import MagicMock

import pytest
from coincurve import PrivateKey
from fastapi import BackgroundTasks

from lnurlmint import bech32m, spend
from lnurlmint.crud import get_note
from lnurlmint.taproot import NUMS_H, TAPLEAF_VERSION, tapleaf_hash, tweak
from lnurlmint.tests.conftest import (
    TEST_DOMAIN,
    TEST_MINT_ID,
    bearer_id,
    fresh_secret,
    mint_note,
)
from lnurlmint.views_lnurl import get_pay_callback, get_withdraw, get_withdraw_callback

DOMAIN = TEST_DOMAIN
AMOUNT = 20_000_000
LOCK = 1_800_000_000
CSV_4_UNITS = (1 << 22) | 4  # BIP-68 time-type, 4 * 512 s

needs_kernel = pytest.mark.skipif(
    not spend.kernel_available(), reason="arbitrary script-path spends need lnurlcashkernel"
)


def _xonly(key: PrivateKey) -> bytes:
    return key.public_key.format(compressed=True)[1:]


def _push_num(n: int) -> bytes:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "little")
    if raw[-1] & 0x80:
        raw += b"\x00"
    return bytes([len(raw)]) + raw


def _push_key(key: PrivateKey) -> bytes:
    return b"\x20" + _xonly(key)


@pytest.fixture
def owner() -> PrivateKey:
    return PrivateKey()


@pytest.fixture
def other() -> PrivateKey:
    return PrivateKey()


def pk_leaf(key: PrivateKey) -> bytes:
    return _push_key(key) + b"\xac"


def cltv_leaf(key: PrivateKey) -> bytes:
    return _push_num(LOCK) + b"\xb1\x75" + pk_leaf(key)


def csv_leaf(key: PrivateKey) -> bytes:
    return _push_num(CSV_4_UNITS) + b"\xb2\x75" + pk_leaf(key)


def two_of_two_leaf(a: PrivateKey, b: PrivateKey) -> bytes:
    """2-of-2 via CHECKSIGVERIFY: in no list of shapes, and needs none."""
    return _push_key(a) + b"\xad" + _push_key(b) + b"\xac"


def _nonce_leaf(script: bytes) -> bytes:
    """`script` behind a fresh pushed-and-dropped nonce: the same condition,
    a Q no other test shares."""
    return b"\x20" + urandom(32) + b"\x75" + script


def _mock_request() -> MagicMock:
    req = MagicMock()
    req.base_url = "http://test/"
    return req


@dataclass
class Locked:
    """A note locked to one leaf under NUMS: its Q, and a way to spend it."""

    leaf: bytes
    q: bytes
    control: bytes

    @property
    def cp1(self) -> str:
        return bech32m.encode_cp1(self.q)

    def cw1(
        self,
        signers: list[PrivateKey],
        *,
        locktime: int = 0,
        sequence: int = 0xFFFFFFFE,
        extra: list[bytes] | None = None,
        domain: str = DOMAIN,
    ) -> str:
        digest = spend.script_path_sighash(
            self.q, domain, self.leaf, locktime=locktime, sequence=sequence
        )
        # BIP-342 consumes the last key's signature first, so bottom to top
        # the stack is the signatures reversed, then anything else on top
        witness = [k.sign_schnorr(digest) for k in reversed(signers)] + (extra or [])
        return spend.encode_cw1(
            spend.Spend(locktime, sequence, self.leaf, self.control, tuple(witness))
        )


def _lock(leaf: bytes, version: int = TAPLEAF_VERSION) -> Locked:
    tweaked = tweak(NUMS_H, tapleaf_hash(leaf, version))
    assert tweaked is not None
    q, parity = tweaked
    return Locked(leaf, q, bytes([version | parity]) + NUMS_H)


def _at(monkeypatch: pytest.MonkeyPatch, now: int) -> None:
    monkeypatch.setattr(spend, "time", types.SimpleNamespace(time=lambda: float(now)))


async def _fund(node, locked: Locked) -> None:
    """Rotate a fresh bearer note into `locked` - as a WALLET locking value."""
    k1, _, _ = await mint_note(node, AMOUNT)
    response = await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(), k1=[k1], p1=locked.cp1
    )
    assert response.get("c"), response


async def _redeem(k1: str, p1: str | None = None) -> dict:
    _, h = fresh_secret()
    return await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(),
        k1=[k1], p1=p1 or h,
    )


def _refused(data: dict) -> bool:
    return data.get("status") == "ERROR"


@needs_kernel
@pytest.mark.parametrize("shape", ["pk", "two-of-two"])
@pytest.mark.anyio
async def test_lock_then_redeem_by_script_path(node, db_setup, owner, other, shape):
    leaf, signers = (pk_leaf(owner), [owner]) if shape == "pk" else (two_of_two_leaf(owner, other), [owner, other])
    locked = _lock(leaf)
    await _fund(node, locked)
    new_secret, h = fresh_secret()
    cw1 = locked.cw1(signers)

    response = await _redeem(cw1, h)
    assert not _refused(response), response
    assert bech32m.decode_cs1(response["c"])[0] == AMOUNT
    assert await get_note(locked.q.hex(), TEST_MINT_ID) is None  # burned
    assert (await get_note(bearer_id(h), TEST_MINT_ID)).amount_msat == AMOUNT

    # LUD-25 "Retrying a mutation": the same request again replays its result
    assert await _redeem(cw1, h) == response
    # ...but a different p1 is a genuine double-spend
    assert _refused(await _redeem(cw1))


@needs_kernel
@pytest.mark.anyio
async def test_informational_get_verifies_the_cw1_and_certifies(node, db_setup, owner):
    locked = _lock(pk_leaf(owner))
    await _fund(node, locked)
    data = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=locked.cw1([owner]))
    assert data["minWithdrawable"] == data["maxWithdrawable"] == AMOUNT
    assert bech32m.decode_cs1(data["c"])[0] == AMOUNT
    assert (await get_note(locked.q.hex(), TEST_MINT_ID)).amount_msat == AMOUNT  # informational: not burned


@needs_kernel
@pytest.mark.anyio
async def test_cltv_is_refused_before_its_locktime_with_the_specific_reason(
    node, db_setup, owner, monkeypatch
):
    """A cw1 that opens a real note, but whose time claim the mint's clock
    hasn't reached, says so specifically - never the "invalid or already
    spent" wording a wrong or burned spend gets."""
    locked = _lock(cltv_leaf(owner))
    await _fund(node, locked)
    cw1 = locked.cw1([owner], locktime=LOCK)

    _at(monkeypatch, LOCK - 1)
    response = await _redeem(cw1)
    assert _refused(response) and "future" in response["reason"]
    assert "already spent" not in response["reason"].lower()
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=cw1)
    assert _refused(info) and "future" in info["reason"]
    assert (await get_note(locked.q.hex(), TEST_MINT_ID)).amount_msat == AMOUNT  # never burned
    # its value is still visible by its public key
    pub = await get_withdraw(TEST_MINT_ID, _mock_request(), p=locked.cp1)
    assert pub["maxWithdrawable"] == AMOUNT

    _at(monkeypatch, LOCK)
    assert not _refused(await _redeem(cw1))


@needs_kernel
@pytest.mark.anyio
async def test_csv_counts_from_when_the_mint_credited_the_note(node, db_setup, owner, monkeypatch):
    locked = _lock(csv_leaf(owner))
    await _fund(node, locked)
    credited_at = (await get_note(locked.q.hex(), TEST_MINT_ID)).locked_at
    cw1 = locked.cw1([owner], sequence=CSV_4_UNITS)

    _at(monkeypatch, credited_at + 2047)  # one second short of 4 * 512
    assert _refused(await _redeem(cw1))
    _at(monkeypatch, credited_at + 2048)
    assert not _refused(await _redeem(cw1))


@needs_kernel
@pytest.mark.anyio
async def test_the_signature_commits_to_the_claimed_time(node, db_setup, owner, monkeypatch):
    locked = _lock(cltv_leaf(owner))
    await _fund(node, locked)
    honest = spend.decode_cw1(locked.cw1([owner], locktime=LOCK))
    # claim an earlier locktime than the one that was signed
    forged = spend.encode_cw1(
        spend.Spend(LOCK - 100, honest.sequence, honest.script, honest.control_block, honest.witness)
    )
    _at(monkeypatch, LOCK - 50)
    assert _refused(await _redeem(forged))
    assert (await get_note(locked.q.hex(), TEST_MINT_ID)).amount_msat == AMOUNT


@needs_kernel
@pytest.mark.anyio
async def test_a_tampered_witness_is_refused(node, db_setup, owner):
    locked = _lock(pk_leaf(owner))
    await _fund(node, locked)
    honest = spend.decode_cw1(locked.cw1([owner]))
    sig = bytearray(honest.witness[0])
    sig[5] ^= 1
    forged = spend.encode_cw1(
        spend.Spend(honest.locktime, honest.sequence, honest.script, honest.control_block, (bytes(sig),))
    )
    assert _refused(await _redeem(forged))
    assert (await get_note(locked.q.hex(), TEST_MINT_ID)).amount_msat == AMOUNT


@needs_kernel
@pytest.mark.anyio
async def test_a_cw1_signed_for_another_domain_is_refused(node, db_setup, owner, other):
    locked = _lock(pk_leaf(owner))
    await _fund(node, locked)
    assert _refused(await _redeem(locked.cw1([owner], domain="other.example")))
    assert (await get_note(locked.q.hex(), TEST_MINT_ID)).amount_msat == AMOUNT


@needs_kernel
@pytest.mark.anyio
async def test_a_leaf_for_another_note_opens_nothing(node, db_setup, owner, other):
    """A cw1 derives its own Q from its control block: another leaf names
    another note, one never funded here - the ambiguous generic reason."""
    locked = _lock(pk_leaf(owner))
    await _fund(node, locked)
    elsewhere = _lock(pk_leaf(other))
    assert await _redeem(elsewhere.cw1([other])) == {"status": "ERROR", "reason": "Invalid or already spent k1."}
    assert (await get_note(locked.q.hex(), TEST_MINT_ID)).amount_msat == AMOUNT


@needs_kernel
@pytest.mark.anyio
async def test_an_op_success_leaf_is_refused_even_though_consensus_would_accept(node, db_setup):
    locked = _lock(_nonce_leaf(b"\x50"))  # OP_SUCCESS80
    await _fund(node, locked)
    response = await _redeem(locked.cw1([]))
    assert _refused(response) and "OP_SUCCESS" in response["reason"]
    assert (await get_note(locked.q.hex(), TEST_MINT_ID)).amount_msat == AMOUNT


@needs_kernel
@pytest.mark.anyio
async def test_an_unknown_leaf_version_is_refused(node, db_setup):
    locked = _lock(_nonce_leaf(b"\x51"), version=0xC2)
    await _fund(node, locked)
    response = await _redeem(locked.cw1([]))
    assert _refused(response) and "leaf version" in response["reason"]


@needs_kernel
@pytest.mark.anyio
async def test_mint_straight_to_a_script_path_note(node, db_setup, owner):
    locked = _lock(pk_leaf(owner))
    response = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), AMOUNT, comment=locked.cp1
    )
    assert response.get("pr"), response
    import bolt11

    node.settled.add(bolt11.decode(response["pr"]).payment_hash)
    assert not _refused(await _redeem(locked.cw1([owner])))


@pytest.mark.anyio
async def test_a_bearer_note_spends_in_either_form(node, db_setup):
    """The hex preimage is the short form of the bearer note's cw1: the
    mint builds the same spend from it, so the full cw1 works just as
    well - and this one needs no kernel."""
    k1, _, _ = await mint_note(node, 5000)
    full = spend.encode_cw1(spend.preimage_spend(bytes.fromhex(k1)))
    assert full.startswith("cw1")
    info = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=full)
    assert info["maxWithdrawable"] == 5000
    assert not _refused(await _redeem(full))
    assert _refused(await _redeem(k1))  # the same note, already spent
