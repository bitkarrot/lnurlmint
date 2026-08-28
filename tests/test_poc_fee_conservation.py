"""PoC B4 (2026-08-17 review): value-conservation / inflation hunt.

Candidate claim (hunters' P4): the fee arithmetic might let a holder
inflate value via mint -> split -> merge cycles. The suspect spots:
  - _mint_fee_msat rounds the fee UP to a whole sat (services.py)
  - a split collects base_fee_msat once but produces 2 notes
  - a merge refunds (n-1) * base_fee_msat even though note
    lineage is not tracked, so a merged note "claims" a full base fee per
    input regardless of what that input historically cost

Method: a white-box Ledger drives the real endpoint functions
(get_pay_callback, get_withdraw_callback) and tracks paid_in (gross
invoice amounts), melted_out (invoices the mint paid), fees collected
(mint fees + split base fees) and refunds (merge refunds). After every
operation it asserts the conservation identity

    paid_in == outstanding + melted_out + fees_collected - refunds

and reads every note's value straight from the db (never trusting a
response). Any cycle ending with attacker_gain = outstanding + melted_out
- paid_in > 0 is an inflation bug.

Ported from the source's test_poc_fee_conservation.py, adapting to
LNbits async fixtures: endpoint functions called directly (not via
TestClient), fee settings updated via update_mint (not monkeypatching
global settings), note values read via get_note (not notes.note_amount).
"""

from hashlib import sha256
from unittest.mock import MagicMock

import bolt11
import pytest
from fastapi import BackgroundTasks

from lnurlmint.crud import get_note, update_mint
from lnurlmint.services import _mint_fee_msat, _try_settle_mint
from lnurlmint.views_lnurl import get_pay_callback, get_withdraw_callback
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    fake_invoice,
    fresh_secret,
    mint_note,
)


def _note_id(k1: str) -> str:
    return sha256(bytes.fromhex(k1)).hexdigest()


