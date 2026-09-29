"""Shared test fixtures for the lnurlmint tests — LUD-25 taproot protocol.

The fakes monkeypatch the module-level imports that ``services.py`` and
``views_lnurl.py`` hold — ``lnbits_create_invoice``, ``lnbits_pay_invoice``,
``check_transaction_status``, ``get_standalone_payment`` — with controllable
tristate behaviour, so the funds-loss PoCs exercise ``paid=True`` /
``paid=False`` / ``paid=None`` without a real Lightning node.

Protocol notes (post-m004):

* A note id is hex(Q) — the taproot output key (``bearer_id(h)`` for the
  NUMS + ``OP_SHA256 <h> OP_EQUAL`` leaf, or a wallet key / branch-derived
  key for cp1 notes). ``k1_id`` maps a 64-hex preimage to the note it
  spends. ``ck1_for`` builds a real key-path spend (BIP-340 sig over the
  canonical sighash) so tests exercise the real ck1 path.
* A mint comment is now MANDATORY on the fixed identity — the helpers
  always mint via ``comment=h`` (or a cp1), matching upstream's
  comment-protection-everything rule.
* The response's replacement-note fields are ``p1``/``p2`` (upstream's
  rename of h/h2) and certificates come back as ``c``/``c2`` (`cs1`
  bech32m, not hex sigs).

Test isolation: the extension's tables are dropped and re-migrated per
test (all four migrations), and the in-flight registry is cleared.
``_CONFIRMATION_RETRY_DELAYS_SECONDS`` is monkeypatched to ``()`` so
confirmation is a single attempt with no sleep.
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
from coincurve import PrivateKey

import lnurlmint.services as services_module
import lnurlmint.views_lnurl as views_module
from lnbits.core.models.payments import Payment, PaymentState
from lnbits.exceptions import PaymentError
from lnbits.wallets.base import (
    PaymentFailedStatus,
    PaymentPendingStatus,
    PaymentSuccessStatus,
)
from lnurlmint import derivation, spend
from lnurlmint.bech32m import encode_cx1
from lnurlmint.crud import (
    create_mint,
    db,
    get_mint_by_id,
    record_mint_record,
)
from lnurlmint.migrations import (
    m001_initial,
    m002_notes_records_melts,
    m003_comment_hash_unique,
    m004_taproot_protocol,
)
from lnurlmint.models import Mint
from lnurlmint.services import _in_flight_melts, _try_settle_mint
from lnurlmint.taproot import preimage_note

# A fixed test wallet id; the mint created by ``db_setup`` belongs to it.
TEST_WALLET = "testwallet"
TEST_MINT_ID = "testmint"
TEST_DOMAIN = "test"


def fake_invoice(amount_msat: int, payment_hash: Optional[str] = None) -> str:
    """A syntactically-valid (but unpayable) BOLT11 invoice.

    Used to fake the node's invoice creation and to build melt targets
    of an exact msat amount.
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
    """A (k1, h) pair for a WALLET-generated bearer note, in LUD-25's
    short forms: k1 is its spend (the preimage), what the test keeps;
    h = sha256(k1) hex names the note on a mint comment or as p1/p2.
    Stored under bearer_id(h)."""
    secret = urandom(32).hex()
    return secret, sha256(bytes.fromhex(secret)).hexdigest()


def bearer_id(h: str) -> str:
    """The note id (hex Q) of the bearer note whose secret hashes to `h`."""
    return preimage_note(bytes.fromhex(h))[0].hex()


def k1_hash(k1: str) -> str:
    """The hex `h` naming a hex `k1` secret's bearer note (its `cp1`
    short form, e.g. for ?p=)."""
    return sha256(bytes.fromhex(k1)).hexdigest()


def k1_id(k1: str) -> str:
    """The note id (hex Q) of the bearer note a hex `k1` secret spends."""
    return bearer_id(k1_hash(k1))


def fresh_p1(key: Optional[PrivateKey] = None) -> tuple[str, str]:
    """A (cp1, note_id) pair for a WALLET-generated non-bearer note:
    the x-only key of `key` (or a fresh one) as p1. The note can then be
    spent by ck1 with the matching private key (see ck1_for)."""
    key = key or PrivateKey()
    q = key.public_key.format(compressed=True)[1:]
    from lnurlmint.bech32m import encode_cp1

    return encode_cp1(q), q.hex()


