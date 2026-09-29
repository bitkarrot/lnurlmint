"""Adversarial threat-suite for the bearer-note transport/exposure options
in the LUD-25 design debate — ported from the source test_bearer_threat_suite_poc.py.

One executable scenario per scorecard row, so candidate fixes get measured
against the same attacks instead of argued about in the abstract.

Under the taproot protocol the whole board shifted: no NEW mint is ever
preimage-keyed (comment output or branch-derived key mandatory), so the
T2 routing-node race and the T9 silent-fallback are CLOSED for all new
notes — they survive only on MIGRATED pre-m004 records
(comment_protected=0), pinned by tests that simulate one.

Control tests (T4, T5) assert behavior that must never change.
"""

from hashlib import sha256
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import get_note, update_mint
from lnurlmint.taproot import preimage_note
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    bearer_id,
    fresh_secret,
    mint_note,
)
from lnurlmint.views_lnurl import (
    get_pay_callback,
    get_withdraw,
    get_withdraw_callback,
)

VALUE = 50_000


def _mock_request() -> MagicMock:
    """A minimal Request mock for endpoints that call _public_base_url."""
    req = MagicMock()
    req.base_url = "http://test/"
    return req


async def _migrated_mint(node):
    """A settled migrated no-comment mint (pre-m004): note_id =
    Q(payment_hash), comment_protected=0. Returns (payment_hash,
    preimage, note_id)."""
    from lnurlmint.crud import db

    payment = await node.create_invoice(wallet_id=TEST_WALLET, amount=VALUE // 1000)
    ph = payment.payment_hash
    note_id = preimage_note(bytes.fromhex(ph))[0].hex()
    await db.execute(
        "INSERT INTO lnurlmint.mints_records "
        "(payment_hash, mint_id, pr, amount_msat, minted, note_id, "
        "comment_protected) VALUES (:ph, :mid, :pr, :amt, 0, :nid, 0)",
        {
            "ph": ph,
            "mid": TEST_MINT_ID,
            "pr": payment.bolt11,
            "amt": VALUE,
            "nid": note_id,
        },
    )
    node.settled.add(ph)
    return ph, node.preimages[ph], note_id


# ---------------------------------------------------------------------------
# T2 — routing-node race, migrated preimage-keyed note
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t2_routing_node_race_still_works_on_migrated_notes(node, db_setup):
    """T2 — for MIGRATED preimage-keyed notes the routing-node race is
    exactly the same: the routing node learns P as the HTLC settles, and
    P alone redeems, so it rotates the note onto its own output before
    the payer's wallet does. Pinned so nobody mistakes migrated notes
    for comment-protected ones."""
    victim_ph, preimage, note_id = await _migrated_mint(node)

    # materialize via a lazy-settle lookup (as a holder's /w would)
    from lnurlmint.crud import get_mint_by_id
    from lnurlmint.services import _try_settle_mint

    mint = await get_mint_by_id(TEST_MINT_ID)
    assert await _try_settle_mint(note_id, mint)

    # ATTACKER (any routing hop, holding only P): rotate immediately
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[preimage], p1=attacker_h,
    )
    assert r["status"] == "OK", r  # ATTACK STILL SUCCEEDS on migrated notes
    rotated = await get_note(bearer_id(attacker_h), TEST_MINT_ID)
    assert rotated is not None and rotated.amount_msat == VALUE

    # the legitimate payer arrives a moment later with the same P — too late
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[preimage], p1=victim_h,
    )
    assert r["status"] == "ERROR", r


@pytest.mark.anyio
async def test_t2b_comment_protected_note_defeats_the_routing_node_race(
    node, db_setup
):
    """T2, protected case — every NEW mint is comment-protected: the
    routing node still learns the invoice preimage P, but P was never
    the note's k1 — the note is keyed by the WALLET-held secret behind
    ``comment``, which no routing node ever sees."""
    victim_secret, comment = fresh_secret()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment=comment
    )
    import bolt11
    payment_hash = bolt11.decode(resp["pr"]).payment_hash
    preimage = node.preimages[payment_hash]
    node.settled.add(payment_hash)

    # ATTACKER (any routing hop, holding only P): rotating with it fails
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[preimage], p1=attacker_h,
    )
    assert r["status"] == "ERROR", r
    assert await get_note(bearer_id(attacker_h), TEST_MINT_ID) is None

    # the legitimate payer's own held secret redeems the note, no race
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[victim_secret], p1=victim_h,
    )
    assert r["status"] == "OK", r
    redeemed = await get_note(bearer_id(victim_h), TEST_MINT_ID)
    assert redeemed is not None and redeemed.amount_msat == VALUE