class Ledger:
    """White-box accounting ledger that drives the real endpoints.

    Tracks paid_in (gross invoice amounts), melted_out (invoices the
    mint paid), fees (mint fees + split base fees collected), and
    refunds (merge refunds paid out). After every operation it asserts
    the conservation identity and reads note values from the DB (never
    trusting responses).
    """

    def __init__(self, node) -> None:
        self.node = node
        self.paid_in = 0
        self.melted_out = 0
        self.fees = 0  # mint fees + split base fees actually collected
        self.refunds = 0  # merge refunds actually paid out
        self.ids: list[str] = []  # outstanding note ids, white-box

    async def _mint_row(self):
        from lnurlmint.crud import get_mint_by_id

        return await get_mint_by_id(TEST_MINT_ID)

    # -- operations (each asserts its own expected arithmetic) --

    async def mint(self, gross_msat: int) -> str:
        mint = await self._mint_row()
        resp = await get_pay_callback(TEST_MINT_ID, MagicMock(), amount=gross_msat)
        assert resp.get("pr"), resp
        pr = resp["pr"]
        decoded = bolt11.decode(pr)
        payment_hash = decoded.payment_hash
        k1 = self.node.preimages[payment_hash]
        note_id = sha256(bytes.fromhex(k1)).hexdigest()
        self.node.settled.add(payment_hash)
        await _try_settle_mint(note_id, mint)
        note = await get_note(note_id, mint.id)
        net = note.amount_msat
        # the minted value must equal gross minus the exact fee formula
        assert net == gross_msat - _mint_fee_msat(gross_msat, mint)
        self.paid_in += gross_msat
        self.fees += gross_msat - net
        self.ids.append(note_id)
        await self.assert_conserved()
        return k1

    async def rotate(self, k1: str) -> str:
        mint = await self._mint_row()
        old_note = await get_note(_note_id(k1), mint.id)
        assert old_note is not None
        old = old_note.amount_msat
        secret, h = fresh_secret()
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1], h=h,
        )
        assert resp["status"] == "OK", resp
        self.ids.remove(_note_id(k1))
        self.ids.append(h)
        new_note = await get_note(h, mint.id)
        assert new_note.amount_msat == old  # rotate is value-neutral
        await self.assert_conserved()
        return secret

    async def split(self, k1: str, amount_msat: int) -> tuple[str, str]:
        mint = await self._mint_row()
        total_note = await get_note(_note_id(k1), mint.id)
        assert total_note is not None
        total = total_note.amount_msat
        secret_amount, h = fresh_secret()
        secret_change, h2 = fresh_secret()
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1], h=h, h2=h2, amount=amount_msat,
        )
        assert resp["status"] == "OK", resp
        change = total - amount_msat - mint.base_fee_msat
        self.fees += mint.base_fee_msat
        self.ids.remove(_note_id(k1))
        self.ids.extend([h, h2])
        amount_note = await get_note(h, mint.id)
        change_note = await get_note(h2, mint.id)
        assert amount_note.amount_msat == amount_msat
        assert change_note.amount_msat == change
        await self.assert_conserved()
        return secret_amount, secret_change

    async def merge(self, k1s: list[str]) -> str:
        mint = await self._mint_row()
        values = []
        for k1 in k1s:
            note = await get_note(_note_id(k1), mint.id)
            assert note is not None
            values.append(note.amount_msat)
        secret, h = fresh_secret()
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=k1s, h=h,
        )
        assert resp["status"] == "OK", resp
        refund = (len(k1s) - 1) * mint.base_fee_msat
        self.refunds += refund
        for k1 in k1s:
            self.ids.remove(_note_id(k1))
        self.ids.append(h)
        merged_note = await get_note(h, mint.id)
        assert merged_note.amount_msat == sum(values) + refund
        await self.assert_conserved()
        return secret

    async def melt(self, k1: str) -> None:
        mint = await self._mint_row()
        note = await get_note(_note_id(k1), mint.id)
        assert note is not None
        value = note.amount_msat
        resp = await get_withdraw_callback(
            TEST_MINT_ID, MagicMock(), BackgroundTasks(),
            k1=[k1], pr=fake_invoice(value),
        )
        assert resp["status"] == "OK", resp
        self.melted_out += value
        self.ids.remove(_note_id(k1))
        await self.assert_conserved()

    # -- accounting --

    async def outstanding(self) -> int:
        mint = await self._mint_row()
        total = 0
        for nid in self.ids:
            note = await get_note(nid, mint.id)
            if note is not None:
                total += note.amount_msat
        return total

    async def attacker_gain(self) -> int:
        return await self.outstanding() + self.melted_out - self.paid_in

    async def assert_conserved(self) -> None:
        # the pure bookkeeping identity - holds even in the operator
        # fee-raise scenario, where the over-refund is funded by the mint's
        # own treasury (fees/refunds are tracked exactly, so the identity
        # absorbs it)
        outstanding = await self.outstanding()
        assert self.paid_in == outstanding + self.melted_out + self.fees - self.refunds

    async def assert_no_attacker_gain(self) -> None:
        """The adversarial invariant: after any attacker-reachable cycle the
        holder has NOT ended up with more than they paid in. Checked
        explicitly at the end of each attack test rather than inside every
        op, because the informational operator-fee-raise test deliberately
        violates it (from the mint's treasury, not from thin air)."""
        assert await self.attacker_gain() <= 0


@pytest.fixture
async def ledger(node, db_setup) -> Ledger:
    return Ledger(node)


@pytest.fixture
async def fee_settings(db_setup):
    """Set base_fee_msat=1000, fee_percent_ppm=0, min_mint_msat=0,
    min_sendable_msat=1000 on the test mint."""
    await update_mint(
        TEST_MINT_ID, TEST_WALLET,
        base_fee_msat=1000,
        fee_percent_ppm=0,
        min_mint_msat=0,
        min_sendable_msat=1000,
    )


async def _set_fees(**kwargs) -> None:
    """Update the test mint's fee fields inline (bypasses pydantic
    validation — update_mint filters against _UPDATABLE_FIELDS only)."""
    await update_mint(TEST_MINT_ID, TEST_WALLET, **kwargs)


