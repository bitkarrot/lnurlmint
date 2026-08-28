"""Shared test fixtures for the lnurlmint PoC tests (Plan 02-05).

Ports the source's ``FakeNode`` / ``HodlNode`` / ``InFlightNode`` pattern
(``~/lnurl-mint/tests/conftest.py``) to LNbits' async payment-service layer.
The fakes monkeypatch the module-level imports that ``services.py`` and
``views_lnurl.py`` hold — ``lnbits_create_invoice``, ``lnbits_pay_invoice``,
``check_transaction_status`` — with controllable tristate behaviour, so the
five funds-loss security PoCs can exercise ``paid=True`` / ``paid=False`` /
``paid=None`` without a real Lightning node.

Tristate modelling (the single highest-risk port detail, per CONTEXT.md):

* ``paid=True``  → ``PaymentSuccessStatus()``  — finalize (burn the note)
* ``paid=False`` → ``PaymentFailedStatus()``   — restore the note
* ``paid=None``  → ``PaymentPendingStatus()``  — leave the note pending

The source modelled ``paid=None`` by *raising* from ``is_payment_complete``;
LNbits' ``PaymentStatus`` models it as ``paid=None`` returned from
``check_transaction_status``. ``_confirm_payment`` treats both the same
(retry / leave pending) — the behaviour is equivalent (RQ12 gotcha #4).

Test isolation: the extension's file-backed SQLite (``ext_lnurlmint``) is
dropped and re-migrated per test, and the module-level in-flight registry is
cleared between tests. ``_CONFIRMATION_RETRY_DELAYS_SECONDS`` is monkeypatched
to ``()`` so confirmation is a single attempt with no sleep (fast tests).
"""

import asyncio
import time
from datetime import datetime, timezone
from hashlib import sha256
from os import urandom
from typing import Optional

import bolt11
import pytest
from bolt11.models.tags import TagChar, Tags
from bolt11.types import Bolt11

from lnbits.core.models.payments import Payment, PaymentState
from lnbits.exceptions import PaymentError
from lnbits.wallets.base import (
    PaymentFailedStatus,
    PaymentPendingStatus,
    PaymentSuccessStatus,
)

import lnurlmint.services as services_module
import lnurlmint.views_lnurl as views_module
from lnurlmint.crud import (
    db,
    create_mint,
    get_mint_by_id,
    record_mint_record,
)
from lnurlmint.migrations import m001_initial, m002_notes_records_melts
from lnurlmint.models import Mint
from lnurlmint.services import _try_settle_mint, _in_flight_melts

# A fixed test wallet id; the mint created by ``db_setup`` belongs to it.
TEST_WALLET = "testwallet"
TEST_MINT_ID = "testmint"


def fake_invoice(amount_msat: int, payment_hash: Optional[str] = None) -> str:
    """A syntactically-valid (but unpayable) BOLT11 invoice.

    Direct port of the source's ``fake_invoice`` — used to fake the node's
    invoice creation and to build melt targets of an exact msat amount.
    """
    tags = Tags()
    tags.add(TagChar.payment_hash, payment_hash or urandom(32).hex())
    tags.add(TagChar.payment_secret, urandom(32).hex())
    tags.add(TagChar.description, "test")
    return bolt11.encode(
        Bolt11(
            currency="bc",
            amount_msat=amount_msat,
            date=int(time.time()),
            tags=tags,
        ),
        private_key=urandom(32).hex(),
    )


def fresh_secret() -> tuple[str, str]:
    """A (k1, h) pair for LUD-25's WALLET-generated rotate/split/merge
    secret: k1 is what a real wallet would keep and never transmit,
    h = sha256(k1) hex is what goes on the /w/cb request as h/h2."""
    secret = urandom(32).hex()
    return secret, sha256(bytes.fromhex(secret)).hexdigest()