def ck1_for(key: PrivateKey, domain: str = TEST_DOMAIN) -> str:
    """The `ck1` key-path spend of the note Q = x(key·G) at `domain`:
    a BIP-340 signature over the canonical spend transaction's sighash.
    Zero aux randomness, like a real WALLET (and upstream's test helper)
    — deterministic, so spec test vectors are reproducible."""
    q = key.public_key.format(compressed=True)[1:]
    sig = key.sign_schnorr(spend.key_path_sighash(q, domain), aux_randomness=b"\x00" * 32)
    return spend.encode_ck1(spend.Spend(0, 0xFFFFFFFF, None, None, (sig,), key=q))


def cw1_preimage(preimage_hex: str) -> str:
    """The `cw1` script-path spend of a bearer note, in the long form —
    the same spend a 64-hex `k1` encodes, but explicit."""
    return spend.encode_cw1(spend.preimage_spend(bytes.fromhex(preimage_hex)))


def register_sig(
    branch_key: PrivateKey, action: str, username: str, domain: str = TEST_DOMAIN
) -> str:
    """The ownership-proof Schnorr signature a wallet produces for
    POST/DELETE /p/{mint}/{username}: over
    sha256("LNURLcash:<action>:<domain>:<username>") with the branch's
    PURPOSE_WALLET index-0 SECRET key. `branch_key` is the wallet's
    branch-root secret key (whose x-only pubkey is P in the cx1, with
    chain_code = branch_key.secret in fresh_branch() — the test's own
    stand-in for a BIP-32 chain code).

    The index-0 secret is the BIP-340 x-only tweak counterpart of
    derive_pubkey: d' = d (or n-d if P's y is odd) + tweak mod n."""
    branch_point = branch_key.public_key.format(compressed=True)[1:]
    chain_code = branch_key.secret  # fresh_branch()'s convention
    t = derivation.tagged_hash(
        b"LNURLcash/derive",
        branch_point
        + chain_code
        + (0).to_bytes(4, "big")
        + (0).to_bytes(4, "big"),
    )
    tweak = int.from_bytes(t, "big") % derivation._N
    d = int.from_bytes(branch_key.secret, "big")
    # x-only parity normalization: lift_x(P) uses the EVEN y, so if the
    # full pubkey's y is odd the secret is n - d.
    if branch_key.public_key.format(compressed=True)[0] == 0x03:
        d = derivation._N - d
    idx0 = PrivateKey(((d + tweak) % derivation._N).to_bytes(32, "big"))
    msg = sha256(f"LNURLcash:{action}:{domain}:{username}".encode()).digest()
    # zero aux randomness: deterministic, upstream's own helper shape
    return idx0.sign_schnorr(msg, aux_randomness=b"\x00" * 32).hex()


def fresh_branch() -> tuple[PrivateKey, str]:
    """A (branch_secret_key, cx1) pair: the wallet's branch root and its
    watch-only export. For tests the chain code doubles as the secret —
    register_sig above derives index 0 consistently with it."""
    key = PrivateKey()
    branch_point = key.public_key.format(compressed=True)[1:]
    chain_code = key.secret
    return key, encode_cx1(branch_point + chain_code)


def zap_request_for(
    zapper: PrivateKey,
    p_pubkey_hex: str,
    amount_msat: int,
    relays: Optional[list[str]] = None,
    content: str = "",
) -> str:
    """A signed kind-9734 zap request (NIP-57) as raw JSON — what a
    zapping client sends as `nostr=`."""
    import json

    from lnurlmint import nostr as nostr_module

    tags = [
        ["p", p_pubkey_hex],
        ["amount", str(amount_msat)],
        ["relays", *(relays or ["wss://relay.test"])],
    ]
    event = nostr_module.sign_event(
        nostr_module.ZAP_REQUEST_KIND, tags, content, zapper.secret.hex()
    )
    return json.dumps(event, separators=(",", ":"))


