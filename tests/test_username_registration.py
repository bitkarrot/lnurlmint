"""LUD-25 Seed & derivation's cx1 registration (views_lnurl's
POST/DELETE /p/{mint_id}/{username}): a WALLET claims a Lightning
Address username against its own watch-only branch, and this mint
auto-mints cp1 notes off it directly. Every register/unregister call
needs an ownership-proof signature - a fresh claim over the branch being
submitted right now, an overwrite or delete over whichever branch is
already on file (see views_lnurl._owns_branch and upsert_username for
why those differ) - there is no proof-free case.

Ported from upstream's test_username_registration.py, adapted to the
extension's async direct-call harness: feature toggles are per-mint
(update_mint), the mint's own fixed identity is mint.username.
"""

import json
from unittest.mock import MagicMock

import bech32 as bech32_pkg
import pytest
from coincurve import PrivateKey
from fastapi import BackgroundTasks

from lnurlmint import bech32m, derivation
from lnurlmint.crud import update_mint, username_branch
from lnurlmint.signing import verify_register_signature
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    fresh_branch,
    mint_note,
    note_value_by_p,
    register_sig,
)
from lnurlmint.views_lnurl import (
    delete_registered_username,
    get_nip05,
    get_pay_callback_for_username,
    get_payrequest,
    get_payrequest_for_username,
    get_withdraw,
    get_withdraw_callback,
    upsert_registered_username,
)

# a well-formed-length but cryptographically meaningless signature
_BOGUS_SIG = "00" * 64


@pytest.fixture
async def _no_min_mint(db_setup):
    """The tests that pay 5000 msat through a callback would be rejected
    by the test mint's default min_mint_msat=10000 floor."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, min_mint_msat=0)


def _mock_request() -> MagicMock:
    req = MagicMock()
    req.base_url = "http://test/"
    return req


def _npub() -> tuple[bytes, str]:
    """A fresh (pubkey, npub) pair - NIP-19's classic (non-bech32m) bech32
    encoding, built straight off the `bech32` package rather than
    lnurlmint.bech32m (whose encode() is bech32m-only, wrong checksum for
    an npub - see bech32m.decode_npub)."""
    pubkey = PrivateKey().public_key.format(compressed=True)[1:]
    data = bech32_pkg.convertbits(pubkey, 8, 5, True)
    assert data is not None
    return pubkey, bech32_pkg.bech32_encode("npub", data)


async def _registered_lnurlp(username: str) -> dict:
    return await get_payrequest_for_username(TEST_MINT_ID, username, _mock_request())


async def _note_value(node_id_hex: str) -> int | None:
    """Value of the outstanding note with id `note_id_hex` - via the
    informational GET's `p` lookup, which also triggers lazy
    settle-on-first-lookup materialization."""
    return await note_value_by_p(bech32m.encode_cp1(bytes.fromhex(node_id_hex)))


@pytest.mark.anyio
async def test_register_claims_a_username(node, db_setup):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "alice")
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "alice", cx1=cx1, sig=sig
    )
    assert resp == {"status": "OK"}
    assert await username_branch(TEST_MINT_ID, "alice") == bech32m.decode_cx1(cx1).hex()


@pytest.mark.anyio
async def test_register_rejects_a_proof_signed_for_a_different_domain(node, db_setup):
    """A proof signed for some OTHER mint's domain must NOT verify against
    this one — the cross-mint replay _owns_branch's domain binding
    exists to close."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "zoe", domain="other-mint.example")
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "zoe", cx1=cx1, sig=sig
    )
    assert resp["status"] == "ERROR"
    assert await username_branch(TEST_MINT_ID, "zoe") is None