@pytest.mark.anyio
async def test_simple_cycles(ledger: Ledger, fee_settings):
    # cycle A: mint -> rotate -> melt
    k1 = await ledger.mint(100_000)
    k1 = await ledger.rotate(k1)
    await ledger.melt(k1)
    assert await ledger.attacker_gain() == -1000  # one mint fee, kept by the mint

    # cycle B: mint -> split -> merge -> melt
    k1 = await ledger.mint(100_000)
    a, change = await ledger.split(k1, 40_000)
    k1 = await ledger.merge([a, change])
    await ledger.melt(k1)
    # two mint fees total so far, split fee and merge refund cancel exactly
    assert await ledger.attacker_gain() == -2000

    # cycle C: deep split chain - split off 1 msat dust nine times, merge
    # all ten notes back, melt
    k1 = await ledger.mint(1_000_000)
    dust = []
    for _ in range(9):
        d, k1 = await ledger.split(k1, 1)
        dust.append(d)
    k1 = await ledger.merge([*dust, k1])
    await ledger.melt(k1)
    assert await ledger.attacker_gain() == -3000

    # cycle D: three separate mints, split each, cross-merge everything
    parts = []
    for _ in range(3):
        k1 = await ledger.mint(100_000)
        a, change = await ledger.split(k1, 25_000)
        parts.extend([a, change])
    k1 = await ledger.merge(parts)
    await ledger.melt(k1)
    # cumulative across all four cycles: 6 mint fees + 13 split fees - 15
    # merge refunds = 4000 kept by the mint
    assert await ledger.attacker_gain() == -4000


@pytest.mark.anyio
async def test_dust_split_edges(ledger: Ledger, fee_settings):
    # change of exactly 1 msat is allowed (change == 0 is rejected)
    k1 = await ledger.mint(100_000)  # nets 99_000
    a, change = await ledger.split(k1, 97_999)  # change_before_fee = 1001 -> change = 1
    mint = await ledger._mint_row()
    change_note = await get_note(_note_id(change), mint.id)
    assert change_note.amount_msat == 1
    # ...and the 1-msat dust note still merges back losslessly
    k1 = await ledger.merge([a, change])
    await ledger.melt(k1)
    assert await ledger.attacker_gain() == -1000

    # amount of exactly 1 msat works too
    k1 = await ledger.mint(100_000)
    a, change = await ledger.split(k1, 1)
    k1 = await ledger.merge([a, change])
    await ledger.melt(k1)
    assert await ledger.attacker_gain() == -2000

    # change_before_fee == base_fee exactly (change would be 0) is rejected,
    # and the failed split changes nothing
    k1 = await ledger.mint(100_000)
    total_note = await get_note(_note_id(k1), mint.id)
    total = total_note.amount_msat
    _, h = fresh_secret()
    _, h2 = fresh_secret()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=h, h2=h2, amount=total - 1000,
    )
    assert resp["status"] == "ERROR"
    intact_note = await get_note(_note_id(k1), mint.id)
    assert intact_note.amount_msat == total
    assert await get_note(h, mint.id) is None
    assert await get_note(h2, mint.id) is None
    await ledger.assert_conserved()


@pytest.mark.anyio
async def test_hundred_note_merge_is_not_a_base_fee_printing_press(ledger: Ledger, fee_settings):
    """The lead's suspect: merging N notes refunds (N-1) base fees, but each
    split only collected ONE base fee while producing two notes. Quantified
    here at the maximum batch: carve 99 dust notes of 1 sat off one mint
    (99 splits, 99 base fees collected), then merge all 100 notes at once
    (max_k1s) - the refund is 99 base fees, EXACTLY what the splits
    collected. Net effect zero; the mint keeps precisely the mint fee."""
    k1 = await ledger.mint(301_000)  # nets 300_000 after the 1000-msat mint fee
    dust = []
    for _ in range(99):
        d, k1 = await ledger.split(k1, 1000)
        dust.append(d)
    assert len(dust) == 99
    mint = await ledger._mint_row()
    # change note: 300_000 - 99*1000 (amounts) - 99*1000 (split fees) = 102_000
    change_note = await get_note(_note_id(k1), mint.id)
    assert change_note.amount_msat == 102_000
    k1 = await ledger.merge([*dust, k1])  # 100 k1s, refund = 99 * 1000
    merged_note = await get_note(_note_id(k1), mint.id)
    assert merged_note.amount_msat == 300_000
    await ledger.melt(k1)
    assert await ledger.attacker_gain() == -1000  # exactly the one mint fee
    # fees collected: 1000 (mint) + 99_000 (splits) = refunds 99_000 + 1000 kept
    assert ledger.fees == 100_000
    assert ledger.refunds == 99_000