class FakeNode:
    """Test double that monkeypatches LNbits' payment services.

    ``create_invoice`` generates its own preimage (``urandom(32)``) so the
    test knows the invoice preimage — which under the taproot protocol
    redeems NOTHING on its own (every mint is comment/branch-keyed).
    ``pay_invoice`` records successful pays and marks their payment hash
    settled. ``check_transaction_status`` returns the tristate via
    ``PaymentStatus`` — defaulting to ``PaymentPendingStatus``
    (``paid=None``) for unknown hashes.
    """

    def __init__(self) -> None:
        self.settled: set[str] = set()
        self.preimages: dict[str, str] = {}  # payment_hash -> preimage hex
        self.description_hashes: dict[str, str] = {}  # payment_hash -> description
        self.paid: list[str] = []  # successfully paid payment_requests
        self.fail_payments: bool = False
        self.fail_reason: Optional[str] = None
        self.payment_actually_completed: bool = False
        self.is_payment_complete_raises: bool = False
        self.pay_delay: float = 0.0
        self.last_fee_limit_msat: Optional[int] = None
        self.check_transaction_status_calls: int = 0
        # InFlightNode coordination events (harmless on the base class).
        self.pay_started: asyncio.Event = asyncio.Event()
        self.pay_release: asyncio.Event = asyncio.Event()

    async def create_invoice(self, *, wallet_id, amount, memo="", **kwargs):
        """Replacement for ``lnbits.core.services.payments.create_invoice``.

        ``amount`` is in satoshis (LNbits convention). Returns a ``Payment``
        with the generated preimage.
        """
        preimage = urandom(32)
        payment_hash = sha256(preimage).hexdigest()
        self.preimages[payment_hash] = preimage.hex()
        description_hash = kwargs.get("description_hash")
        if description_hash is not None:
            # LNbits' create_invoice takes the description_hash digest
            # itself (the invoice's `h` tag) — record it verbatim
            self.description_hashes[payment_hash] = description_hash
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
        self.last_fee_limit_msat = kwargs.get("fee_limit_msat")
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
        """Replacement for
        ``lnbits.core.services.payments.check_transaction_status``.

        Default tristate: settled hash → ``paid=True``; otherwise
        ``paid=None`` (pending) — the unconfirmable case that must NOT
        trigger a restore (TEST-03).
        """
        self.check_transaction_status_calls += 1
        if self.is_payment_complete_raises:
            raise ConnectionError("funding source unreachable")
        if self.payment_actually_completed:
            return PaymentSuccessStatus()
        if payment_hash in self.settled:
            return PaymentSuccessStatus()
        return PaymentPendingStatus()  # paid=None

    async def get_standalone_payment(
        self, checking_id_or_hash, incoming=None, **kwargs
    ):
        """Replacement for ``lnbits.core.crud.payments.get_standalone_payment``.

        Returns a ``Payment`` with ``.preimage`` from the ``preimages``
        dict (keyed by payment_hash), or ``None`` if not found. Used by
        the LUD-21 verify helpers to fetch the preimage live on every
        call (SEC-02).
        """
        ph = checking_id_or_hash
        preimage = self.preimages.get(ph)
        if preimage is None:
            return None
        return Payment(
            checking_id=ph,
            payment_hash=ph,
            wallet_id="",
            amount=50_000 if incoming else -50_000,
            fee=0,
            bolt11="",
            status=PaymentState.SUCCESS,
            preimage=preimage,
        )