def test_register_and_unregister_proofs_match_lud25_spec_test_vector_2():
    """Cross-implementation check against 25.md's own published "Test
    vector 2" registration proofs — asserts verify_register_signature
    against the literal signature bytes 25.md publishes."""
    pk_0 = bytes.fromhex("01fee34e378bf66de6afa1bfa6e30f5c89551fd92bc1b089dca93c52b7ab61bc")
    domain = "cash.example.com"
    register_sig_hex = (
        "9169a81db3372d8bb8a080f271f8036192131d4ed02596c0baa181613fdc5d6e17b3230b01f510a759fdb6c46b53671e5"
        "7678f57ac0a6a3deb6300761225adc7"
    )
    unregister_sig_hex = (
        "fcc6a96f560d6505bfc475d8c2d4383047f2ece6593412af9b2834913af60f108b6e194cef31c377a23a051f8c80c1660e"
        "fc2313b7876a2f80bc1f0f423c7835"
    )
    assert verify_register_signature(pk_0, register_sig_hex, "register", domain, "alice")
    assert verify_register_signature(pk_0, unregister_sig_hex, "unregister", domain, "alice")

    # a register proof is never valid as an unregister one (or vice versa)
    assert not verify_register_signature(pk_0, register_sig_hex, "unregister", domain, "alice")
    assert not verify_register_signature(pk_0, unregister_sig_hex, "register", domain, "alice")

    # nor does either proof replay against a different SERVICE domain
    assert not verify_register_signature(pk_0, register_sig_hex, "register", "mint.example", "alice")
    assert not verify_register_signature(pk_0, unregister_sig_hex, "unregister", "mint.example", "alice")


@pytest.mark.anyio
async def test_register_fresh_claim_with_wrong_signature_rejected(node, db_setup):
    """A fresh claim's proof must be over the NEW cx1 being submitted -
    signed by any OTHER branch's key, it's rejected outright."""
    _, cx1 = fresh_branch()
    wrong_key, _ = fresh_branch()
    sig = register_sig(wrong_key, "register", "walter")
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "walter", cx1=cx1, sig=sig
    )
    assert resp["status"] == "ERROR"
    assert await username_branch(TEST_MINT_ID, "walter") is None


@pytest.mark.anyio
async def test_overwrite_without_ownership_proof_rejected(node, db_setup):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "bob")
    assert (await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "bob", cx1=cx1, sig=sig
    ))["status"] == "OK"
    _, cx1_2 = fresh_branch()
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "bob", cx1=cx1_2, sig=_BOGUS_SIG
    )
    assert resp["status"] == "ERROR"
    # rejected outright: the original branch is untouched
    assert await username_branch(TEST_MINT_ID, "bob") == bech32m.decode_cx1(cx1).hex()


@pytest.mark.anyio
async def test_overwrite_with_wrong_signature_rejected(node, db_setup):
    key, cx1 = fresh_branch()
    sig0 = register_sig(key, "register", "carol")
    await upsert_registered_username(_mock_request(), TEST_MINT_ID, "carol", cx1=cx1, sig=sig0)
    wrong_key, _ = fresh_branch()
    sig = register_sig(wrong_key, "register", "carol")
    _, cx1_2 = fresh_branch()
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "carol", cx1=cx1_2, sig=sig
    )
    assert resp["status"] == "ERROR"
    assert await username_branch(TEST_MINT_ID, "carol") == bech32m.decode_cx1(cx1).hex()


@pytest.mark.anyio
async def test_overwrite_with_valid_ownership_proof_replaces_the_branch(node, db_setup):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "dana")
    await upsert_registered_username(_mock_request(), TEST_MINT_ID, "dana", cx1=cx1, sig=sig)
    # same sig: the fresh claim proved control of `cx1`, this overwrite
    # proves continued control of that SAME branch still on file
    _, cx1_2 = fresh_branch()
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "dana", cx1=cx1_2, sig=sig
    )
    assert resp == {"status": "OK"}
    assert await username_branch(TEST_MINT_ID, "dana") == bech32m.decode_cx1(cx1_2).hex()