@pytest.mark.anyio
async def test_fee_arithmetic_grid_never_attacker_favorable(node, db_setup):
    """Property sweep: over a grid of (base_fee, ppm, gross), the minted
    net value never exceeds gross - base_fee, i.e. every minted note has
    provably 'paid' at least one base fee - the load-bearing fact for the
    merge-refund conservation argument (see report)."""
    from lnurlmint.crud import get_mint_by_id

    await _set_fees(min_mint_msat=0, min_sendable_msat=1000)
    for base_fee in (0, 1, 500, 1000, 1500, 10_000):
        for ppm in (0, 1, 1000, 500_000, 99_999):
            await _set_fees(base_fee_msat=base_fee, fee_percent_ppm=ppm)
            mint = await get_mint_by_id(TEST_MINT_ID)
            for gross in (1000, 10_000, 999_999, 1_000_000, 1_500_000, 100_000_000):
                fee = _mint_fee_msat(gross, mint)
                # fee always >= the unrounded formula, and always >= base_fee
                assert fee >= base_fee + (gross * ppm) // 1_000_000
                assert fee >= base_fee
                net = gross - fee
                if net >= 0:  # mintable at min_mint=0
                    assert net <= gross - base_fee


@pytest.mark.anyio
async def test_zero_value_mint_edge_no_gain(ledger: Ledger, db_setup):
    """min_mint_msat=0 + fee == gross mints a ZERO-value note (net=0 is not
    < min_mint=0, so /p/cb allows it). Confirm it buys the attacker nothing:
    zero-notes merge into zero-notes (refund only ever adds base_fee, which
    each zero-note already paid in full at mint time)."""
    from lnurlmint.crud import get_mint_by_id

    # variant 1: ppm=1e6, bf=0 - fee == gross exactly at multiples of 1000
    await _set_fees(base_fee_msat=0, fee_percent_ppm=1_000_000, min_mint_msat=0, min_sendable_msat=1000)
    z1 = await ledger.mint(1000)
    z2 = await ledger.mint(1000)
    mint = await get_mint_by_id(TEST_MINT_ID)
    assert (await get_note(_note_id(z1), mint.id)).amount_msat == 0
    merged = await ledger.merge([z1, z2])  # refund = 1 * 0 = 0
    assert (await get_note(_note_id(merged), mint.id)).amount_msat == 0
    assert await ledger.attacker_gain() == -2000  # attacker paid everything, holds nothing

    # variant 2: bf=1000, ppm=0 - zero-note that 'paid' a full base fee
    await _set_fees(base_fee_msat=1000, fee_percent_ppm=0, min_mint_msat=0, min_sendable_msat=1000)
    z1 = await ledger.mint(1000)
    z2 = await ledger.mint(1000)
    mint = await get_mint_by_id(TEST_MINT_ID)
    assert (await get_note(_note_id(z1), mint.id)).amount_msat == 0
    merged = await ledger.merge([z1, z2])  # refund = 1 * 1000, paid for by the two mint fees
    assert (await get_note(_note_id(merged), mint.id)).amount_msat == 1000
    await ledger.melt(merged)
    assert await ledger.attacker_gain() == -3000  # paid 4000 total, got 1000 back


@pytest.mark.anyio
async def test_sub_sat_base_fee_rounding_is_mint_favorable(ledger: Ledger, db_setup):
    """base_fee_msat=1 (sub-sat): the mint fee rounds UP to 1000 msat while
    splits collect and merges refund the raw 1 msat - the rounding gap is
    always kept by the mint, never by the holder."""
    from lnurlmint.crud import get_mint_by_id

    await _set_fees(base_fee_msat=1, fee_percent_ppm=0, min_mint_msat=0, min_sendable_msat=1000)
    k1 = await ledger.mint(100_000)
    mint = await get_mint_by_id(TEST_MINT_ID)
    assert (await get_note(_note_id(k1), mint.id)).amount_msat == 99_000  # fee rounded 1 -> 1000
    a, change = await ledger.split(k1, 50_000)  # collects 1 msat
    k1 = await ledger.merge([a, change])  # refunds 1 msat
    await ledger.melt(k1)
    assert await ledger.attacker_gain() == -1000


