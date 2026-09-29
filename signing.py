"""Per-mint secp256k1 signing for LUD-25 offline verification + cx1
registration ownership proofs.

Each mint has its own secp256k1 keypair (``mints.mint_privkey``, populated
at creation by ``crud._generate_mint_privkey``) - upstream signs notes via
the funding node's ``signmessage`` RPC, which LNbits does not expose, so
this port keeps the mint's own key (LUD-25 only *recommends* the node-id
key; a wallet verifies a note by recovering the key from (digest, sig) and
comparing it to the advertised ``mintPubkey``, which holds for any
secp256k1 key).

The signed payload now matches upstream exactly: the standard "Lightning
Signed Message" double-sha256 wrap of ``LNURLcash:<amount_msat>:<hex(Q)>``,
signed recoverably and emitted as a ``cs1`` certificate (amount folded
into the bech32m HRP, see bech32m.encode_cs1). ``hex(Q)`` is the note's
taproot output key - the ``cp1`` a WALLET disclosed (or a bearer note's
``h`` derived one), so this mint signs exactly what it was given, never a
secret it derived itself.

Registration ownership proofs (POST/DELETE /p/{mint_id}/{username}) are
plain BIP-340 schnorr signatures over sha256("LNURLcash:<action>:<domain>:
<username>"), domain-separated from a note's own spend (a transaction
sighash, never a text message) and bound to this mint's host, per
upstream's signing.verify_register_signature.

coincurve is a transitive LNbits dependency (nwc.py, nostr.py) - no new
dependency. No spendable credential (privkey, k1, spend) is logged.
"""

from hashlib import sha256
from typing import Optional

from coincurve import PrivateKey, PublicKey, PublicKeyXOnly
from loguru import logger

from .models import Mint

# The standard "Lightning Signed Message" wrap, identical to what lnd's
# and cln's signmessage RPCs compute internally - so any tool that already
# verifies a Lightning node's signed messages can verify a note's cs1.
_LIGHTNING_SIGNED_MESSAGE_PREFIX = b"Lightning Signed Message:"
_DOMAIN_TAG = "LNURLcash"


def lightning_signed_message_digest(message: str) -> bytes:
    """The LUD-25 note-signature digest: sha256(sha256(prefix || message))."""
    return sha256(sha256(_LIGHTNING_SIGNED_MESSAGE_PREFIX + message.encode()).digest()).digest()


def _message(note_id_hex: str, amount_msat: int) -> str:
    """The message a note's `cs1` certificate commits to, per LUD-25's
    Offline verification: "LNURLcash:<amount_msat>:<hex(Q)>". `note_id_hex`
    is the note's output key Q, lowercase hex - this mint's own storage
    id, and exactly what the `cp1` a WALLET disclosed decodes to."""
    return f"{_DOMAIN_TAG}:{amount_msat}:{note_id_hex}"


def mint_pubkey(mint: Mint) -> Optional[str]:
    """The compressed secp256k1 public key hex of the mint's signing key,
    advertised as LUD-25 `mintPubkey`. Returns None if the privkey is
    empty or invalid (never raises)."""
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


async def sign_note(note_id_hex: str, amount_msat: int, mint: Mint) -> Optional[str]:
    """A recoverable ECDSA signature over (note_id_hex, amount_msat) per
    LUD-25's Offline verification, as 65 bytes (r ‖ s ‖ recovery id),
    hex-encoded - matching upstream's wire format (the caller wraps it in
    a `cs1` certificate via bech32m.encode_cs1). Deterministic (RFC6979),
    so a replayed rotate/split/merge re-derives the identical certificate.
    Returns None on ANY failure (never raises) - a signing error must
    never block a rotate/split/merge."""
    try:
        pk = PrivateKey(bytes.fromhex(mint.mint_privkey))
        digest = lightning_signed_message_digest(_message(note_id_hex, amount_msat))
        return pk.sign_recoverable(digest, hasher=None).hex()
    except Exception as exc:
        logger.warning(f"sign_note: signing failed: {exc}")
        return None


async def certificate(note_id_hex: str, amount_msat: int, mint: Mint) -> Optional[str]:
    """This mint's Offline-verification certificate for a note, `cs1<...>`
    over (Q, amount). None if signing isn't available right now."""
    from .bech32m import encode_cs1

    raw = await sign_note(note_id_hex, amount_msat, mint)
    if raw is None:
        return None
    return encode_cs1(amount_msat, bytes.fromhex(raw))


def verify_note(
    mint_pubkey_hex: str, note_id_hex: str, amount_msat: int, signature_hex: str
) -> bool:
    """Recover the pubkey from a signature and compare to mint_pubkey_hex.

    TEST-ONLY - never called in production. Returns True iff the recovered
    compressed public key matches ``mint_pubkey_hex``. Returns False on any
    error (corrupt signature, wrong message, invalid hex, etc.).
    """
    try:
        recovered = PublicKey.from_signature_and_message(
            bytes.fromhex(signature_hex),
            lightning_signed_message_digest(_message(note_id_hex, amount_msat)),
            hasher=None,
        )
        return recovered.format(compressed=True).hex() == mint_pubkey_hex
    except Exception:
        return False


# ---------------------------------------------------------------------------
# cx1 registration ownership proofs (LUD-25 Seed & derivation)
#
# Everything a WALLET signs outside a note spend - the registration proof
# below - is a plain BIP-340 Schnorr signature over sha256(a message):
# BIP-340 signers only accept a 32-byte message. A note spend itself
# (ck1/cw1) signs a transaction sighash instead, verified by spend.py.
# ---------------------------------------------------------------------------


def _schnorr_digest(message: str) -> bytes:
    return sha256(message.encode()).digest()


# `action`/`username` fold in so a signature captured from one
# overwrite/delete can never be replayed against a different username
# sharing that branch, or against the other action for that same one; and
# `domain` (the SERVICE's own full domain name, LUD-05 style) so a proof
# captured by one mint can never be replayed by it against a different
# one.
def _register_message(action: str, domain: str, username: str) -> str:
    return f"{_DOMAIN_TAG}:{action}:{domain}:{username}"


def verify_register_signature(
    pubkey: bytes, signature_hex: str, action: str, domain: str, username: str
) -> bool:
    """Whether `signature_hex` is a valid registration ownership-proof
    Schnorr signature by `pubkey` (the branch's own index-0 public key,
    derived by the caller from the cx1 on file) over
    sha256(_register_message(action, domain, username)). `action` is
    "register" or "unregister". False (never raises) on a malformed
    signature."""
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    try:
        return PublicKeyXOnly(pubkey).verify(
            signature, _schnorr_digest(_register_message(action, domain, username))
        )
    except ValueError:
        return False