@pytest.mark.anyio
async def test_overwrite_omitting_npub_clears_it(node, db_setup):
    key, cx1 = fresh_branch()
    _, npub = _npub()
    sig = register_sig(key, "register", "edna")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "edna", cx1=cx1, npub=npub, sig=sig
    )
    assert (await get_nip05(TEST_MINT_ID, name="edna"))["names"]

    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "edna", cx1=cx1, sig=sig
    )
    assert resp == {"status": "OK"}
    assert await get_nip05(TEST_MINT_ID, name="edna") == {"names": {}}


@pytest.mark.anyio
async def test_register_rejects_reserved_username(node, db_setup):
    _, cx1 = fresh_branch()
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "testuser", cx1=cx1, sig=_BOGUS_SIG
    )
    assert resp["status"] == "ERROR"
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "_", cx1=cx1, sig=_BOGUS_SIG
    )
    assert resp["status"] == "ERROR"


@pytest.mark.anyio
async def test_register_rejects_malformed_cx1(node, db_setup):
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "finn", cx1="notbech32m", sig=_BOGUS_SIG
    )
    assert resp["status"] == "ERROR"


@pytest.mark.anyio
async def test_register_with_npub_serves_nip05(node, db_setup):
    key, cx1 = fresh_branch()
    pubkey, npub = _npub()
    sig = register_sig(key, "register", "mallory")
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "mallory", cx1=cx1, npub=npub, sig=sig
    )
    assert resp == {"status": "OK"}

    nip05 = await get_nip05(TEST_MINT_ID, name="mallory")
    assert nip05 == {"names": {"mallory": pubkey.hex()}}


@pytest.mark.anyio
async def test_register_without_npub_has_no_nip05_name(node, db_setup):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "nora")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "nora", cx1=cx1, sig=sig
    )
    assert await get_nip05(TEST_MINT_ID, name="nora") == {"names": {}}


@pytest.mark.anyio
async def test_register_rejects_malformed_npub(node, db_setup):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "oscarnpub")
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "oscarnpub", cx1=cx1, npub="notanpub", sig=sig
    )
    assert resp["status"] == "ERROR"
    assert await username_branch(TEST_MINT_ID, "oscarnpub") is None


@pytest.mark.anyio
async def test_nip05_unknown_name_returns_empty_names(node, db_setup):
    assert await get_nip05(TEST_MINT_ID, name="nobody") == {"names": {}}


@pytest.mark.anyio
async def test_nip05_with_no_name_returns_empty_names(node, db_setup):
    """Never dumps the whole directory - only the one name asked about,
    and no `name` at all asks about none."""
    key, cx1 = fresh_branch()
    _, npub = _npub()
    sig = register_sig(key, "register", "petra")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "petra", cx1=cx1, npub=npub, sig=sig
    )
    assert await get_nip05(TEST_MINT_ID) == {"names": {}}


@pytest.mark.anyio
async def test_nip05_lookup_is_case_insensitive_but_echoes_the_queried_name(node, db_setup):
    key, cx1 = fresh_branch()
    pubkey, npub = _npub()
    sig = register_sig(key, "register", "quinn")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "Quinn", cx1=cx1, npub=npub, sig=sig
    )
    nip05 = await get_nip05(TEST_MINT_ID, name="QUINN")
    assert nip05 == {"names": {"QUINN": pubkey.hex()}}


@pytest.mark.anyio
async def test_nip05_hidden_while_username_registration_disabled(node, db_setup):
    key, cx1 = fresh_branch()
    _, npub = _npub()
    sig = register_sig(key, "register", "ray")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "ray", cx1=cx1, npub=npub, sig=sig
    )
    await update_mint(TEST_MINT_ID, TEST_WALLET, registration_enabled=False)
    assert await get_nip05(TEST_MINT_ID, name="ray") == {"names": {}}


@pytest.mark.anyio
async def test_nip05_404s_while_nip05_disabled(node, db_setup):
    key, cx1 = fresh_branch()
    _, npub = _npub()
    sig = register_sig(key, "register", "sam")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "sam", cx1=cx1, npub=npub, sig=sig
    )
    await update_mint(TEST_MINT_ID, TEST_WALLET, nip05_enabled=False)
    assert await get_nip05(TEST_MINT_ID, name="sam") == {"status": "ERROR", "reason": "Not found"}