@pytest.mark.anyio
async def test_failed_requests_change_no_value(ledger: Ledger, fee_settings):
    """Every adversarially-malformed multi-k1 request must fail atomically:
    no input burned, no output minted, ledger untouched."""
    from lnurlmint.crud import get_mint_by_id

    mint = await get_mint_by_id(TEST_MINT_ID)

    # duplicate k1 in one merge: swap's dedup check rejects, whole
    # transaction rolls back
    k1 = await ledger.mint(100_000)
    _, h = fresh_secret()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1, k1], h=h,
    )
    assert resp["status"] == "ERROR"
    assert (await get_note(_note_id(k1), mint.id)).amount_msat == 99_000  # intact
    assert await get_note(h, mint.id) is None  # nothing minted
    await ledger.assert_conserved()

    # split with h == h2: swap's dedup check rejects, rolls back
    k1b = await ledger.mint(100_000)
    _, h_dup = fresh_secret()
    _, h2_dup = fresh_secret()
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1b], h=h_dup, h2=h_dup, amount=1000,
    )
    assert resp["status"] == "ERROR"
    assert (await get_note(_note_id(k1b), mint.id)).amount_msat == 99_000
    assert await get_note(h_dup, mint.id) is None
    await ledger.assert_conserved()

    # merge onto an EXISTING outstanding note id: collision, rolls back
    k1c = await ledger.mint(100_000)
    existing_id = _note_id(k1)
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1c], h=existing_id,
    )
    assert resp["status"] == "ERROR"
    assert (await get_note(_note_id(k1c), mint.id)).amount_msat == 99_000
    assert (await get_note(existing_id, mint.id)).amount_msat == 99_000
    await ledger.assert_conserved()

    # split amount == total (change would be negative) rejected, no-op
    resp = await get_withdraw_callback(
        TEST_MINT_ID, MagicMock(), BackgroundTasks(),
        k1=[k1], h=h, h2=h2_dup, amount=99_000,
    )
    assert resp["status"] == "ERROR"
    assert (await get_note(_note_id(k1), mint.id)).amount_msat == 99_000
    await ledger.assert_conserved()
    await ledger.assert_no_attacker_gain()


@pytest.mark.anyio
async def test_merge_can_exceed_max_sendable_but_stays_conserved(ledger: Ledger, fee_settings, db_setup):
    """maxSendable bounds /p/cb only; nothing caps a merged note's value.
    Merging two near-max notes into one oversized note and melting it works
    - but pays out exactly what was paid in, minus fees. Not inflation,
    documented here because it doubles the melt size an operator may expect
    to have to route."""
    from lnurlmint.crud import get_mint_by_id

    await _set_fees(max_sendable_msat=200_000)
    k1a = await ledger.mint(200_000)
    k1b = await ledger.mint(200_000)
    k1 = await ledger.merge([k1a, k1b])
    mint = await get_mint_by_id(TEST_MINT_ID)
    oversized_note = await get_note(_note_id(k1), mint.id)
    assert oversized_note.amount_msat == 199_000 + 199_000 + 1000 > mint.max_sendable_msat
    await ledger.melt(k1)  # pays the oversized invoice fine (FakeNode)
    # two mint fees collected, one base fee refunded on the merge: net -1000
    assert await ledger.attacker_gain() == -1000


@pytest.mark.anyio
async def test_operator_fee_raise_overrefunds(ledger: Ledger, db_setup):
    """Informational, config-change gated (NOT attacker-reachable): merge
    refunds use the CURRENT base_fee_msat, not the one historical notes
    actually paid. If an operator raises base_fee_msat while notes are
    outstanding, merges of pre-raise notes refund more than was ever
    collected for them - quantified here: 3 notes minted at bf=1000 (3000
    collected), merged at bf=5000 (10000 refunded): the mint pays out 7000
    it never collected. The mirror-image fee CUT under-refunds holders.
    Attacker cannot trigger this; it's an operator footgun worth a doc
    line, not a vulnerability."""
    from lnurlmint.crud import get_mint_by_id

    await _set_fees(base_fee_msat=1000, fee_percent_ppm=0, min_mint_msat=0, min_sendable_msat=1000)
    k1s = [await ledger.mint(100_000) for _ in range(3)]
    fees_before = ledger.fees
    await _set_fees(base_fee_msat=5000)  # operator raises the fee
    k1 = await ledger.merge(k1s)
    mint = await get_mint_by_id(TEST_MINT_ID)
    assert (await get_note(_note_id(k1), mint.id)).amount_msat == 3 * 99_000 + 2 * 5000
    overrefund = 2 * 5000 - 2 * 1000  # refunded 10_000 vs 2_000 historically collected
    assert overrefund == 8000
    assert fees_before == 3000
