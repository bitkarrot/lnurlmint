"""LUD-25 bech32m codecs: cp1/ck1/cw1/cs1/cx1 plus NIP-19 npub.

Ported from upstream lnurl-mint's ``bech32m.py`` (MIT). LUD-25's Encoding
uses BIP-350 bech32m (not classic bech32 - the ``bech32`` package's own
bech32_encode used elsewhere in LNbits for ``lnurl`` strings is a different
checksum constant). The ``bech32`` dependency only exposes classic bech32
checksums directly but does export the primitives (bech32_hrp_expand,
bech32_polymod, CHARSET, convertbits) a bech32m checksum is built from -
see BIP-350's reference implementation, which this mirrors.

``bech32_decode`` can't be reused either: it caps the total string at ~90
characters (BIP-173's segwit-address limit), while ck1/cs1/cx1 deliberately
exceed that per 25.md - the same departure BOLT-11 invoices already make.
"""

from bech32 import (
    CHARSET,
    bech32_decode,
    bech32_hrp_expand,
    bech32_polymod,
    convertbits,
)
from bolt11.exceptions import Bolt11AmountInvalidException
from bolt11.utils import amount_to_msat, msat_to_amount

BECH32M_CONST = 0x2BC830A3


def _create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = bech32_hrp_expand(hrp) + data
    polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ BECH32M_CONST
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _verify_checksum(hrp: str, data: list[int]) -> bool:
    return bech32_polymod(bech32_hrp_expand(hrp) + data) == BECH32M_CONST


def encode(hrp: str, data: bytes) -> str:
    """bech32m-encode `data` under `hrp` - no witness-version byte, unlike
    a segwit address: just the HRP, separator, and raw data."""
    values = convertbits(data, 8, 5, True)
    assert values is not None
    combined = values + _create_checksum(hrp, values)
    return hrp + "1" + "".join(CHARSET[d] for d in combined)


def decode(hrp: str, s: str) -> bytes | None:
    """Inverse of encode(). None on any malformed input - wrong HRP, wrong
    checksum, non-charset characters, mixed case, or a non-byte-aligned
    payload - never raises."""
    if any(ord(c) < 33 or ord(c) > 126 for c in s):
        return None
    if s.lower() != s and s.upper() != s:
        return None
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        return None
    found_hrp, data_part = s[:pos], s[pos + 1 :]
    if found_hrp != hrp or not all(c in CHARSET for c in data_part):
        return None
    data = [CHARSET.find(c) for c in data_part]
    if not _verify_checksum(found_hrp, data):
        return None
    decoded = convertbits(data[:-6], 5, 8, False)
    if decoded is None:
        return None
    return bytes(decoded)


def _fixed_length_codec(hrp: str, length: int) -> tuple:
    def enc(data: bytes) -> str:
        if len(data) != length:
            raise ValueError(f"{hrp}1... payload must be {length} bytes, got {len(data)}")
        return encode(hrp, data)

    def dec(s: str) -> bytes | None:
        data = decode(hrp, s)
        return data if data is not None and len(data) == length else None

    return enc, dec


# cp1<Q>: a note's 32-byte x-only taproot output key. Decoding a cp1, ck1
# or cw1 a request carries is spend.py's job; this encoder is for this
# mint's own output (tests, the frontend).
encode_cp1, decode_cp1 = _fixed_length_codec("cp", 32)


# cs1<sig>: a 65-byte recoverable signature, produced by SERVICE - an
# issuance certificate, never a spend authorization on its own. Its HRP
# folds in the certificate's own amount_msat the way a BOLT-11 invoice's
# HRP folds in its amount (e.g. "cs10n" for 1000 msat), via BOLT-11's own
# amount<>multiplier rules (bolt11.utils) - a verifier reads the amount
# straight off the certificate, nothing needs to travel alongside it.
def encode_cs1(amount_msat: int, signature: bytes) -> str:
    if len(signature) != 65:
        raise ValueError(f"cs1... payload must be 65 bytes, got {len(signature)}")
    return encode(f"cs{msat_to_amount(amount_msat)}", signature)


def decode_cs1(s: str) -> tuple[int, bytes] | None:
    """Inverse of encode_cs1: (amount_msat, signature), or None on any
    malformed input. The HRP itself (the encoded amount) is what is being
    recovered, so it has to be split out of `s` before the checksum can be
    verified against it - decode() can't be reused here."""
    if any(ord(c) < 33 or ord(c) > 126 for c in s):
        return None
    if s.lower() != s and s.upper() != s:
        return None
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        return None
    found_hrp, data_part = s[:pos], s[pos + 1 :]
    if not found_hrp.startswith("cs") or not all(c in CHARSET for c in data_part):
        return None
    try:
        amount_msat = int(amount_to_msat(found_hrp[2:]))
    except Bolt11AmountInvalidException:
        return None
    data = [CHARSET.find(c) for c in data_part]
    if not _verify_checksum(found_hrp, data):
        return None
    decoded = convertbits(data[:-6], 5, 8, False)
    if decoded is None or len(decoded) != 65:
        return None
    return amount_msat, bytes(decoded)


# cx1<P || chaincode>: a 64-byte watch-only export of a WALLET's derivation
# branch for one SERVICE - never appears in a mint interaction itself, only
# on POST /p/{mint_id}/{username}.
encode_cx1, decode_cx1 = _fixed_length_codec("cx", 64)


def decode_npub(s: str) -> bytes | None:
    """Inverse of NIP-19's npub encoding: classic bech32 (BIP-173, not
    bech32m - Nostr predates BIP-350's checksum), hrp "npub", a 32-byte
    x-only pubkey payload. None on any malformed input - never raises.
    Only ever used on username registration: this mint accepts an npub to
    serve alongside a registered username on nostr.json (NIP-05)."""
    hrp, data = bech32_decode(s)
    if hrp != "npub" or data is None:
        return None
    decoded = convertbits(data, 5, 8, False)
    return bytes(decoded) if decoded is not None and len(decoded) == 32 else None