@pytest.mark.anyio
async def test_registration_rejects_npub_while_nip05_disabled(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, nip05_enabled=False)
    key, cx1 = fresh_branch()
    _, npub = _npub()
    sig = register_sig(key, "register", "tina")
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "tina", cx1=cx1, npub=npub, sig=sig
    )
    assert resp == {"status": "ERROR", "reason": "npub registration (NIP-05) is disabled on this mint."}
    assert await username_branch(TEST_MINT_ID, "tina") is None


@pytest.mark.anyio
async def test_registration_without_npub_still_works_while_nip05_disabled(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, nip05_enabled=False)
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "uma")
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "uma", cx1=cx1, sig=sig
    )
    assert resp == {"status": "OK"}
    assert await username_branch(TEST_MINT_ID, "uma") is not None


@pytest.mark.anyio
async def test_unregistered_username_errors(node, db_setup):
    resp = await _registered_lnurlp("nobody")
    assert resp == {"status": "ERROR", "reason": "Unknown user."}


@pytest.mark.anyio
async def test_registered_lnaddress_callback_carries_username(node, db_setup):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "dave")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "dave", cx1=cx1, sig=sig
    )
    data = await _registered_lnurlp("dave")
    assert data["callback"].endswith("/lnurlmint/p/testmint/dave")
    assert data["tag"] == "payRequest"


@pytest.mark.anyio
async def test_registered_lnaddress_metadata_advertises_xpub_for_internal_transfers(
    node, db_setup
):
    """25.md's Internal mint transfers: a registered username's payRequest
    metadata carries its own `cx1`, so a payer's WALLET already holding a
    note on this mint can skip Lightning entirely - deriving the next note
    key itself rather than paying an invoice."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "gina")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "gina", cx1=cx1, sig=sig
    )
    metadata = json.loads((await _registered_lnurlp("gina"))["metadata"])
    assert ["text/cpub", f"{cx1}:0"] in metadata


@pytest.mark.anyio
async def test_fixed_identity_lnaddress_metadata_has_no_xpub(node, db_setup):
    """This mint's own fixed identity has no watch-only branch to advertise
    - only a registered (cx1-backed) username does."""
    metadata = json.loads(
        (await get_payrequest(TEST_MINT_ID, _mock_request()))["metadata"]
    )
    assert not any(entry[0] == "text/cpub" for entry in metadata)


@pytest.mark.anyio
async def test_xpub_index_hint_advances_after_an_automint(node, db_setup, _no_min_mint):
    """claim_next_index reserves and persists past the index it hands out
    at callback (invoice-creation) time, not at settlement - so the
    advertised hint must already reflect that on the very next lookup,
    even before this particular invoice is paid."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "hana")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "hana", cx1=cx1, sig=sig
    )
    metadata = json.loads((await _registered_lnurlp("hana"))["metadata"])
    assert ["text/cpub", f"{cx1}:0"] in metadata

    pay_response = await get_pay_callback_for_username(
        TEST_MINT_ID, "hana", _mock_request(), amount=5000
    )
    assert pay_response.get("pr")

    metadata = json.loads((await _registered_lnurlp("hana"))["metadata"])
    assert ["text/cpub", f"{cx1}:1"] in metadata


