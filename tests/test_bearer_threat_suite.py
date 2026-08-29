"""Adversarial threat-suite for the bearer-note transport/exposure options
in the LUD-25 design debate — ported from the source test_bearer_threat_suite_poc.py.

One executable scenario per scorecard row, so candidate fixes get measured
against the same attacks instead of argued about in the abstract.

Red/green convention: tests documenting an attack that succeeds TODAY
assert the current vulnerable behavior and say "INVERTS WHEN" in their
docstring — the PR landing the named option flips them red. Control
tests (T4, T5) assert behavior that must never change.

Adapted to LNbits fixtures: direct endpoint calls (no TestClient),
``mint_note`` / ``mint_note_with_comment`` helpers from conftest.py,
``update_mint`` for verify_enabled (no monkeypatching settings).
"""

from hashlib import sha256
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import get_note, update_mint
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    fresh_secret,
    mint_note,
    mint_note_with_comment,
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


# ---------------------------------------------------------------------------
# T2 — routing-node race, no-comment fallback (ATTACK SUCCEEDS today)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t2_routing_node_race_p_alone_is_sufficient(node, db_setup):
    """T2 — deliberately NOT fixed for a WALLET that skips comment
    protection: the no-comment fallback LUD-25 keeps for backward
    compatibility. The attacker is a routing node on the mint payment's
    path — it learns the preimage P as the settling HTLC propagates back.
    In the fallback, P alone redeems, so the attacker rotates the note
    onto its own h before the payer's wallet does, and wins."""
    k1, note_id, mint = await mint_note(node, VALUE)

    # ATTACKER (any routing hop, holding only P): rotate immediately
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=attacker_h,
    )
    assert r["status"] == "OK", r  # ATTACK SUCCEEDS in the no-comment fallback
    rotated = await get_note(attacker_h, TEST_MINT_ID)
    assert rotated is not None and rotated.amount_msat == VALUE

    # the legitimate payer arrives a moment later with the same P — too late
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=victim_h,
    )
    assert r == {"status": "ERROR", "reason": "Invalid or already spent k1."}


# ---------------------------------------------------------------------------
# T2b — comment-protected note defeats the routing-node race
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t2b_comment_protected_note_defeats_the_routing_node_race(
    node, db_setup
):
    """T2, protected case — a WALLET that DOES attach LUD-25 comment
    protection is immune: the routing node still learns P, but P was never
    the note's k1 — the note is keyed by the WALLET-held secret behind
    ``comment``, which no routing node ever sees."""
    victim_secret, comment_hash, note_id, payment_hash, mint = (
        await mint_note_with_comment(node, VALUE)
    )
    preimage = node.preimages[payment_hash]

    # ATTACKER (any routing hop, holding only P): rotating with it fails
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[preimage], h=attacker_h,
    )
    assert r["status"] == "ERROR", r
    assert await get_note(attacker_h, TEST_MINT_ID) is None

    # the legitimate payer's own held secret redeems the note, no race
    _, victim_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[victim_secret], h=victim_h,
    )
    assert r["status"] == "OK", r
    redeemed = await get_note(victim_h, TEST_MINT_ID)
    assert redeemed is not None and redeemed.amount_msat == VALUE


# ---------------------------------------------------------------------------
# T3 — informational poll leaks the live note (ATTACK SUCCEEDS today)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t3_informational_poll_leaks_the_live_note(node, db_setup):
    """T3 — INVERTS WHEN option D (hash-keyed informational GET) lands.

    Checking a note's value means GET /w?k1=<live bearer secret> — purely
    informational, it burns nothing — so every poll leaves the SPENDABLE
    k1 in whatever retains request URLs. Anyone reading that log line
    afterward can rotate the note out from under its holder."""
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
        k1=[k1], h=attacker_h,
    )
    assert r["status"] == "OK"  # ATTACK SUCCEEDS today
    rotated = await get_note(attacker_h, TEST_MINT_ID)
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
        k1=[k1], h=h,
    )
    assert r["status"] == "OK", r
    rotated_note_id = sha256(bytes.fromhex(new_k1)).hexdigest()
    rotated = await get_note(rotated_note_id, TEST_MINT_ID)
    assert rotated is not None and rotated.amount_msat == VALUE

    # ATTACKER, reading the logged callback URL after the fact: replay it
    _, attacker_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=attacker_h,
    )
    assert r == {"status": "ERROR", "reason": "Invalid or already spent k1."}
    # the rotated note is untouched
    still_there = await get_note(rotated_note_id, TEST_MINT_ID)
    assert still_there is not None and still_there.amount_msat == VALUE


