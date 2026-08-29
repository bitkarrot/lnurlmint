"""Per-mint secp256k1 signing for LUD-25 offline verification (Phase 5).

Option B: each mint has its own secp256k1 keypair (stored as
``mints.mint_privkey``, populated at creation by ``crud._generate_mint_privkey``).
``mint_pubkey`` derives the compressed public key for advertisement on the /w
withdrawRequest response. ``sign_note`` produces a recoverable ECDSA signature
over ``LNURLcash:<amount_msat>:<note_id_hex>`` so holders can verify
rotate/split/merge notes offline against the advertised ``mintPubkey`` without
trusting the mint online. ``verify_note`` recovers the public key from a
signature (test-only — never called in production).

No Lightning Signed Message prefix / double-sha256 (the source's Option A used
node ``signmessage`` RPC; Option B uses coincurve's default sha256 hasher).
``mintPubkey`` is the mint's own key, not the node's — portable across all
LNbits backends (FakeWallet, VoidWallet, etc.).

coincurve is a transitive LNbits dependency (nwc.py, nostr.py), already used by
``crud._generate_mint_privkey`` since Phase 1 — no new dependency (EXT-04).

No spendable credential (privkey, k1, h) is logged (SEC-05).
"""

from typing import Optional

from coincurve import PrivateKey, PublicKey
from loguru import logger

from .models import Mint

_DOMAIN_TAG = "LNURLcash"


def _message(note_id_hex: str, amount_msat: int) -> bytes:
    """The signed message: ``LNURLcash:<amount_msat>:<note_id_hex>``."""
    return f"{_DOMAIN_TAG}:{amount_msat}:{note_id_hex}".encode()


def mint_pubkey(mint: Mint) -> Optional[str]:
    """Derive the compressed secp256k1 public key hex from the mint's privkey.

    Returns None if the privkey is empty or invalid (never raises). The
    mint_privkey column is NOT NULL, so None only happens on corruption.
    """
    if not mint.mint_privkey:
        return None
    try:
        return (
            PublicKey.from_secret(bytes.fromhex(mint.mint_privkey))
            .format(compressed=True)
            .hex()
        )
    except Exception:
        return None


async def sign_note(h: str, amount_msat: int, mint: Mint) -> Optional[str]:
    """Recoverable ECDSA signature over ``LNURLcash:<amount_msat>:<h>``.

    Returns a 130-char hex string (65-byte r‖s‖recovery-id) using
    coincurve's default sha256 hasher. Returns None on ANY failure
    (never raises) — a signing error must never block a
    rotate/split/merge. The warning log includes the exception but NOT
    the privkey or h (SEC-05).
    """
    try:
        pk = PrivateKey(bytes.fromhex(mint.mint_privkey))
        return pk.sign_recoverable(_message(h, amount_msat)).hex()
    except Exception as exc:
        logger.warning(f"sign_note: signing failed: {exc}")
        return None


def verify_note(
    mint_pubkey_hex: str, h: str, amount_msat: int, signature_hex: str
) -> bool:
    """Recover the pubkey from a signature and compare to mint_pubkey_hex.

    TEST-ONLY — never called in production. Returns True iff the recovered
    compressed public key matches ``mint_pubkey_hex``. Returns False on any
    error (corrupt signature, wrong message, invalid hex, etc.).
    """
    try:
        recovered = PublicKey.from_signature_and_message(
            bytes.fromhex(signature_hex), _message(h, amount_msat)
        )
        return recovered.format(compressed=True).hex() == mint_pubkey_hex
    except Exception:
        return False