class FakeNode:
    """Test double that monkeypatches LNbits' payment services.

    ``create_invoice`` generates its own preimage (``urandom(32)``) so the
    test always knows the bearer credential ``k1 = preimage.hex()`` and the
    derived note id ``sha256(k1)``. ``pay_invoice`` records successful pays
    and marks their payment hash settled. ``check_transaction_status``
    returns the tristate via ``PaymentStatus`` — defaulting to
    ``PaymentPendingStatus`` (``paid=None``) for unknown hashes.
    """

    def __init__(self) -> None:
        self.settled: set[str] = set()
        self.preimages: dict[str, str] = {}  # payment_hash -> preimage hex
        self.paid: list[str] = []  # successfully paid payment_requests
        self.fail_payments: bool = False
        self.fail_reason: Optional[str] = None
        self.payment_actually_completed: bool = False
        self.is_payment_complete_raises: bool = False
        self.pay_delay: float = 0.0
        # InFlightNode coordination events (harmless on the base class).
        self.pay_started: asyncio.Event = asyncio.Event()
        self.pay_release: asyncio.Event = asyncio.Event()

    async def create_invoice(self, *, wallet_id, amount, memo="", **kwargs):
        """Replacement for ``lnbits.core.services.payments.create_invoice``.

        ``amount`` is in satoshis (LNbits convention). Returns a ``Payment``
        with the generated preimage so the test can recover ``k1``.
        """
        preimage = urandom(32)
        payment_hash = sha256(preimage).hexdigest()
        self.preimages[payment_hash] = preimage.hex()
        pr = fake_invoice(amount * 1000, payment_hash)
        return Payment(
            checking_id=payment_hash,
            payment_hash=payment_hash,
            wallet_id=wallet_id,
            amount=amount * 1000,
            fee=0,
            bolt11=pr,
            status=PaymentState.PENDING,
            preimage=preimage.hex(),
        )

    async def pay_invoice(self, *, wallet_id, payment_request, **kwargs):
        """Replacement for ``lnbits.core.services.payments.pay_invoice``."""
        if self.pay_delay:
            await asyncio.sleep(self.pay_delay)
        if self.fail_reason is not None:
            raise PaymentError(self.fail_reason, status="failed")
        if self.fail_payments:
            raise PaymentError("Payment failed: no route.", status="failed")
        decoded = bolt11.decode(payment_request)
        self.paid.append(payment_request)
        if decoded.has_payment_hash:
            self.settled.add(decoded.payment_hash)
        return Payment(
            checking_id=decoded.payment_hash or "",
            payment_hash=decoded.payment_hash or "",
            wallet_id=wallet_id,
            amount=-(decoded.amount_msat or 0),
            fee=0,
            bolt11=payment_request,
            status=PaymentState.SUCCESS,
        )

    async def check_transaction_status(self, wallet_id, payment_hash):
        """Replacement for ``lnbits.core.services.payments.check_transaction_status``.

        Default tristate: settled hash → ``paid=True``; otherwise
        ``paid=None`` (pending) — the unconfirmable case that must NOT
        trigger a restore (TEST-03).
        """
        if self.is_payment_complete_raises:
            raise ConnectionError("funding source unreachable")
        if self.payment_actually_completed:
            return PaymentSuccessStatus()
        if payment_hash in self.settled:
            return PaymentSuccessStatus()
        return PaymentPendingStatus()  # paid=None