# ---------------------------------------------------------------------------
# T5 — note at rest is cash (CONTROL — bearer axiom, must never change)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t5_note_at_rest_is_cash_control(node, db_setup):
    """T5 — the bearer axiom, expected to "fail" under every option
    forever: a note URL sitting in a chat log, a screenshot or a printed
    QR IS the money, and whoever finds it spends it. Not a bug and not
    fixable without killing bearer-ness itself — pinned so the scorecard's
    all-minus row stays deliberate."""
    k1, note_id, mint = await mint_note(node, VALUE)

    # FINDER of the URL, whoever and wherever they are: spend it
    _, finder_h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=finder_h,
    )
    assert r["status"] == "OK", r
    found = await get_note(finder_h, TEST_MINT_ID)
    assert found is not None and found.amount_msat == VALUE


# ---------------------------------------------------------------------------
# T6 — operator can link rotate to later spend (privacy row)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t6_operator_can_link_rotate_to_later_spend(node, db_setup):
    """T6 — the privacy row only option E (blinded signatures) wins. At
    rotate time WALLET discloses h = sha256(new_k1), and the mint keys its
    storage by exactly that h — so when new_k1 is later spent, its hash
    matches a recorded h and issuance links to redemption. Pinned so the
    scorecard's E column can't be claimed for free by the other options."""
    k1, note_id, mint = await mint_note(node, VALUE)
    new_k1, h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=h,
    )
    assert r["status"] == "OK", r

    # the mint's storage key for the new note is verbatim the h it was
    # given — the correlation is exact, not inferred
    rotated = await get_note(h, TEST_MINT_ID)
    assert rotated is not None and rotated.amount_msat == VALUE
    assert sha256(bytes.fromhex(new_k1)).hexdigest() == h

    # ...so a later spend of new_k1 matches the recorded h one-to-one
    _, h2 = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[new_k1], h=h2,
    )
    assert r["status"] == "OK", r


# ---------------------------------------------------------------------------
# T10 — merge URL budget (pure arithmetic, no endpoints)
# ---------------------------------------------------------------------------


def test_t10_merge_url_budget_plaintext_fits_encrypted_does_not():
    """T10 — pure URL arithmetic against the ~2000 character practical GET
    budget. A merge callback carries one k1 per input note plus h for the
    result: 25 inputs in plaintext hex fit comfortably; the same merge
    with every k1 swapped for an encrypted-to-the-mint blob (option C:
    33-byte ephemeral pubkey + 12-byte nonce + 32-byte ciphertext + 16-byte
    tag = 93 bytes, 124 base64 chars) does not."""
    base = "http://testserver/w/cb?"
    h_param = "&h=" + "0" * 64

    plaintext = base + "&".join(f"k1={'a' * 64}" for _ in range(25)) + h_param
    assert len(plaintext) <= 2000

    blob = "A" * 124  # option-C encrypted k1, base64
    encrypted = base + "&".join(f"p={blob}" for _ in range(25)) + h_param
    assert len(encrypted) > 2000


# ---------------------------------------------------------------------------
# T9 — comment silently ignored today (option B landed: malformed → fallback)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_t9_comment_is_silently_ignored_today(node, db_setup):
    """T9 — INVERTED: option B (comment-secret) landed. /p/cb now takes a
    ``comment`` parameter with defined semantics, and a malformed or absent
    ``comment`` degrades to the ordinary k1=P note visibly — by withholding
    verify for that invoice rather than advertising it as usual."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), VALUE, comment="this-will-be-ignored"
    )
    assert resp.get("pr")
    assert "verify" not in resp, resp

    # the malformed comment falls back cleanly: the settled preimage still
    # redeems the note exactly as the no-comment path always has
    import bolt11
    ph = bolt11.decode(resp["pr"]).payment_hash
    node.settled.add(ph)
    k1 = node.preimages[ph]
    _, h = fresh_secret()
    r = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=h,
    )
    assert r["status"] == "OK", r