@pytest.mark.anyio
async def test_internal_transfer_skips_lightning_via_rotate(node, db_setup):
    """The whole point of Internal mint transfers: a payer already holding
    a note on this mint reads `ivan`'s `cx1`/index hint off his payRequest
    metadata and lands a note directly on his branch via an ordinary
    rotate - never paying a Lightning invoice to `ivan`'s address at all."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "ivan")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "ivan", cx1=cx1, sig=sig
    )
    metadata = json.loads((await _registered_lnurlp("ivan"))["metadata"])
    xpub_entry = next(entry for entry in metadata if entry[0] == "text/cpub")
    advertised_cx1, index_hint = xpub_entry[1].rsplit(":", 1)
    assert advertised_cx1 == cx1
    decoded_branch = bech32m.decode_cx1(advertised_cx1)
    branch_point, chain_code = decoded_branch[:32], decoded_branch[32:]

    pk_i = derivation.derive_pubkey(
        branch_point, chain_code, derivation.PURPOSE_LIGHTNING_ADDRESS, int(index_hint)
    )
    cp1 = bech32m.encode_cp1(pk_i)

    # the sender rotates an ordinary note directly onto ivan's derived key
    k1, _, _ = await mint_note(node, 5000)
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[k1], p1=cp1
    )
    assert resp["status"] == "OK"
    assert await _note_value(pk_i.hex()) == 5000

    # ivan's own advertised next_index is untouched by this - it's only a
    # hint, never reserved by anything other than his own auto-mint path
    metadata = json.loads((await _registered_lnurlp("ivan"))["metadata"])
    assert ["text/cpub", f"{cx1}:{index_hint}"] in metadata


@pytest.mark.anyio
async def test_internal_transfer_to_a_stale_index_is_rejected_like_any_collision(
    node, db_setup
):
    """`i` is only a hint (25.md): if two senders race for the same
    advertised index, the second one must fail cleanly, exactly like any
    other already-in-use p1, never double-credit or overwrite."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "jack")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "jack", cx1=cx1, sig=sig
    )
    decoded_branch = bech32m.decode_cx1(cx1)
    pk0 = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 0
    )
    cp1 = bech32m.encode_cp1(pk0)

    first_k1, _, _ = await mint_note(node, 3000)
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[first_k1], p1=cp1
    )
    assert r["status"] == "OK"

    second_k1, _, _ = await mint_note(node, 2000)
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(), k1=[second_k1], p1=cp1
    )
    assert resp == {"status": "ERROR", "reason": "already in use"}
    # the first transfer's note is untouched, the second sender's note
    # was never burned
    assert await _note_value(pk0.hex()) == 3000
    info = await get_withdraw(TEST_MINT_ID, MagicMock(), k1=second_k1)
    assert info["maxWithdrawable"] == 2000


@pytest.mark.anyio
async def test_paying_registered_address_with_no_comment_automints(node, db_setup, _no_min_mint):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "erin")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "erin", cx1=cx1, sig=sig
    )
    decoded_branch = bech32m.decode_cx1(cx1)

    pay_response = await get_pay_callback_for_username(
        TEST_MINT_ID, "erin", _mock_request(), amount=5000
    )
    assert pay_response.get("pr"), pay_response
    import bolt11

    node.settled.add(bolt11.decode(pay_response["pr"]).payment_hash)

    expected_id = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 0
    ).hex()
    assert await _note_value(expected_id) == 5000