class HodlNode(FakeNode):
    """Models a hodl/ambiguous payment — ``paid=None`` while an HTLC is
    live. See ``pay_mode`` in the class docstring of the pre-port version;
    unchanged behaviour."""

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
        """Reality catches up: live HTLCs complete, hashes become
        settled."""
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
    ``listpays`` returns) unless the hash is already settled.
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
    """Monkeypatch the module-level payment imports in services + views."""
    monkeypatch.setattr(services_module, "lnbits_create_invoice", fake.create_invoice)
    monkeypatch.setattr(services_module, "lnbits_pay_invoice", fake.pay_invoice)
    monkeypatch.setattr(
        services_module, "check_transaction_status", fake.check_transaction_status
    )
    # LUD-21 verify preimage fetch — live, never cached.
    monkeypatch.setattr(
        services_module, "get_standalone_payment", fake.get_standalone_payment
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
    """InFlightNode — models the pre-registration window with
    asyncio.Event."""
    fake = InFlightNode()
    _patch_services(monkeypatch, fake)
    return fake


async def _reset_db() -> None:
    """Drop and re-create the lnurlmint tables (per-test isolation)."""
    for table in ("notes", "mints_records", "melts", "mints", "burns", "usernames"):
        await db.execute(f"DROP TABLE IF EXISTS lnurlmint.{table}")
    await m001_initial(db)
    await m002_notes_records_melts(db)
    await m003_comment_hash_unique(db)
    await m004_taproot_protocol(db)


@pytest.fixture
async def db_setup():
    """Initialize a fresh DB with a test mint + wallet.

    The extension's tables are dropped and re-migrated per test, and the
    in-flight melt registry is cleared so no test leaks state into the
    next.
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

    Mirrors how a real wallet obtains a note under the taproot protocol:
    generate a preimage, send ``comment=sha256(preimage)`` (the `h` short
    form) on /p/cb, "pay" the invoice, then trigger lazy materialization
    via ``_try_settle_mint``. The returned ``k1`` is the preimage hex
    (the bearer credential); ``note_id`` is hex(Q) = bearer_id(h).
    """
    mint = await get_mint_by_id(TEST_MINT_ID)
    k1, h = fresh_secret()
    note_id = bearer_id(h)
    payment = await node.create_invoice(
        wallet_id=mint.wallet, amount=amount_msat // 1000
    )
    payment_hash = payment.payment_hash
    await record_mint_record(
        payment_hash, mint.id, payment.bolt11, amount_msat, note_id
    )
    node.settled.add(payment_hash)
    await _try_settle_mint(note_id, mint)
    return k1, note_id, mint


async def mint_note_cp1(node: FakeNode, amount_msat: int = 50_000):
    """Mint a settled non-bearer note keyed by a fresh wallet key and
    return ``(key, cp1, note_id, mint)`` — the ck1-spendable shape."""
    key = PrivateKey()
    cp1, note_id = fresh_p1()
    mint = await get_mint_by_id(TEST_MINT_ID)
    payment = await node.create_invoice(
        wallet_id=mint.wallet, amount=amount_msat // 1000
    )
    payment_hash = payment.payment_hash
    await record_mint_record(
        payment_hash, mint.id, payment.bolt11, amount_msat, note_id
    )
    node.settled.add(payment_hash)
    await _try_settle_mint(note_id, mint)
    return key, cp1, note_id, mint


# ---------------------------------------------------------------------------
# Helpers — note_value, _leave_a_note_pending
# ---------------------------------------------------------------------------


def _mock_request() -> "object":
    from unittest.mock import MagicMock

    req = MagicMock()
    req.base_url = "http://test/"
    return req


async def note_value(k1: str) -> Optional[int]:
    """Check a note's value via the informational /w endpoint.

    Returns ``maxWithdrawable`` (msat) if the note is live, or ``None``
    if the endpoint returns an error (unknown or spent k1). Goes through
    the real endpoint so the full LNURL path is exercised.
    """
    from lnurlmint.views_lnurl import get_withdraw

    r = await get_withdraw(TEST_MINT_ID, _mock_request(), k1=k1)
    if r.get("status") == "ERROR":
        return None
    return r.get("maxWithdrawable")


async def note_value_by_p(p: str) -> Optional[int]:
    """Check a note's value via the informational /w endpoint's `p`
    lookup (LUD-25 hash lookup — no spend disclosed)."""
    from lnurlmint.views_lnurl import get_withdraw

    r = await get_withdraw(TEST_MINT_ID, _mock_request(), p=p)
    if r.get("status") == "ERROR":
        return None
    return r.get("maxWithdrawable")


async def _leave_a_note_pending(node, amount_msat: int = 5000) -> str:
    """Mint a note, then melt it with an unconfirmable payment.

    Leaves the note in the pending state (melt in-flight, payment status
    unknown) and returns the original k1. Used by reconcile and pending
    tests that need a stranded note.
    """
    from fastapi import BackgroundTasks

    from lnurlmint.views_lnurl import get_withdraw_callback

    k1, note_id, mint = await mint_note(node, amount_msat)
    node.fail_payments = True
    node.is_payment_complete_raises = True
    pr = fake_invoice(amount_msat)
    await get_withdraw_callback(
        TEST_MINT_ID, _mock_request(), BackgroundTasks(),
        k1=[k1], pr=pr,
    )
    # Verify the note is pending
    val = await note_value(k1)
    assert val is None, "pending note must not be advertised as withdrawable"
    return k1