class HodlNode(FakeNode):
    """Models a hodl/ambiguous payment — ``paid=None`` while an HTLC is live.

    ``pay_mode`` controls how ``pay_invoice`` fails:

    * ``"ambiguous"``     — raises ``PaymentError(status="pending")``; the
      HTLC stays live (``pending_hodl`` non-empty), so
      ``check_transaction_status`` returns ``paid=None`` (can't confirm).
    * ``"failed"``        — raises ``PaymentError(status="failed")``; HTLC
      stays live → ``paid=None`` (a terminal raise does NOT mean no HTLC).
    * ``"benign_failed"`` — raises ``PaymentError(status="failed")``; no
      HTLC → ``check_transaction_status`` returns ``paid=False`` (restore).
    * ``"ok"``            — pays normally.

    ``settle_hodl_payments()`` simulates reality catching up: the live HTLCs
    complete, their hashes move into ``settled``, and
    ``check_transaction_status`` now reports ``paid=True``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.pay_mode: str = "ok"
        self.pending_hodl: list[str] = []  # payment_requests with live HTLCs

    async def pay_invoice(self, *, wallet_id, payment_request, **kwargs):
        if self.pay_mode == "ambiguous":
            self.pending_hodl.append(payment_request)
            raise PaymentError(
                "lnd did not report a terminal payment status.",
                status="pending",
            )
        if self.pay_mode == "failed":
            self.pending_hodl.append(payment_request)
            raise PaymentError("Timed out trying to find a route.", status="failed")
        if self.pay_mode == "benign_failed":
            raise PaymentError("Could not find a route.", status="failed")
        return await super().pay_invoice(
            wallet_id=wallet_id, payment_request=payment_request, **kwargs
        )

    async def check_transaction_status(self, wallet_id, payment_hash):
        if self.pending_hodl:
            # Can't confirm either way while an HTLC is live — paid=None.
            return PaymentPendingStatus()
        if payment_hash in self.settled:
            return PaymentSuccessStatus()
        return PaymentFailedStatus()  # paid=False — confirmed not paid

    def settle_hodl_payments(self) -> None:
        """Reality catches up: live HTLCs complete, hashes become settled."""
        for pr in self.pending_hodl:
            decoded = bolt11.decode(pr)
            if decoded.has_payment_hash:
                self.settled.add(decoded.payment_hash)
        self.paid.extend(self.pending_hodl)
        self.pending_hodl.clear()


class InFlightNode(FakeNode):
    """Models the pre-registration window (TEST-04).

    ``pay_invoice`` sets ``pay_started`` then blocks on ``pay_release`` —
    the payment is in-flight. ``check_transaction_status`` reports
    ``paid=False`` for an unregistered payment (what lnd 404 / cln empty
    ``listpays`` returns) unless the hash is already settled (so the mint
    flow's lazy settlement still works during ``mint_note`` setup).
    """

    async def pay_invoice(self, *, wallet_id, payment_request, **kwargs):
        self.pay_started.set()
        await self.pay_release.wait()
        return await super().pay_invoice(
            wallet_id=wallet_id, payment_request=payment_request, **kwargs
        )

    async def check_transaction_status(self, wallet_id, payment_hash):
        if payment_hash in self.settled:
            return PaymentSuccessStatus()
        return PaymentFailedStatus()  # lnd 404 for an unregistered payment


def _patch_services(monkeypatch, fake) -> None:
    """Monkeypatch the module-level payment imports in services + views.

    ``services.py`` imports ``lnbits_create_invoice`` / ``lnbits_pay_invoice``
    / ``check_transaction_status`` at module level; ``views_lnurl.py`` imports
    ``lnbits_create_invoice`` separately. Both must be patched so the mint
    flow (views) and the melt/confirm flow (services) see the fake.
    """
    monkeypatch.setattr(services_module, "lnbits_create_invoice", fake.create_invoice)
    monkeypatch.setattr(services_module, "lnbits_pay_invoice", fake.pay_invoice)
    monkeypatch.setattr(
        services_module, "check_transaction_status", fake.check_transaction_status
    )
    # No real backoff in tests — single-attempt confirmation.
    monkeypatch.setattr(services_module, "_CONFIRMATION_RETRY_DELAYS_SECONDS", ())
    monkeypatch.setattr(views_module, "lnbits_create_invoice", fake.create_invoice)


@pytest.fixture
def node(monkeypatch) -> FakeNode:
    """FakeNode with default tristate behaviour (paid=None for unknown)."""
    fake = FakeNode()
    _patch_services(monkeypatch, fake)
    return fake


@pytest.fixture
def hodl_node(monkeypatch) -> HodlNode:
    """HodlNode — models paid=None via PaymentPendingStatus."""
    fake = HodlNode()
    _patch_services(monkeypatch, fake)
    return fake


@pytest.fixture
def inflight_node(monkeypatch) -> InFlightNode:
    """InFlightNode — models the pre-registration window with asyncio.Event."""
    fake = InFlightNode()
    _patch_services(monkeypatch, fake)
    return fake


async def _reset_db() -> None:
    """Drop and re-create the lnurlmint tables (per-test isolation)."""
    await db.execute("DROP TABLE IF EXISTS lnurlmint.notes")
    await db.execute("DROP TABLE IF EXISTS lnurlmint.mints_records")
    await db.execute("DROP TABLE IF EXISTS lnurlmint.melts")
    await db.execute("DROP TABLE IF EXISTS lnurlmint.mints")
    await m001_initial(db)
    await m002_notes_records_melts(db)


@pytest.fixture
async def db_setup():
    """Initialize a fresh in-memory-style DB with a test mint + wallet.

    The extension's SQLite file (``ext_lnurlmint.sqlite3``) is dropped and
    re-migrated per test, and the module-level in-flight melt registry is
    cleared so no test leaks state into the next.
    """
    _in_flight_melts.clear()
    await _reset_db()
    now = datetime.now(timezone.utc)
    await create_mint(
        Mint(
            id=TEST_MINT_ID,
            wallet=TEST_WALLET,
            username="testuser",
            mint_privkey="ab" * 32,
            created_at=now,
            updated_at=now,
        )
    )
    yield
    _in_flight_melts.clear()


async def mint_note(node: FakeNode, amount_msat: int = 50_000):
    """Mint a settled bearer note and return ``(k1, note_id, mint)``.

    Mirrors how a real wallet obtains a note: create a mint invoice, "pay"
    it (settle the hash), then trigger lazy materialization via
    ``_try_settle_mint`` (the same path the /w poll walks). The returned
    ``k1`` is the preimage hex (the bearer credential); ``note_id`` is
    ``sha256(k1)`` (the stored hash, never the credential itself — SEC-02).
    """
    mint = await get_mint_by_id(TEST_MINT_ID)
    payment = await node.create_invoice(
        wallet_id=mint.wallet, amount=amount_msat // 1000
    )
    payment_hash = payment.payment_hash
    k1 = payment.preimage
    note_id = sha256(bytes.fromhex(k1)).hexdigest()
    await record_mint_record(payment_hash, mint.id, payment.bolt11, amount_msat)
    node.settled.add(payment_hash)
    await _try_settle_mint(note_id, mint)
    return k1, note_id, mint