@pytest.mark.anyio
async def test_hex_lookup_of_an_automint_key_leaves_the_note_alone(node, db_setup, _no_min_mint):
    """A 64-hex `p` is a bearer note's short form: it names that bearer
    note's Q, never the cp1 note stored under the same 32 bytes."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "hexa")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "hexa", cx1=cx1, sig=sig
    )
    decoded_branch = bech32m.decode_cx1(cx1)

    pay_response = await get_pay_callback_for_username(
        TEST_MINT_ID, "hexa", _mock_request(), amount=5000
    )
    import bolt11

    node.settled.add(bolt11.decode(pay_response["pr"]).payment_hash)

    expected_id = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 0
    ).hex()
    info = await get_withdraw(TEST_MINT_ID, MagicMock(), p=expected_id)
    assert info["reason"] == "Unknown note."
    assert await _note_value(expected_id) == 5000


@pytest.mark.anyio
async def test_second_automint_payment_uses_the_next_index(node, db_setup, _no_min_mint):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "frank")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "frank", cx1=cx1, sig=sig
    )
    decoded_branch = bech32m.decode_cx1(cx1)

    import bolt11

    for _ in range(2):
        pay_response = await get_pay_callback_for_username(
            TEST_MINT_ID, "frank", _mock_request(), amount=5000
        )
        assert pay_response.get("pr")
        node.settled.add(bolt11.decode(pay_response["pr"]).payment_hash)

    pk0 = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 0
    ).hex()
    pk1 = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 1
    ).hex()
    assert await _note_value(pk0) == 5000
    assert await _note_value(pk1) == 5000


@pytest.mark.anyio
async def test_automint_skips_an_index_already_taken_by_a_manual_mint(node, db_setup, _no_min_mint):
    """LUD-25 Seed & derivation's own race-avoidance: if index 0 is already
    outstanding (a WALLET's own manual mint got there first), the
    auto-mint path must skip to the next free index rather than
    double-credit or collide."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "grace")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "grace", cx1=cx1, sig=sig
    )
    decoded_branch = bech32m.decode_cx1(cx1)
    pk0 = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 0
    )

    from lnurlmint.views_lnurl import get_pay_callback

    manual = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), 1000, comment=bech32m.encode_cp1(pk0)
    )
    assert manual.get("pr")
    import bolt11

    node.settled.add(bolt11.decode(manual["pr"]).payment_hash)
    assert await _note_value(pk0.hex()) == 1000

    pay_response = await get_pay_callback_for_username(
        TEST_MINT_ID, "grace", _mock_request(), amount=5000
    )
    assert pay_response.get("pr")
    node.settled.add(bolt11.decode(pay_response["pr"]).payment_hash)

    pk1 = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 1
    )
    assert await _note_value(pk1.hex()) == 5000


@pytest.mark.anyio
async def test_comment_is_still_honored_for_registered_username(node, db_setup, _no_min_mint):
    """The address owner minting for themselves with a specific key
    already in hand overrides auto-derivation."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "henry")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "henry", cx1=cx1, sig=sig
    )
    sk = PrivateKey()
    cp1 = bech32m.encode_cp1(sk.public_key.format(compressed=True)[1:])

    pay_response = await get_pay_callback_for_username(
        TEST_MINT_ID, "henry", _mock_request(), amount=5000, comment=cp1
    )
    assert pay_response.get("pr")
    import bolt11

    node.settled.add(bolt11.decode(pay_response["pr"]).payment_hash)
    assert await _note_value(bech32m.decode_cp1(cp1).hex()) == 5000


@pytest.mark.anyio
async def test_ordinary_lud12_comment_automints_for_registered_username(node, db_setup, _no_min_mint):
    """A payer's WALLET sending a plain human LUD-12 message (not a note
    ref) must not block minting - it's ignored and the payment still
    auto-mints on the username's own branch, same as no comment at all."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "lenny")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "lenny", cx1=cx1, sig=sig
    )
    decoded_branch = bech32m.decode_cx1(cx1)

    pay_response = await get_pay_callback_for_username(
        TEST_MINT_ID, "lenny", _mock_request(), amount=5000, comment="gm!"
    )
    assert pay_response.get("pr"), pay_response
    import bolt11

    node.settled.add(bolt11.decode(pay_response["pr"]).payment_hash)

    expected_id = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 0
    ).hex()
    assert await _note_value(expected_id) == 5000