# ---------------------------------------------------------------------------
# T3 — informational poll leaks the live note
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t3_informational_poll_leaks_the_live_note(node, db_setup):
    """T3 — checking a note's value means GET /w?k1=<live bearer secret>
    — purely informational, it burns nothing — so every poll leaves the
    SPENDABLE k1 in whatever retains request URLs. Anyone reading that
    log line afterward can rotate the note out from under its holder.
    (?p= closes this for wallets that use the hash lookup instead.)"""
    k1, note_id, mint = await mint_note(node, VALUE)

    # victim checks the note's value — the poll burns nothing, but the
    # request URL carrying the live k1 is exactly what lands in logs
    r = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    assert r["tag"] == "withdrawRequest"
    assert r["maxWithdrawable"] == VALUE
    outstanding = await get_note(note_id, TEST_MINT_ID)
    assert outstanding is not None and outstanding.amount_msat == VALUE

    # ATTACKER, reading the logged URL afterward: replay the k1
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], p1=attacker_h,
    )
    assert r["status"] == "OK"  # ATTACK SUCCEEDS
    rotated = await get_note(bearer_id(attacker_h), TEST_MINT_ID)
    assert rotated is not None and rotated.amount_msat == VALUE


# ---------------------------------------------------------------------------
# T4 — callback log replay fails (CONTROL — must hold under every option)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t4_callback_log_replay_fails_control(node, db_setup):
    """T4 — control, must hold under EVERY option: a k1 captured from a
    MUTATING callback's URL was burned by the very request it rode in on,
    so replaying it after the fact can never work."""
    k1, note_id, mint = await mint_note(node, VALUE)
    new_k1, h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], p1=h,
    )
    assert r["status"] == "OK", r
    rotated_note_id = bearer_id(h)
    rotated = await get_note(rotated_note_id, TEST_MINT_ID)
    assert rotated is not None and rotated.amount_msat == VALUE

    # ATTACKER, reading the logged callback URL after the fact: replay it
    # with a DIFFERENT output — a conflict, not a replay (the find_burn
    # replay path only answers the exact same p1).
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], p1=attacker_h,
    )
    assert r["status"] == "ERROR", r
    # the rotated note is untouched
    still_there = await get_note(rotated_note_id, TEST_MINT_ID)
    assert still_there is not None and still_there.amount_msat == VALUE


# ---------------------------------------------------------------------------
# T5 — note at rest is cash (CONTROL — bearer axiom, must never change)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t5_note_at_rest_is_cash_control(node, db_setup):
    """T5 — the bearer axiom, expected to hold under every option
    forever: a note URL sitting in a chat log, a screenshot or a printed
    QR IS the money, and whoever finds it spends it. Not a bug and not
    fixable without killing bearer-ness itself."""
    k1, note_id, mint = await mint_note(node, VALUE)

    # FINDER of the URL, whoever and wherever they are: spend it
    _, finder_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], p1=finder_h,
    )
    assert r["status"] == "OK", r
    found = await get_note(bearer_id(finder_h), TEST_MINT_ID)
    assert found is not None and found.amount_msat == VALUE


# ---------------------------------------------------------------------------
# T6 — operator can link rotate to later spend (privacy row)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t6_operator_can_link_rotate_to_later_spend(node, db_setup):
    """T6 — the privacy row only option E (blinded signatures) wins. At
    rotate time WALLET discloses an output (the `h` short form names the
    new note's output key), and the mint keys its storage by
    deterministically-derived id — so when the note is later spent, its
    spend names that same id and issuance links to redemption."""
    k1, note_id, mint = await mint_note(node, VALUE)
    new_k1, h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], p1=h,
    )
    assert r["status"] == "OK", r

    # the mint's storage key for the new note is fully determined by the
    # disclosed output — the correlation is exact, not inferred
    rotated = await get_note(bearer_id(h), TEST_MINT_ID)
    assert rotated is not None and rotated.amount_msat == VALUE
    assert sha256(bytes.fromhex(new_k1)).hexdigest() == h

    # ...so a later spend of new_k1 names the recorded note id one-to-one
    _, h2 = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[new_k1], p1=h2,
    )
    assert r["status"] == "OK", r


# ---------------------------------------------------------------------------
# T10 — merge URL budget (pure arithmetic, no endpoints)
# ---------------------------------------------------------------------------


def test_t10_merge_url_budget_plaintext_fits_encrypted_does_not():
    """T10 — pure URL arithmetic against the ~2000 character practical GET
    budget. A merge callback carries one k1 per input note plus p1 for the
    result: 25 inputs in plaintext hex fit comfortably; the same merge
    with every k1 swapped for an encrypted-to-the-mint blob does not."""
    base = "http://testserver/w/cb?"
    h_param = "&p1=" + "0" * 64

    plaintext = base + "&".join(f"k1={'a' * 64}" for _ in range(25)) + h_param
    assert len(plaintext) <= 2000

    blob = "A" * 124  # option-C encrypted k1, base64
    encrypted = base + "&".join(f"p={blob}" for _ in range(25)) + h_param
    assert len(encrypted) > 2000


# ---------------------------------------------------------------------------
# T9 — comment is now MANDATORY (option B landed fully)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t9_malformed_or_missing_comment_is_rejected(node, db_setup):
    """T9 — upstream made comment protection mandatory: a malformed or
    absent comment on the fixed identity is a plain error, never a
    silent fallback to a preimage-keyed note. So the old T9 hole (a
    wallet THINKING it commented while it actually didn't) can't exist:
    there is no fallback note to steal."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True, min_mint_msat=0)
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), 5000, comment="this-will-be-ignored"
    )
    assert resp["status"] == "ERROR", resp

    no_comment = await get_pay_callback(TEST_MINT_ID, _mock_request(), 5000)
    assert no_comment["status"] == "ERROR", no_comment