@pytest.mark.anyio
async def test_empty_lud12_comment_automints_for_registered_username(node, db_setup, _no_min_mint):
    """Wallets commonly send `comment=` when the payer leaves the optional
    comment box empty. It must behave like an omitted comment."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "mabel")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "mabel", cx1=cx1, sig=sig
    )
    decoded_branch = bech32m.decode_cx1(cx1)

    pay_response = await get_pay_callback_for_username(
        TEST_MINT_ID, "mabel", _mock_request(), amount=5000, comment=""
    )
    assert pay_response.get("pr"), pay_response
    import bolt11

    node.settled.add(bolt11.decode(pay_response["pr"]).payment_hash)

    expected_id = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 0
    ).hex()
    assert await _note_value(expected_id) == 5000


@pytest.mark.anyio
async def test_username_registration_disabled_404s_register(node, db_setup):
    await update_mint(TEST_MINT_ID, TEST_WALLET, registration_enabled=False)
    _, cx1 = fresh_branch()
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "iris", cx1=cx1, sig=_BOGUS_SIG
    )
    assert resp == {"status": "ERROR", "reason": "Not found"}


@pytest.mark.anyio
async def test_username_registration_disabled_hides_registered_address(node, db_setup):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "jill")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "jill", cx1=cx1, sig=sig
    )
    await update_mint(TEST_MINT_ID, TEST_WALLET, registration_enabled=False)
    resp = await _registered_lnurlp("jill")
    assert resp == {"status": "ERROR", "reason": "Unknown user."}


@pytest.mark.anyio
async def test_registration_lowercases_a_mixed_case_username(node, db_setup):
    """A registered username is always stored normalized - 'Kevin'
    registers as 'kevin', so every lookup site (which also lowercases its
    own input) resolves it the same way regardless of how a client
    capitalized either side."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "kevin")
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "Kevin", cx1=cx1, sig=sig
    )
    assert resp == {"status": "OK"}
    assert await username_branch(TEST_MINT_ID, "kevin") == bech32m.decode_cx1(cx1).hex()
    assert await username_branch(TEST_MINT_ID, "Kevin") is None  # stored lowercase


@pytest.mark.anyio
async def test_lnaddress_lookup_is_case_insensitive(node, db_setup):
    """A payer's client capitalizing the local-part differently than how
    it was registered (e.g. Alice@host vs alice@host) must still resolve -
    LUD-16 local-parts are conventionally case-insensitive."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "liam")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "liam", cx1=cx1, sig=sig
    )
    lower = await _registered_lnurlp("liam")
    mixed = await _registered_lnurlp("Liam")
    upper = await _registered_lnurlp("LIAM")
    assert lower["tag"] == mixed["tag"] == upper["tag"] == "payRequest"


@pytest.mark.anyio
async def test_registered_lnaddress_case_insensitive_duplicate_rejected(node, db_setup):
    """Registering 'Noah' after 'noah' is already taken normalizes to the
    same row, so it hits the overwrite path - and without an ownership
    proof for the branch already on file, that's rejected, not a silent
    second, differently-cased identity for the same logical username."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "noah")
    assert (await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "noah", cx1=cx1, sig=sig
    ))["status"] == "OK"
    _, cx1_2 = fresh_branch()
    resp = await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "Noah", cx1=cx1_2, sig=_BOGUS_SIG
    )
    assert resp["status"] == "ERROR"
    assert await username_branch(TEST_MINT_ID, "noah") == bech32m.decode_cx1(cx1).hex()


@pytest.mark.anyio
async def test_mint_username_config_is_case_insensitive(node, db_setup):
    """This mint's own fixed identity (mint.username) resolves the same
    way regardless of how a payer's client capitalized it."""
    assert (await get_payrequest_for_username(
        TEST_MINT_ID, "TESTUSER", _mock_request()
    ))["tag"] == "payRequest"
    assert (await get_payrequest_for_username(
        TEST_MINT_ID, "Testuser", _mock_request()
    ))["tag"] == "payRequest"


@pytest.mark.anyio
async def test_automint_works_with_mixed_case_username_in_callback(node, db_setup, _no_min_mint):
    """The exact bug this guards: the callback URL carries whatever case
    the lnaddress lookup was queried with, and the auto-mint path
    (claim_next_index) must still find the registered branch's row even
    though it was stored lowercase."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "oscar")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "oscar", cx1=cx1, sig=sig
    )
    decoded_branch = bech32m.decode_cx1(cx1)

    lnaddress = await _registered_lnurlp("Oscar")
    assert lnaddress["callback"].endswith("/lnurlmint/p/testmint/Oscar")

    pay_response = await get_pay_callback_for_username(
        TEST_MINT_ID, "Oscar", _mock_request(), amount=5000
    )
    assert pay_response.get("pr"), pay_response
    import bolt11

    node.settled.add(bolt11.decode(pay_response["pr"]).payment_hash)

    expected_pk = derivation.derive_pubkey(
        decoded_branch[:32], decoded_branch[32:], derivation.PURPOSE_LIGHTNING_ADDRESS, 0
    )
    assert await _note_value(expected_pk.hex()) == 5000


@pytest.mark.anyio
async def test_delete_requires_ownership_proof(node, db_setup):
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "percy")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "percy", cx1=cx1, sig=sig
    )
    resp = await delete_registered_username(
        _mock_request(), TEST_MINT_ID, "percy", sig=_BOGUS_SIG
    )
    assert resp["status"] == "ERROR"
    assert await username_branch(TEST_MINT_ID, "percy") is not None


@pytest.mark.anyio
async def test_delete_with_valid_signature_frees_the_username(node, db_setup):
    key, cx1 = fresh_branch()
    sig0 = register_sig(key, "register", "quincy")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "quincy", cx1=cx1, sig=sig0
    )
    sig = register_sig(key, "unregister", "quincy")
    resp = await delete_registered_username(
        _mock_request(), TEST_MINT_ID, "quincy", sig=sig
    )
    assert resp == {"status": "OK"}
    assert await username_branch(TEST_MINT_ID, "quincy") is None

    # freed: anyone can claim it again - still needs to prove control of
    # the NEW branch being submitted
    key2, cx1_2 = fresh_branch()
    sig2 = register_sig(key2, "register", "quincy")
    assert (await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "quincy", cx1=cx1_2, sig=sig2
    ))["status"] == "OK"


@pytest.mark.anyio
async def test_delete_unknown_username_errors(node, db_setup):
    resp = await delete_registered_username(
        _mock_request(), TEST_MINT_ID, "nobody", sig=_BOGUS_SIG
    )
    assert resp == {"status": "ERROR", "reason": "Unknown user."}


@pytest.mark.anyio
async def test_register_signature_cannot_be_replayed_as_unregister(node, db_setup):
    """25.md binds the action into the signed message — a signature
    published to authorize a register can't be replayed to delete the
    same username."""
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", "sybil")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "sybil", cx1=cx1, sig=sig
    )
    resp = await delete_registered_username(
        _mock_request(), TEST_MINT_ID, "sybil", sig=sig
    )
    assert resp["status"] == "ERROR"
    assert await username_branch(TEST_MINT_ID, "sybil") is not None


@pytest.mark.anyio
async def test_ownership_signature_cannot_be_replayed_across_usernames(node, db_setup):
    """A signature proving ownership of one username on a branch must not
    authorize a delete of a DIFFERENT username sharing that same branch -
    25.md binds `username` into the signed message for exactly this."""
    key, cx1 = fresh_branch()
    sig_tanya = register_sig(key, "register", "tanya")
    sig_ursula = register_sig(key, "register", "ursula")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "tanya", cx1=cx1, sig=sig_tanya
    )
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "ursula", cx1=cx1, sig=sig_ursula
    )
    sig_for_tanya = register_sig(key, "unregister", "tanya")
    resp = await delete_registered_username(
        _mock_request(), TEST_MINT_ID, "ursula", sig=sig_for_tanya
    )
    assert resp["status"] == "ERROR"
    assert await username_branch(TEST_MINT_ID, "ursula") is not None


@pytest.mark.anyio
async def test_delete_disabled_while_username_registration_disabled(node, db_setup):
    key, cx1 = fresh_branch()
    sig0 = register_sig(key, "register", "river")
    await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, "river", cx1=cx1, sig=sig0
    )
    sig = register_sig(key, "unregister", "river")
    await update_mint(TEST_MINT_ID, TEST_WALLET, registration_enabled=False)
    resp = await delete_registered_username(
        _mock_request(), TEST_MINT_ID, "river", sig=sig
    )
    assert resp == {"status": "ERROR", "reason": "Not found"}
